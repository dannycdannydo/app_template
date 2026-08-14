"""Real-database integration tests for the outbox data contract (plan P1).

The durable outbox contract is a PostgreSQL guarantee, so these tests run the
real migration and the real outbox service against a reachable PostgreSQL
(same skip pattern as ``test_jobs_db.py``: migrated to head up front, reverted
to base afterwards). They prove: job + dispatch event commit and roll back
together; the event copies the validated organisation id; maintenance events
have no tenant context; the unique deduplication key and the status/version/
attempt/payload bounds are enforced by the database; and the due-claim,
aggregate-history, stale-claim and retention query helpers select exactly the
rows the coordinator will need. The pure payload-contract tests live in
``test_outbox.py``.
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
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.jobs.models import Job
from app.modules.organisations.models import Organisation
from app.modules.outbox import service as outbox_service
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus
from app.modules.outbox.queries import (
    due_outbox_events_statement,
    outbox_events_for_aggregate_statement,
    published_events_retention_statement,
    stale_claim_events_statement,
)

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

    Reverting to base exercises the downgrade path of every migration
    including ``a5b6c7d8e9f0`` (outbox_events + job delivery columns).
    """
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


def _session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        create_async_engine(database_url, poolclass=NullPool), expire_on_commit=False
    )


async def _create_org(session: AsyncSession) -> Organisation:
    organisation = Organisation(name=f"Outbox DB {uuid.uuid4().hex[:8]} Ltd")
    session.add(organisation)
    await session.commit()
    return organisation


async def _create_job(session: AsyncSession, organisation_id: uuid.UUID) -> Job:
    job = Job(
        organisation_id=organisation_id,
        job_type="file.processing",
        input_reference="file-1",
    )
    session.add(job)
    # Flush so ``job.id`` exists (SQLAlchemy applies the id default on flush)
    # before it is referenced as the dispatch aggregate.
    await session.flush()
    return job


async def _event_count(session: AsyncSession, *, job_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            text("SELECT count(*) FROM outbox_events WHERE aggregate_id = :job_id"),
            {"job_id": job_id},
        )
    ) or 0


async def test_job_and_dispatch_event_commit_together(migrated_database: str) -> None:
    """The durable job row and its dispatch event land in one transaction."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)
    try:
        async with factory() as session:
            organisation = await _create_org(session)
            job = await _create_job(session, organisation.id)
            event = await outbox_service.create_dispatch_event(
                session, organisation_id=organisation.id, job_id=job.id
            )
            await session.commit()

        async with factory() as session:
            stored_job = await session.get(Job, job.id)
            stored_event = await session.get(OutboxEvent, event.id)
            assert stored_job is not None
            assert stored_event is not None
            assert stored_event.organisation_id == organisation.id
            assert stored_event.event_type == "job.dispatch_requested"
            assert stored_event.event_version == 1
            assert stored_event.aggregate_type == "job"
            assert stored_event.aggregate_id == job.id
            assert stored_event.payload == {"job_id": str(job.id)}
            assert stored_event.deduplication_key == f"job.dispatch_requested:{job.id}"
            assert stored_event.status == OutboxEventStatus.PENDING
            assert stored_event.attempt_count == 0
            assert stored_event.claim_token is None
            assert stored_event.available_at is not None
            assert stored_event.created_at is not None
    finally:
        await engine.dispose()


async def test_job_and_dispatch_event_rollback_together(migrated_database: str) -> None:
    """A rollback leaves neither the job nor its dispatch event behind."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)
    try:
        async with factory() as session:
            organisation = await _create_org(session)
            job = await _create_job(session, organisation.id)
            event = await outbox_service.create_dispatch_event(
                session, organisation_id=organisation.id, job_id=job.id
            )
            await session.rollback()
            assert await session.get(Job, job.id) is None
            assert await session.get(OutboxEvent, event.id) is None

        async with factory() as session:
            assert (await session.get(Job, job.id)) is None
            assert (await session.get(OutboxEvent, event.id)) is None
    finally:
        await engine.dispose()


