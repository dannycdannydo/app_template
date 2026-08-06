"""Real-database integration tests for the WorkOS org mapping (Scope §6.3).

The fakes in ``test_workos_org_mapping.py`` prove the request-flow contract
but never execute SQL, so the migrated column shape, the unique constraint and
the persistence of the mapping could silently regress. These tests run the
real migration and the real services against a reachable PostgreSQL, using
the same skip pattern as ``test_audit_db.py``: migrated to head up front,
reverted to base afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.context_helpers import FakeWorkOSOrganizationsProvider

from app.modules.organisations.models import Organisation
from app.modules.platform_admin import service
from app.modules.users.models import User

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _database_reachable(database_url: str) -> bool:
    """Probe the configured database with a short async engine connect."""

    async def _probe() -> bool:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(_probe())


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards.

    Reverting to base keeps the test database clean for the other migration
    smoke tests, whichever runs first.
    """
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


def _session_factory(database_url: str):
    engine = create_async_engine(database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_workos_organisation_id_column_is_nullable_and_unique(
    migrated_database: str,
) -> None:
    """Scope §6.3: the mapping column is nullable with a database unique index."""
    engine, _ = _session_factory(migrated_database)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns WHERE table_name = 'organisations'"
                    )
                )
            ).all()
            constraints = (
                await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'organisations'::regclass AND contype = 'u'"
                    )
                )
            ).all()
        columns = {row.column_name: row for row in rows}
        assert columns["workos_organisation_id"].data_type == "character varying"
        assert columns["workos_organisation_id"].is_nullable == "YES"
        unique_names = {row.conname for row in constraints}
        assert "uq_organisations_workos_organisation_id" in unique_names
    finally:
        await engine.dispose()


async def test_create_platform_organisation_round_trips_mapping(
    migrated_database: str,
) -> None:
    """Scope §6.3: the service persists the internal org and its mapping."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = User(
                workos_user_id=f"mapping_db_actor_{uuid.uuid4().hex[:8]}",
                email="platform@example.com",
                name="Platform Admin",
            )
            session.add(actor)
            await session.commit()
        async with session_factory() as session:
            organisation = await service.create_platform_organisation(
                session,
                actor=actor,
                name="Round Trip Ltd",
                workos=FakeWorkOSOrganizationsProvider(),
            )
            created_id = organisation.id
            mapping = organisation.workos_organisation_id
            assert mapping is not None

            # Re-read through a fresh session to prove the mapping persisted.
            fresh = await session.get(Organisation, created_id)
            assert fresh is not None
            assert fresh.name == "Round Trip Ltd"
            assert fresh.workos_organisation_id == mapping
    finally:
        await engine.dispose()


async def test_workos_organisation_id_uniqueness_is_enforced(migrated_database: str) -> None:
    """Scope §6.3: two internal orgs can never claim the same WorkOS org."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            first = Organisation(name="Unique A Ltd", workos_organisation_id="org_workos_dup")
            session.add(first)
            await session.commit()

            duplicate = Organisation(name="Unique B Ltd", workos_organisation_id="org_workos_dup")
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


async def test_ensure_workos_organisation_backfills_existing_organisation(
    migrated_database: str,
) -> None:
    """Scope §6.3: a pre-existing org gains its mapping lazily and persistently."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = Organisation(name="Legacy Ltd")
            session.add(organisation)
            await session.commit()
            legacy_id = organisation.id

        async with session_factory() as session:
            organisation = await session.get(Organisation, legacy_id)
            assert organisation is not None
            assert organisation.workos_organisation_id is None

            await service.ensure_workos_organisation(
                session,
                organisation,
                FakeWorkOSOrganizationsProvider(),
            )
            await session.commit()
            mapping = organisation.workos_organisation_id
            assert mapping is not None

            fresh = await session.get(Organisation, legacy_id)
            assert fresh is not None
            assert fresh.workos_organisation_id == mapping
    finally:
        await engine.dispose()
