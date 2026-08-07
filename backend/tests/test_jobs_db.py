"""Real-database integration tests for the durable job service (Scope §6.4).

The retry-mechanics tests in ``test_jobs.py`` never execute SQL, so the job
lifecycle could silently regress at the query and constraint level. These
tests run the real migration and the real service against a reachable
PostgreSQL (same skip pattern as ``test_records_db.py``: migrated to head up
front, reverted to base afterwards). The broker is an in-process StubBroker
with a sync recording task, so enqueueing is proven without Redis; the full
worker round trip against a real Redis broker is ``test_jobs_broker.py``.

Acceptance §5.6/§5.7 are proven here: the durable row moves
queued -> running -> succeeded (or -> failed with ``error_code`` /
``error_message``), ``attempt_count`` and ``started_at``/``completed_at`` are
maintained, ``succeed``/``fail`` are idempotent, terminal states are never
re-run, and the ``job.succeeded`` / ``job.failed`` audit rows are written in
the same transaction as the transition.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dramatiq
import pytest
from alembic import command
from alembic.config import Config
from dramatiq.brokers.stub import StubBroker
from dramatiq.worker import Worker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import ConflictError, ValidationError
from app.modules.audit.models import AuditEvent
from app.modules.audit.queries import audit_events_statement
from app.modules.audit.service import ACTION_JOB_FAILED, ACTION_JOB_SUCCEEDED
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import JobStatus
from app.modules.organisations.models import Organisation

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_QUEUE = "test-jobs-db"


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


@pytest.fixture
def stub_broker_and_worker() -> Iterator[tuple[StubBroker, Worker]]:
    """A process-wide StubBroker and an in-process Worker consuming it.

    Test actors and the job service agree on the broker; the worker runs the
    sync recording tasks that prove the enqueue side of
    ``create_and_enqueue``. Sync tasks need no AsyncIO middleware.
    """
    broker = StubBroker()
    dramatiq.set_broker(broker)
    worker = Worker(broker, worker_timeout=100, worker_threads=2)
    worker.start()
    yield broker, worker
    worker.stop()
    broker.flush_all()


def _make_recording_task(received: list[tuple[str, str]]) -> Any:
    """Declare a fresh sync task that records ``(job_id, arg_count)``.

    Declared per call (after the stub broker is set) so every test gets an
    actor registered on its own broker; the actor name is unique per call
    because a broker refuses to re-declare a name. The task just records that
    it was enqueued — proving the durable-row-then-enqueue ordering — and
    never touches the database.
    """

    def _record(*args: str, job_id: str) -> None:
        received.append((job_id, str(args)))

    return dramatiq.actor(actor_name=f"record_{uuid.uuid4().hex[:8]}", queue_name=_QUEUE)(_record)


async def _create_org(session: AsyncSession) -> Organisation:
    organisation = Organisation(name=f"Jobs DB {uuid.uuid4().hex[:8]} Ltd")
    session.add(organisation)
    await session.commit()
    return organisation


async def _job_audit_events(
    session: AsyncSession, *, job_id: uuid.UUID, action: str
) -> list[AuditEvent]:
    """Return the audit rows written for one job by the given action."""
    return list(
        (
            await session.scalars(
                audit_events_statement(action=action).where(AuditEvent.resource_id == str(job_id))
            )
        ).all()
    )


async def test_create_and_enqueue_writes_row_then_enqueues(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """Acceptance §5.6: the durable queued row exists before the task enqueues."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    received: list[tuple[str, str]] = []
    try:
        async with session_factory() as session:
            organisation = await _create_org(session)
            task = _make_recording_task(received)
            job = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                actor_user_id=None,
                task=task,
            )
            assert job.status == JobStatus.QUEUED
            assert job.progress == 0
            assert job.attempt_count == 0
            assert job.job_type == "file.processing"
            assert job.input_reference == "file-1"

        # The row committed before the message ran: the recording task already
        # holds the job id that only the flushed row could have produced.
        stub_broker_and_worker[0].join(_QUEUE, timeout=10000)
        assert received == [(str(job.id), "()")]
    finally:
        await engine.dispose()