async def test_dispatch_event_carries_validated_organisation(migrated_database: str) -> None:
    """Tenant association: the event copies the job's organisation id."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)
    try:
        async with factory() as session:
            organisation = await _create_org(session)
            job = await _create_job(session, organisation.id)
            event = await outbox_service.create_dispatch_event(
                session, organisation_id=organisation.id, job_id=job.id
            )
            await session.commit()

        async with factory() as session:
            stored = await session.get(OutboxEvent, event.id)
            assert stored is not None
            assert stored.organisation_id == organisation.id
            assert stored.organisation_id != uuid.UUID(int=0)
    finally:
        await engine.dispose()


async def test_maintenance_event_has_null_organisation(migrated_database: str) -> None:
    """Maintenance events are global: null org, no aggregate, no tenant data."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)
    try:
        async with factory() as session:
            event = await outbox_service.create_schedule_event(
                session,
                event_type="ai.retention",
                schedule_key="ai.retention:2026-08-14T00",
                payload={},
            )
            await session.commit()

        async with factory() as session:
            stored = await session.get(OutboxEvent, event.id)
            assert stored is not None
            assert stored.organisation_id is None
            assert stored.aggregate_type is None
            assert stored.aggregate_id is None
            assert stored.payload == {}
            assert stored.status == OutboxEventStatus.PENDING
            assert stored.deduplication_key == "ai.retention:2026-08-14T00"
    finally:
        await engine.dispose()


async def test_deduplication_key_is_unique(migrated_database: str) -> None:
    """Two dispatch requests for the same job cannot both persist."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)
    try:
        async with factory() as session:
            organisation = await _create_org(session)
            job = await _create_job(session, organisation.id)
            await outbox_service.create_dispatch_event(
                session, organisation_id=organisation.id, job_id=job.id
            )
            await session.commit()

        async with factory() as session:
            await outbox_service.create_dispatch_event(
                session, organisation_id=organisation.id, job_id=job.id
            )
            with pytest.raises(IntegrityError):
                await session.commit()

        async with factory() as session:
            assert await _event_count(session, job_id=job.id) == 1
    finally:
        await engine.dispose()


async def test_payload_bound_is_enforced_by_database(migrated_database: str) -> None:
    """The check constraint rejects an over-bounded payload at the database."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)
    try:
        async with factory() as session:
            organisation = await _create_org(session)
            event = OutboxEvent(
                organisation_id=organisation.id,
                event_type="test.payload_bounds",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"blob": "x" * 20000},
                deduplication_key=f"test.payload_bounds:{uuid.uuid4()}",
                status=OutboxEventStatus.PENDING,
            )
            session.add(event)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


async def test_state_constraints_are_enforced(migrated_database: str) -> None:
    """Invalid status, version and attempt values are rejected by the schema."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)

    def _bad_event(**overrides: object) -> OutboxEvent:
        # ``status``/``event_version`` defaults apply at flush; only the
        # explicit fields and the override are set so overrides never collide.
        return OutboxEvent(
            event_type="test.constraints",
            payload={},
            deduplication_key=f"test.constraints:{uuid.uuid4()}",
            **overrides,  # type: ignore[arg-type]
        )

    try:
        async with factory() as session:
            organisation = await _create_org(session)
            session.add(_bad_event(organisation_id=organisation.id, status="nonsense"))
            with pytest.raises(IntegrityError):
                await session.commit()

        async with factory() as session:
            session.add(_bad_event(event_version=0))
            with pytest.raises(IntegrityError):
                await session.commit()

        async with factory() as session:
            session.add(_bad_event(attempt_count=-1))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


async def test_migration_adds_outbox_table_and_job_columns(migrated_database: str) -> None:
    """After upgrade the outbox table and job delivery columns exist."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            outbox_exists = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'outbox_events')"
                )
            )
            dispatch_column = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'jobs' AND column_name = 'dispatch_id')"
                )
            )
            lease_column = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'jobs' AND column_name = 'execution_lease_expires_at')"
                )
            )
        assert outbox_exists is True
        assert dispatch_column is True
        assert lease_column is True
    finally:
        await engine.dispose()


