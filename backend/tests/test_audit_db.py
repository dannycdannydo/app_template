"""Real-database integration tests for the audit module (Scope §6.1).

The fakes in ``test_audit.py`` prove the request-flow contract but never
execute SQL, so the append-only table shape, the real filter behaviour and the
round-trip of the JSONB metadata could silently regress. These tests run the
real migration and the real services against a reachable PostgreSQL, using the
same skip pattern as ``test_records_db.py``: migrated to head up front,
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.audit import service
from app.modules.audit.queries import audit_events_statement
from app.modules.organisations.models import Organisation
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

    Requires a reachable PostgreSQL as configured by ``DATABASE_URL``; skipped
    otherwise. Reverting to base keeps the test database clean for the other
    migration smoke tests, whichever runs first.
    """
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


async def _seed(
    session: AsyncSession,
) -> tuple[User, Organisation]:
    """Seed one user and one organisation the audit rows can reference.

    The workos user id is unique per call because the module-scoped migration
    fixture upgrades once for the whole module: several tests seed rows into
    the same tables, and ``workos_user_id`` is unique.
    """
    unique = uuid.uuid4().hex[:8]
    user = User(
        workos_user_id=f"audit_db_user_{unique}",
        email=f"audit_{unique}@example.com",
        name="Audit User",
    )
    session.add(user)
    await session.flush()
    organisation = Organisation(name=f"Audit DB {unique} Ltd")
    session.add(organisation)
    await session.flush()
    await session.commit()
    return user, organisation


async def test_audit_table_shape_is_append_only(migrated_database: str) -> None:
    """Scope §6.1: the migrated table has no update column and a JSONB metadata."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable "
                        "FROM information_schema.columns WHERE table_name = 'audit_events'"
                    )
                )
            ).all()
        columns = {row.column_name: row for row in rows}
        assert "updated_at" not in columns
        assert "created_at" in columns
        assert columns["metadata"].data_type == "jsonb"
        assert columns["organisation_id"].is_nullable == "YES"
        assert columns["actor_user_id"].is_nullable == "YES"
    finally:
        await engine.dispose()


async def test_record_event_round_trips_with_metadata(migrated_database: str) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            user, organisation = await _seed(session)
            event = await service.record_event(
                session,
                organisation_id=organisation.id,
                actor_user_id=user.id,
                action="organisation.created",
                resource_type="organisation",
                resource_id=str(organisation.id),
                metadata={"request_id": "req-123", "client_ip": "127.0.0.1"},
            )
            await session.commit()

            # Re-read through the real query to prove the row persisted with
            # its JSONB metadata intact.
            fresh = await session.scalar(audit_events_statement(action="organisation.created"))
            assert fresh is not None
            assert fresh.id == event.id
            assert fresh.organisation_id == organisation.id
            assert fresh.actor_user_id == user.id
            assert fresh.event_metadata == {"request_id": "req-123", "client_ip": "127.0.0.1"}
    finally:
        await engine.dispose()


async def test_list_audit_events_filters_by_org_actor_and_action(migrated_database: str) -> None:
    """Scope §6.1: the approved filters actually filter at the SQL level."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            user, org_a = await _seed(session)
            org_b = Organisation(name="Second Ltd")
            session.add(org_b)
            await session.commit()

            await service.record_event(
                session,
                organisation_id=org_a.id,
                actor_user_id=user.id,
                action="organisation.created",
                resource_type="organisation",
                resource_id=str(org_a.id),
            )
            await service.record_event(
                session,
                organisation_id=org_b.id,
                actor_user_id=user.id,
                action="organisation.created",
                resource_type="organisation",
                resource_id=str(org_b.id),
            )
            await service.record_event(
                session,
                organisation_id=org_a.id,
                actor_user_id=user.id,
                action="record.deleted",
                resource_type="record",
                resource_id="record-1",
            )
            await session.commit()

            by_org, total_by_org = await service.list_audit_events(
                session, page=1, page_size=50, organisation_id=org_a.id
            )
            assert total_by_org == 2
            assert {event.organisation_id for event in by_org} == {org_a.id}

            # Actor and action filters are combined with the org filter so the
            # assertions stay deterministic even though the module-scoped
            # migration fixture leaves earlier tests' rows in the database.
            by_actor, total_by_actor = await service.list_audit_events(
                session,
                page=1,
                page_size=50,
                organisation_id=org_a.id,
                actor_user_id=user.id,
            )
            assert total_by_actor == 2
            assert all(event.actor_user_id == user.id for event in by_actor)

            by_action, total_by_action = await service.list_audit_events(
                session,
                page=1,
                page_size=50,
                organisation_id=org_a.id,
                action="organisation.created",
            )
            assert total_by_action == 1
            assert all(event.action == "organisation.created" for event in by_action)

            combined, total_combined = await service.list_audit_events(
                session,
                page=1,
                page_size=50,
                organisation_id=org_a.id,
                actor_user_id=user.id,
                action="organisation.created",
            )
            assert total_combined == 1
            assert all(event.organisation_id == org_a.id for event in combined)
    finally:
        await engine.dispose()