async def test_mark_running_transitions_and_counts_attempts(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """queued -> running: attempt_count increments, started_at set once."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session)
            task = _make_recording_task([])
            job = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                task=task,
            )
            stub_broker_and_worker[0].join(_QUEUE, timeout=10000)

            running = await jobs_service.mark_running(session, job_id=job.id)
            assert running.status == JobStatus.RUNNING
            assert running.attempt_count == 1
            assert running.started_at is not None
            first_started_at = running.started_at

            # A retried attempt is idempotent on the status and keeps the
            # original started_at, but counts the extra attempt.
            retried = await jobs_service.mark_running(session, job_id=job.id)
            assert retried.status == JobStatus.RUNNING
            assert retried.attempt_count == 2
            assert retried.started_at == first_started_at
    finally:
        await engine.dispose()


async def test_update_progress_and_reject_out_of_range(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session)
            task = _make_recording_task([])
            job = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                task=task,
            )
            stub_broker_and_worker[0].join(_QUEUE, timeout=10000)

            await jobs_service.mark_running(session, job_id=job.id)
            progressed = await jobs_service.update_progress(session, job_id=job.id, progress=42)
            assert progressed.progress == 42

            with pytest.raises(ValidationError):
                await jobs_service.update_progress(session, job_id=job.id, progress=101)
            with pytest.raises(ValidationError):
                await jobs_service.update_progress(session, job_id=job.id, progress=-1)
    finally:
        await engine.dispose()


async def test_succeed_marks_terminal_and_audits(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """running -> succeeded: progress 100, completed_at, result, audit row."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session)
            task = _make_recording_task([])
            job = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                actor_user_id=None,
                task=task,
            )
            stub_broker_and_worker[0].join(_QUEUE, timeout=10000)
            await jobs_service.mark_running(session, job_id=job.id)

            succeeded = await jobs_service.succeed(
                session, job_id=job.id, result_reference="file-1"
            )
            assert succeeded.status == JobStatus.SUCCEEDED
            assert succeeded.progress == 100
            assert succeeded.completed_at is not None
            assert succeeded.result_reference == "file-1"
            events = await _job_audit_events(session, job_id=job.id, action=ACTION_JOB_SUCCEEDED)
            assert len(events) == 1
            assert events[0].organisation_id == organisation.id
            assert events[0].event_metadata["job_type"] == "file.processing"
            assert events[0].event_metadata["attempt_count"] == 1
    finally:
        await engine.dispose()


async def test_succeed_is_idempotent_and_single_audit(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """A re-delivered completion does not double-transition or double-audit."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session)
            task = _make_recording_task([])
            job = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                task=task,
            )
            stub_broker_and_worker[0].join(_QUEUE, timeout=10000)
            await jobs_service.mark_running(session, job_id=job.id)

            first = await jobs_service.succeed(session, job_id=job.id)
            again = await jobs_service.succeed(session, job_id=job.id)
            assert again.status == JobStatus.SUCCEEDED
            assert again.completed_at == first.completed_at
            assert (
                len(await _job_audit_events(session, job_id=job.id, action=ACTION_JOB_SUCCEEDED))
                == 1
            )
    finally:
        await engine.dispose()


async def test_fail_records_error_and_audits(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """running -> failed: error_code/error_message recorded, audit row written."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session)
            task = _make_recording_task([])
            job = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                task=task,
            )
            stub_broker_and_worker[0].join(_QUEUE, timeout=10000)
            await jobs_service.mark_running(session, job_id=job.id)

            failed = await jobs_service.fail(
                session,
                job_id=job.id,
                error_code="upload_verification_failed",
                error_message="The stored object size does not match the declaration.",
            )
            assert failed.status == JobStatus.FAILED
            assert failed.error_code == "upload_verification_failed"
            assert failed.error_message == (
                "The stored object size does not match the declaration."
            )
            assert failed.completed_at is not None
            events = await _job_audit_events(session, job_id=job.id, action=ACTION_JOB_FAILED)
            assert len(events) == 1
            assert events[0].event_metadata["error_code"] == "upload_verification_failed"

            # Idempotent: a re-delivered failure does not double-audit.
            again = await jobs_service.fail(
                session, job_id=job.id, error_code="upload_verification_failed", error_message="x"
            )
            assert again.status == JobStatus.FAILED
            assert (
                len(await _job_audit_events(session, job_id=job.id, action=ACTION_JOB_FAILED)) == 1
            )
    finally:
        await engine.dispose()


async def test_terminal_states_are_never_rerun(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """Acceptance §5.7: no helper moves a job out of a terminal state."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session)

            # A succeeded job cannot run, progress-update, or fail.
            task = _make_recording_task([])
            succeeded = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                task=task,
            )
            stub_broker_and_worker[0].join(_QUEUE, timeout=10000)
            await jobs_service.mark_running(session, job_id=succeeded.id)
            await jobs_service.succeed(session, job_id=succeeded.id)
            with pytest.raises(ConflictError):
                await jobs_service.mark_running(session, job_id=succeeded.id)
            with pytest.raises(ConflictError):
                await jobs_service.update_progress(session, job_id=succeeded.id, progress=10)
            with pytest.raises(ConflictError):
                await jobs_service.fail(
                    session, job_id=succeeded.id, error_code="x", error_message="y"
                )

            # A failed job cannot run again or succeed.
            task = _make_recording_task([])
            failed = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                task=task,
            )
            stub_broker_and_worker[0].join(_QUEUE, timeout=10000)
            await jobs_service.mark_running(session, job_id=failed.id)
            await jobs_service.fail(session, job_id=failed.id, error_code="boom", error_message="z")
            with pytest.raises(ConflictError):
                await jobs_service.mark_running(session, job_id=failed.id)
            with pytest.raises(ConflictError):
                await jobs_service.succeed(session, job_id=failed.id)
    finally:
        await engine.dispose()