async def test_due_events_statement_selects_only_pending_due_rows(migrated_database: str) -> None:
    """Due claims cover pending past-due rows only, oldest first."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)
    try:
        async with factory() as session:
            now = datetime.now(UTC)
            due_old = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"job_id": str(uuid.uuid4())},
                deduplication_key=f"test.due:{uuid.uuid4()}",
                status=OutboxEventStatus.PENDING,
                available_at=now - timedelta(minutes=5),
            )
            due_new = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"job_id": str(uuid.uuid4())},
                deduplication_key=f"test.due:{uuid.uuid4()}",
                status=OutboxEventStatus.PENDING,
                available_at=now - timedelta(minutes=1),
            )
            not_due = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"job_id": str(uuid.uuid4())},
                deduplication_key=f"test.due:{uuid.uuid4()}",
                status=OutboxEventStatus.PENDING,
                available_at=now + timedelta(minutes=5),
            )
            already_published = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"job_id": str(uuid.uuid4())},
                deduplication_key=f"test.due:{uuid.uuid4()}",
                status=OutboxEventStatus.PUBLISHED,
                available_at=now - timedelta(minutes=5),
            )
            session.add_all([due_old, due_new, not_due, already_published])
            await session.commit()

        async with factory() as session:
            rows = list(
                (
                    await session.scalars(due_outbox_events_statement(at_or_before=now, limit=10))
                ).all()
            )
            ids = [row.id for row in rows]
            # Earlier tests leave their own pending rows behind, so assert
            # membership and relative order rather than an exact list.
            assert due_old.id in ids
            assert due_new.id in ids
            assert ids.index(due_old.id) < ids.index(due_new.id)
            assert not_due.id not in ids
            assert already_published.id not in ids
    finally:
        await engine.dispose()


async def test_aggregate_history_orders_newest_first(migrated_database: str) -> None:
    """Aggregate-history queries return the job's events newest first."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)
    try:
        async with factory() as session:
            job_id = uuid.uuid4()
            old = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=job_id,
                payload={"job_id": str(job_id)},
                deduplication_key=f"test.history:{uuid.uuid4()}",
                status=OutboxEventStatus.PUBLISHED,
            )
            new = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=job_id,
                payload={"job_id": str(job_id)},
                deduplication_key=f"test.history:{uuid.uuid4()}",
                status=OutboxEventStatus.PENDING,
            )
            session.add_all([old, new])
            await session.commit()

        async with factory() as session:
            rows = list(
                (await session.scalars(outbox_events_for_aggregate_statement("job", job_id))).all()
            )
            ids = [row.id for row in rows]
            # Both events belong to the aggregate and the statement orders the
            # history by id (UUIDv7 is time-ordered). Two ids generated in the
            # same millisecond may order either way, so assert the descending
            # invariant rather than a fixed pair.
            assert set(ids) == {old.id, new.id}
            assert ids == sorted(ids, reverse=True)
    finally:
        await engine.dispose()


async def test_stale_claim_and_retention_queries_select_only_their_rows(
    migrated_database: str,
) -> None:
    """Stale-claim recovery and published cleanup pick exactly their rows."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = _session_factory(migrated_database)
    try:
        async with factory() as session:
            now = datetime.now(UTC)
            stale = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"job_id": str(uuid.uuid4())},
                deduplication_key=f"test.stale:{uuid.uuid4()}",
                status=OutboxEventStatus.PUBLISHING,
                claimed_at=now - timedelta(minutes=30),
            )
            fresh = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"job_id": str(uuid.uuid4())},
                deduplication_key=f"test.stale:{uuid.uuid4()}",
                status=OutboxEventStatus.PUBLISHING,
                claimed_at=now - timedelta(seconds=5),
            )
            published_old = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"job_id": str(uuid.uuid4())},
                deduplication_key=f"test.retention:{uuid.uuid4()}",
                status=OutboxEventStatus.PUBLISHED,
                created_at=now - timedelta(days=31),
            )
            published_new = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"job_id": str(uuid.uuid4())},
                deduplication_key=f"test.retention:{uuid.uuid4()}",
                status=OutboxEventStatus.PUBLISHED,
                created_at=now - timedelta(days=10),
            )
            dead = OutboxEvent(
                event_type="job.dispatch_requested",
                event_version=1,
                aggregate_type="job",
                aggregate_id=uuid.uuid4(),
                payload={"job_id": str(uuid.uuid4())},
                deduplication_key=f"test.retention:{uuid.uuid4()}",
                status=OutboxEventStatus.DEAD,
                created_at=now - timedelta(days=31),
            )
            session.add_all([stale, fresh, published_old, published_new, dead])
            await session.commit()

        async with factory() as session:
            stale_rows = list(
                (
                    await session.scalars(
                        stale_claim_events_statement(
                            claimed_before=now - timedelta(minutes=10), limit=10
                        )
                    )
                ).all()
            )
            assert [row.id for row in stale_rows] == [stale.id]

            retention_rows = list(
                (
                    await session.scalars(
                        published_events_retention_statement(
                            created_before=now - timedelta(days=30), limit=10
                        )
                    )
                ).all()
            )
            assert [row.id for row in retention_rows] == [published_old.id]
            assert dead.id not in [row.id for row in retention_rows]
    finally:
        await engine.dispose()
