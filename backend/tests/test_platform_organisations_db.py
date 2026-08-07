"""Real-database integration tests for platform organisation administration (Scope §6.9).

The fakes in ``test_platform_organisations.py`` prove the request-flow contract
but never execute SQL, so the ordering of the list, the persistence of a
rename and the audit row written by it could silently regress. These tests run
the real migration and the real services against a reachable PostgreSQL, using
the same skip pattern as ``test_workos_org_mapping_db.py``: migrated to head up
front, reverted to base afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.audit.models import AuditEvent
from app.modules.audit.service import ACTION_ORGANISATION_UPDATED
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


async def test_list_organisations_orders_newest_first(migrated_database: str) -> None:
    """Scope §6.9: the platform catalogue lists every organisation, newest first."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            # Explicit, clearly separated timestamps: rows inserted in one
            # transaction share the server clock, and UUIDv7 ids generated in
            # the same millisecond sort by random bits, so the ordering test
            # would otherwise be racy. Fixed timestamps make the assertion
            # deterministic.
            older = datetime.now(UTC) - timedelta(days=2)
            newer = datetime.now(UTC) - timedelta(days=1)
            first = Organisation(name="First Ltd", created_at=older)
            second = Organisation(name="Second Ltd", created_at=newer)
            session.add_all([first, second])
            await session.commit()

        async with session_factory() as session:
            organisations, total = await service.list_organisations(session, page=1, page_size=50)
            names = [organisation.name for organisation in organisations]
            assert total == 2
            assert "First Ltd" in names
            assert "Second Ltd" in names
            # Newest first: the second-inserted organisation precedes the first.
            assert names.index("Second Ltd") < names.index("First Ltd")
    finally:
        await engine.dispose()


async def test_update_organisation_persists_rename_and_audits(migrated_database: str) -> None:
    """Scope §6.9: a rename persists and writes one ``organisation.updated`` row."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = User(
                workos_user_id=f"org_admin_db_{uuid.uuid4().hex[:8]}",
                email="platform@example.com",
                name="Platform Admin",
            )
            organisation = Organisation(name="Acme Ltd")
            session.add_all([actor, organisation])
            await session.commit()
            organisation_id = organisation.id

        async with session_factory() as session:
            updated = await service.update_organisation(
                session,
                actor=actor,
                organisation_id=organisation_id,
                name="Acme International",
            )
            assert updated.name == "Acme International"

            fresh = await session.get(Organisation, organisation_id)
            assert fresh is not None
            assert fresh.name == "Acme International"

            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == ACTION_ORGANISATION_UPDATED,
                    AuditEvent.resource_id == str(organisation_id),
                )
            )
            assert audit_count == 1
    finally:
        await engine.dispose()
