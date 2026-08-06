"""Real-database integration tests for the records module (v0.2 Scope §6.5).

The fakes in ``test_records.py`` prove the request-flow contract but never
execute SQL, so the org-scoping (cross-organisation access must return 404,
never leak) could silently regress at the query level. These tests run the
real migration and the real service against a reachable PostgreSQL, using the
same skip pattern as ``test_permissions_db.py``: migrated to head up front,
reverted to base afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import NotFoundError
from app.modules.organisations.models import Organisation
from app.modules.records import service
from app.modules.records.models import Record

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


async def _create_org_and_record(
    session: AsyncSession,
    *,
    org_name: str,
    title: str,
) -> tuple[Organisation, Record]:
    """Seed one organisation and one record inside it, and commit."""
    organisation = Organisation(name=org_name)
    session.add(organisation)
    await session.commit()
    record = await service.create_record(
        session,
        organisation_id=organisation.id,
        title=title,
        body="Seeded body",
    )
    return organisation, record


async def test_records_crud_within_org(migrated_database: str) -> None:
    """Acceptance §5.7: CRUD works inside the caller's organisation."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation, record = await _create_org_and_record(
                session, org_name="Records CRUD Ltd", title="First"
            )

            fetched = await service.get_record(
                session,
                organisation_id=organisation.id,
                record_id=record.id,
            )
            assert fetched.title == "First"
            assert fetched.body == "Seeded body"

            updated = await service.update_record(
                session,
                organisation_id=organisation.id,
                record_id=record.id,
                title="Second",
                body=None,  # untouched fields keep their values
            )
            assert updated.title == "Second"
            assert updated.body == "Seeded body"

            records, total = await service.list_records(
                session,
                organisation_id=organisation.id,
                page=1,
                page_size=50,
            )
            assert total == 1
            assert [record.title for record in records] == ["Second"]

            await service.delete_record(
                session,
                organisation_id=organisation.id,
                record_id=record.id,
            )
            org_count = await session.scalar(
                select(func.count())
                .select_from(Record)
                .where(Record.organisation_id == organisation.id)
            )
            assert org_count == 0
    finally:
        await engine.dispose()


async def test_cross_org_access_is_not_found(migrated_database: str) -> None:
    """Acceptance §5.7: reading or updating another org's record returns 404."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org_a, record_a = await _create_org_and_record(
                session, org_name="Org A Ltd", title="A record"
            )
            org_b = Organisation(name="Org B Ltd")
            session.add(org_b)
            await session.commit()

            # A record id that exists in another organisation is not found.
            with pytest.raises(NotFoundError):
                await service.get_record(
                    session,
                    organisation_id=org_b.id,
                    record_id=record_a.id,
                )
            with pytest.raises(NotFoundError):
                await service.update_record(
                    session,
                    organisation_id=org_b.id,
                    record_id=record_a.id,
                    title="Hijacked",
                    body=None,
                )
            with pytest.raises(NotFoundError):
                await service.delete_record(
                    session,
                    organisation_id=org_b.id,
                    record_id=record_a.id,
                )

            # The record is untouched and the other org's list stays empty.
            pristine = await service.get_record(
                session,
                organisation_id=org_a.id,
                record_id=record_a.id,
            )
            assert pristine.title == "A record"
            records_b, total_b = await service.list_records(
                session, organisation_id=org_b.id, page=1, page_size=50
            )
            assert records_b == []
            assert total_b == 0
    finally:
        await engine.dispose()


async def test_list_pagination_envelope(migrated_database: str) -> None:
    """The list returns the envelope and pages over org-scoped rows."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = Organisation(name="Paging Ltd")
            session.add(organisation)
            await session.commit()
            for index in range(3):
                await service.create_record(
                    session,
                    organisation_id=organisation.id,
                    title=f"Record {index}",
                    body="",
                )

            page_one, total = await service.list_records(
                session, organisation_id=organisation.id, page=1, page_size=2
            )
            assert total == 3
            assert len(page_one) == 2
            page_two, _total = await service.list_records(
                session, organisation_id=organisation.id, page=2, page_size=2
            )
            assert len(page_two) == 1
            # Newest first: page two carries the oldest of the three.
            assert page_two[0].title == "Record 0"
    finally:
        await engine.dispose()


async def test_delete_outside_org_leaves_record_intact(migrated_database: str) -> None:
    """Deleting through another org cannot remove a record (404, not a leak)."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org_a, record_a = await _create_org_and_record(
                session, org_name="Org A Ltd", title="Keep me"
            )
            org_b = Organisation(name="Org B Ltd")
            session.add(org_b)
            await session.commit()

            with pytest.raises(NotFoundError):
                await service.delete_record(
                    session,
                    organisation_id=org_b.id,
                    record_id=record_a.id,
                )
            org_a_count = await session.scalar(
                select(func.count()).select_from(Record).where(Record.organisation_id == org_a.id)
            )
            assert org_a_count == 1
            still_there = await service.get_record(
                session,
                organisation_id=org_a.id,
                record_id=record_a.id,
            )
            assert still_there.title == "Keep me"
    finally:
        await engine.dispose()
