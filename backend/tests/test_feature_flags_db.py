"""Real-database integration tests for feature flags (Scope §6.7).

The fakes in ``test_feature_flags.py`` prove the request-flow contract but
never execute SQL, so the persistence of the override upsert, the enforcement
helper against real rows and the ``(organisation_id, feature_key)`` unique
pair could silently regress. These tests run the real migration and the real
services against a reachable PostgreSQL, using the same skip pattern as the
other ``*_db.py`` modules: migrated to head up front, reverted to base
afterwards.
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
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import PermissionDenied
from app.core.feature_flags import FEATURE_RECORDS_DELETION, is_feature_enabled
from app.modules.audit.service import ACTION_FEATURE_FLAG_CHANGED
from app.modules.feature_flags import service
from app.modules.feature_flags.models import OrganisationFeature
from app.modules.organisations.models import Organisation
from app.modules.records import service as records_service
from app.modules.records.models import Record
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
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
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


async def _seed_organisation(session: AsyncSession) -> Organisation:
    organisation = Organisation(name=f"Flags Ltd {uuid.uuid4().hex[:8]}")
    session.add(organisation)
    await session.commit()
    return organisation


async def _seed_actor(session: AsyncSession) -> User:
    actor = User(
        workos_user_id=f"admin_{uuid.uuid4().hex[:8]}",
        email="platform@example.com",
        name="Platform Admin",
    )
    session.add(actor)
    await session.commit()
    return actor


async def test_set_feature_flag_persists_override_and_audits(migrated_database: str) -> None:
    """The upsert round-trips, and feature_flag.changed is written once."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            organisation = await _seed_organisation(session)

            state = await service.set_feature_flag(
                session,
                actor=actor,
                feature_key=FEATURE_RECORDS_DELETION,
                organisation_id=organisation.id,
                enabled=True,
                configuration_json={"require_confirmation": True},
            )
            assert state.enabled is True
            assert state.overridden is True
            assert state.configuration_json == {"require_confirmation": True}

            row = await session.scalar(
                select(OrganisationFeature).where(
                    OrganisationFeature.organisation_id == organisation.id,
                    OrganisationFeature.feature_key == FEATURE_RECORDS_DELETION,
                )
            )
            assert row is not None
            assert row.enabled is True

            actions = (
                await session.execute(
                    text(
                        "SELECT action FROM audit_events "
                        "WHERE resource_type = 'feature_flag' AND resource_id = :key"
                    ).bindparams(key=FEATURE_RECORDS_DELETION)
                )
            ).all()
            assert [action for (action,) in actions] == [ACTION_FEATURE_FLAG_CHANGED]

        # The same pair again updates the single row in place.
        async with session_factory() as session:
            state = await service.set_feature_flag(
                session,
                actor=actor,
                feature_key=FEATURE_RECORDS_DELETION,
                organisation_id=organisation.id,
                enabled=False,
                configuration_json=None,
            )
            assert state.enabled is False
            row = await session.scalar(
                select(OrganisationFeature).where(
                    OrganisationFeature.organisation_id == organisation.id,
                    OrganisationFeature.feature_key == FEATURE_RECORDS_DELETION,
                )
            )
            assert row is not None
            assert row.enabled is False
            rows = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM organisation_features "
                        "WHERE organisation_id = :oid AND feature_key = :key"
                    ).bindparams(oid=organisation.id, key=FEATURE_RECORDS_DELETION)
                )
            ).scalar_one()
            assert rows == 1
    finally:
        await engine.dispose()


async def test_organisation_feature_pair_is_unique(migrated_database: str) -> None:
    """The unique (organisation_id, feature_key) pair rejects duplicates."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            session.add(
                OrganisationFeature(
                    organisation_id=organisation.id,
                    feature_key=FEATURE_RECORDS_DELETION,
                    enabled=True,
                )
            )
            await session.commit()
            session.add(
                OrganisationFeature(
                    organisation_id=organisation.id,
                    feature_key=FEATURE_RECORDS_DELETION,
                    enabled=False,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


async def test_is_feature_enabled_enforces_and_isolates(migrated_database: str) -> None:
    """Default off, override on, and never leaking across organisations."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation_a = await _seed_organisation(session)
            organisation_b = await _seed_organisation(session)

        # No rows yet: default off for both (fresh session per step, matching
        # real request boundaries — the helper memoises per session).
        async with session_factory() as session:
            assert (
                await is_feature_enabled(
                    session,
                    organisation_id=organisation_a.id,
                    feature_key=FEATURE_RECORDS_DELETION,
                )
            ) is False
            assert (
                await is_feature_enabled(
                    session,
                    organisation_id=organisation_b.id,
                    feature_key=FEATURE_RECORDS_DELETION,
                )
            ) is False

        # Enable it only for organisation A (one platform request).
        async with session_factory() as session:
            await service.set_feature_flag(
                session,
                actor=await _seed_actor(session),
                feature_key=FEATURE_RECORDS_DELETION,
                organisation_id=organisation_a.id,
                enabled=True,
                configuration_json=None,
            )

        # A later request sees A on, B untouched.
        async with session_factory() as session:
            assert (
                await is_feature_enabled(
                    session,
                    organisation_id=organisation_a.id,
                    feature_key=FEATURE_RECORDS_DELETION,
                )
            ) is True
            assert (
                await is_feature_enabled(
                    session,
                    organisation_id=organisation_b.id,
                    feature_key=FEATURE_RECORDS_DELETION,
                )
            ) is False

            # Unknown keys are never grantable.
            assert (
                await is_feature_enabled(
                    session, organisation_id=organisation_a.id, feature_key="no.such_flag"
                )
            ) is False
    finally:
        await engine.dispose()


async def test_delete_record_enforced_against_real_rows(migrated_database: str) -> None:
    """The service gate: blocked by default, allowed once the flag is on."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = await _seed_organisation(session)
            record = await records_service.create_record(
                session,
                organisation_id=organisation.id,
                title="Keep me",
                body="",
            )

        # Default off: the delete is denied and the record survives.
        async with session_factory() as session:
            with pytest.raises(PermissionDenied) as excinfo:
                await records_service.delete_record(
                    session,
                    organisation_id=organisation.id,
                    record_id=record.id,
                )
            assert excinfo.value.code == "feature_disabled"
            assert (
                await session.scalar(
                    select(func.count()).select_from(Record).where(Record.id == record.id)
                )
                == 1
            )

        # Platform enables the flag (one platform request).
        async with session_factory() as session:
            await service.set_feature_flag(
                session,
                actor=await _seed_actor(session),
                feature_key=FEATURE_RECORDS_DELETION,
                organisation_id=organisation.id,
                enabled=True,
                configuration_json=None,
            )

        # A later request may now delete the record.
        async with session_factory() as session:
            await records_service.delete_record(
                session,
                organisation_id=organisation.id,
                record_id=record.id,
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(Record).where(Record.id == record.id)
                )
                == 0
            )
    finally:
        await engine.dispose()
