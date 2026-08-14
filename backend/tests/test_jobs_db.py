"""Real-database integration tests for the durable job service (Scope §6.4).

The retry-mechanics tests in ``test_jobs.py`` never execute SQL, so the job
lifecycle could silently regress at the query and constraint level. These
tests run the real migration and the real service against a reachable
PostgreSQL (same skip pattern as ``test_records_db.py``: migrated to head up
front, reverted to base afterwards). The broker is an in-process StubBroker
with a sync recording task, so enqueueing is proven without Redis; the full
worker round trip against a real Redis broker is ``test_jobs_broker.py``.

Acceptance §5.6/§5.7 and the durable delivery plan's P2 ownership contract are
proven here: the durable row moves queued -> running -> succeeded (or ->
failed with ``error_code`` / ``error_message``), ``attempt_count`` and
``started_at``/``completed_at`` are maintained, ``succeed``/``fail`` are
idempotent, terminal states are never re-run, and the ``job.succeeded`` /
``job.failed`` audit rows are written in the same transaction as the
transition. The atomic claim (``claim_dispatch``) assigns a dispatch identity
to legacy rows, defers duplicates with a live lease, permits take-over of an
expired lease, and owner-checked mutations refuse a stale attempt; the
retries-exhausted finalizer settles only a dispatch no live attempt owns.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dramatiq
import pytest
from alembic import command
from alembic.config import Config
from dramatiq.brokers.stub import StubBroker
from dramatiq.worker import Worker
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import ConflictError, ValidationError
from app.modules.audit.models import AuditEvent
from app.modules.audit.queries import audit_events_statement
from app.modules.audit.service import ACTION_JOB_FAILED, ACTION_JOB_SUCCEEDED
from app.modules.jobs import execution as jobs_execution
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatus
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


async def test_claim_dispatch_transitions_and_counts_attempts(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """queued -> running: claim increments attempts, sets lease and dispatch.

    The first claim assigns a dispatch identity to the durable row and sets
    the execution lease; a transient release returns the row to ``queued`` so
    the retry of the same dispatch can claim it again with a fresh lease,
    keeping the original ``started_at`` (plan P2).
    """
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

            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert claim.outcome == jobs_service.ClaimOutcome.CLAIMED
            assert claim.taken_over is False
            assert claim.dispatch_id is not None
            running = claim.job
            assert running is not None
            assert running.status == JobStatus.RUNNING
            assert running.attempt_count == 1
            assert running.started_at is not None
            assert running.execution_lease_expires_at is not None
            first_started_at = running.started_at
            first_dispatch = claim.dispatch_id
            first_token = claim.owner_token
            assert first_token is not None

            # A transient release returns the row to queued and clears the
            # lease, so the retry of the same dispatch can claim it again.
            released = await jobs_service.release_dispatch(
                session, job_id=job.id, owner_token=first_token
            )
            assert released is True
            row_after_release = await session.get(Job, job.id)
            assert row_after_release is not None
            assert row_after_release.status == JobStatus.QUEUED
            assert row_after_release.execution_lease_expires_at is None

            retried = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert retried.outcome == jobs_service.ClaimOutcome.CLAIMED
            assert retried.dispatch_id == first_dispatch
            # The retry rotates the ownership token: the released attempt's
            # credential is superseded even though the dispatch is unchanged.
            assert retried.owner_token is not None
            assert retried.owner_token != first_token
            assert retried.job is not None
            assert retried.job.attempt_count == 2
            assert retried.job.started_at == first_started_at
    finally:
        await engine.dispose()


async def test_duplicate_claim_is_deferred_while_lease_live(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """A concurrent duplicate is deferred, never executed concurrently (P2).

    The second claim sees the first attempt's live lease and returns
    ``DEFERRED`` with the lease bound; the row keeps its owner's status,
    attempt count and dispatch id untouched.
    """
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

            first = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert first.outcome == jobs_service.ClaimOutcome.CLAIMED

            duplicate = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert duplicate.outcome == jobs_service.ClaimOutcome.DEFERRED
            assert duplicate.deferred_until is not None
            assert duplicate.dispatch_id is None  # nothing was claimed

            # The owner's row is untouched by the deferred duplicate.
            row = await session.get(Job, job.id)
            assert row is not None
            assert row.status == JobStatus.RUNNING
            assert row.attempt_count == 1
            assert row.dispatch_id == first.dispatch_id
    finally:
        await engine.dispose()


async def test_expired_lease_can_be_taken_over(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """An expired lease lets a retry/duplicate take the dead attempt over (P2).

    The takeover keeps the dispatch identity (the delivery did not change),
    rotates the attempt-distinguishing owner token, increments the attempt
    counter and sets a fresh lease.
    """
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

            first = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert first.outcome == jobs_service.ClaimOutcome.CLAIMED
            first_token = first.owner_token
            assert first_token is not None
            # Simulate a dead worker: the lease expires while the row stays
            # ``running``.
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

            takeover = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert takeover.outcome == jobs_service.ClaimOutcome.CLAIMED
            assert takeover.taken_over is True
            assert takeover.dispatch_id == first.dispatch_id
            assert takeover.owner_token is not None
            assert takeover.owner_token != first_token
            assert takeover.job is not None
            assert takeover.job.attempt_count == 2
            assert takeover.job.execution_lease_expires_at is not None
    finally:
        await engine.dispose()


async def test_takeover_supersedes_old_attempt_owner(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """A dead worker cannot mutate the row after a real takeover (plan P2, AC5).

    The takeover rotates the owner token while keeping the dispatch identity,
    so the expired worker's captured credential is superseded: every stale
    mutation it attempts with the first attempt's token — progress, success,
    failure and release — raises :class:`StaleDispatchError` and the row stays
    the new owner's.
    """
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

            first = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert first.outcome == jobs_service.ClaimOutcome.CLAIMED
            stale_token = first.owner_token
            assert stale_token is not None
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

            takeover = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert takeover.outcome == jobs_service.ClaimOutcome.CLAIMED
            assert takeover.taken_over is True

            with pytest.raises(jobs_service.StaleDispatchError):
                await jobs_service.update_progress(
                    session, job_id=job.id, progress=42, owner_token=stale_token
                )
            with pytest.raises(jobs_service.StaleDispatchError):
                await jobs_service.succeed(
                    session, job_id=job.id, owner_token=stale_token
                )
            with pytest.raises(jobs_service.StaleDispatchError):
                await jobs_service.fail(
                    session,
                    job_id=job.id,
                    error_code="boom",
                    error_message="stale takeover",
                    owner_token=stale_token,
                )
            with pytest.raises(jobs_service.StaleDispatchError):
                await jobs_service.release_dispatch(
                    session, job_id=job.id, owner_token=stale_token
                )

            row = await session.get(Job, job.id)
            assert row is not None
            assert row.status == JobStatus.RUNNING
            assert row.dispatch_id == takeover.dispatch_id
            assert row.owner_token == takeover.owner_token
            assert row.attempt_count == 2
            assert row.progress == 0
            assert row.error_code is None
    finally:
        await engine.dispose()


async def test_claim_assigns_dispatch_id_to_legacy_row(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """A legacy row without a dispatch id receives one atomically (P2).

    Old one-argument broker messages still work: the first claim names the
    dispatch so ownership checks apply from there on.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session)
            legacy = Job(
                organisation_id=organisation.id,
                job_type="file.processing",
                status=JobStatus.QUEUED,
                progress=0,
                input_reference="file-legacy",
                dispatch_id=None,
            )
            session.add(legacy)
            await session.commit()

            claim = await jobs_service.claim_dispatch(session, job_id=legacy.id)
            assert claim.outcome == jobs_service.ClaimOutcome.CLAIMED
            assert claim.dispatch_id is not None
            assert claim.job is not None
            assert claim.job.dispatch_id == claim.dispatch_id
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

            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            owner = claim.owner_token
            progressed = await jobs_service.update_progress(
                session,
                job_id=job.id,
                progress=42,
                owner_token=owner,
            )
            assert progressed.progress == 42
            # Progress renews the execution lease (plan P2).
            assert progressed.execution_lease_expires_at is not None

            with pytest.raises(ValidationError):
                await jobs_service.update_progress(
                    session, job_id=job.id, progress=101, owner_token=owner
                )
            with pytest.raises(ValidationError):
                await jobs_service.update_progress(
                    session, job_id=job.id, progress=-1, owner_token=owner
                )
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
            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            owner = claim.owner_token

            succeeded = await jobs_service.succeed(
                session,
                job_id=job.id,
                result_reference="file-1",
                owner_token=owner,
            )
            assert succeeded.status == JobStatus.SUCCEEDED
            assert succeeded.progress == 100
            assert succeeded.completed_at is not None
            assert succeeded.result_reference == "file-1"
            # Terminal settlement clears the execution lease (plan P2).
            assert succeeded.execution_lease_expires_at is None
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
            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            owner = claim.owner_token

            first = await jobs_service.succeed(
                session, job_id=job.id, owner_token=owner
            )
            again = await jobs_service.succeed(
                session, job_id=job.id, owner_token=owner
            )
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
            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            owner = claim.owner_token

            failed = await jobs_service.fail(
                session,
                job_id=job.id,
                error_code="upload_verification_failed",
                error_message="The stored object size does not match the declaration.",
                owner_token=owner,
            )
            assert failed.status == JobStatus.FAILED
            assert failed.error_code == "upload_verification_failed"
            assert failed.error_message == (
                "The stored object size does not match the declaration."
            )
            assert failed.completed_at is not None
            # Terminal settlement clears the execution lease (plan P2).
            assert failed.execution_lease_expires_at is None
            events = await _job_audit_events(session, job_id=job.id, action=ACTION_JOB_FAILED)
            assert len(events) == 1
            assert events[0].event_metadata["error_code"] == "upload_verification_failed"

            # Idempotent: a re-delivered failure does not double-audit.
            again = await jobs_service.fail(
                session,
                job_id=job.id,
                error_code="upload_verification_failed",
                error_message="x",
                owner_token=owner,
            )
            assert again.status == JobStatus.FAILED
            assert (
                len(await _job_audit_events(session, job_id=job.id, action=ACTION_JOB_FAILED)) == 1
            )
    finally:
        await engine.dispose()


async def test_stale_owner_mutations_are_rejected(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """A superseded attempt cannot mutate over a newer owner (plan P2, AC5).

    Once a newer owner replaces the one the attempt captured (a real claim
    rotates both the dispatch id and the attempt token), every owner-checked
    mutation raises :class:`StaleDispatchError` instead of overwriting the
    row.
    """
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
            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            stale_token = claim.owner_token
            assert stale_token is not None

            # Simulate reconciliation (plan P4) replacing the dispatch while
            # the stale attempt still holds its captured token: a newer claim
            # rotates both the dispatch identity and the ownership token.
            newer_dispatch = uuid.uuid4()
            newer_token = uuid.uuid4()
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(dispatch_id=newer_dispatch, owner_token=newer_token)
            )
            await session.commit()

            with pytest.raises(jobs_service.StaleDispatchError):
                await jobs_service.update_progress(
                    session, job_id=job.id, progress=50, owner_token=stale_token
                )
            with pytest.raises(jobs_service.StaleDispatchError):
                await jobs_service.succeed(
                    session, job_id=job.id, owner_token=stale_token
                )
            with pytest.raises(jobs_service.StaleDispatchError):
                await jobs_service.fail(
                    session,
                    job_id=job.id,
                    error_code="boom",
                    error_message="stale",
                    owner_token=stale_token,
                )
            with pytest.raises(jobs_service.StaleDispatchError):
                await jobs_service.release_dispatch(
                    session, job_id=job.id, owner_token=stale_token
                )

            row = await session.get(Job, job.id)
            assert row is not None
            assert row.status == JobStatus.RUNNING
            assert row.dispatch_id == newer_dispatch
            assert row.owner_token == newer_token
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
            claim = await jobs_service.claim_dispatch(session, job_id=succeeded.id)
            owner = claim.owner_token
            await jobs_service.succeed(session, job_id=succeeded.id, owner_token=owner)
            # A late duplicate claim sees the terminal row as stale (P2).
            stale = await jobs_service.claim_dispatch(session, job_id=succeeded.id)
            assert stale.outcome == jobs_service.ClaimOutcome.STALE
            with pytest.raises(ConflictError):
                await jobs_service.update_progress(
                    session, job_id=succeeded.id, progress=10, owner_token=owner
                )
            with pytest.raises(ConflictError):
                await jobs_service.fail(
                    session,
                    job_id=succeeded.id,
                    error_code="x",
                    error_message="y",
                    owner_token=owner,
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
            claim = await jobs_service.claim_dispatch(session, job_id=failed.id)
            owner = claim.owner_token
            await jobs_service.fail(
                session, job_id=failed.id, error_code="boom", error_message="z", owner_token=owner
            )
            stale = await jobs_service.claim_dispatch(session, job_id=failed.id)
            assert stale.outcome == jobs_service.ClaimOutcome.STALE
            with pytest.raises(ConflictError):
                await jobs_service.succeed(
                    session, job_id=failed.id, owner_token=owner
                )
    finally:
        await engine.dispose()


async def test_retries_exhausted_settles_owned_queued_job(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """The finalizer settles the owned attempt once retries run out (P2).

    A transient failure releases the owned attempt back to ``queued``; the
    exhausted message then settles that attempt failed with the exhausted
    error code and one audit row — the job never sits in ``running`` forever.
    The settlement passes the dispatch id and owner token the message stamped
    at its last claim, matching the row's current attempt.
    """
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
            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert claim.dispatch_id is not None
            assert claim.owner_token is not None
            await jobs_service.release_dispatch(
                session, job_id=job.id, owner_token=claim.owner_token
            )

            settled = await jobs_service.settle_after_retries_exhausted(
                session,
                job_id=job.id,
                exhausted_dispatch_id=claim.dispatch_id,
                exhausted_owner_token=claim.owner_token,
            )
            assert settled is not None
            assert settled.status == JobStatus.FAILED
            assert settled.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED
            assert settled.error_message == jobs_service.ERROR_MESSAGE_RETRIES_EXHAUSTED
            assert settled.completed_at is not None
            assert settled.execution_lease_expires_at is None
            assert (
                len(
                    await _job_audit_events(
                        session, job_id=job.id, action=ACTION_JOB_FAILED
                    )
                )
                == 1
            )
    finally:
        await engine.dispose()


async def test_retries_exhausted_is_stale_for_terminal_and_leased(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """The finalizer never fails a terminal or actively leased job (P2).

    A deferred duplicate that exhausts its retries while the winning attempt
    still holds the lease — or after the job already finished — is a stale
    no-op: ``settle_after_retries_exhausted`` returns ``None`` and leaves the
    row untouched.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session)

            # Case 1: the winner completed; the exhausted duplicate is stale.
            # The duplicate never claimed, so its message carries no stamp;
            # both guards (terminal state, and no matching owner credential)
            # make it a no-op.
            task = _make_recording_task([])
            finished = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                task=task,
            )
            stub_broker_and_worker[0].join(_QUEUE, timeout=10000)
            claim = await jobs_service.claim_dispatch(session, job_id=finished.id)
            await jobs_service.succeed(
                session, job_id=finished.id, owner_token=claim.owner_token
            )
            assert (
                await jobs_service.settle_after_retries_exhausted(
                    session, job_id=finished.id
                )
                is None
            )
            finished_row = await session.get(Job, finished.id)
            assert finished_row is not None
            assert finished_row.status == JobStatus.SUCCEEDED

            # Case 2: a live attempt still owns the lease; the exhausted
            # duplicate must not fail it.
            task = _make_recording_task([])
            leased = await jobs_service.create_and_enqueue(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
                task=task,
            )
            stub_broker_and_worker[0].join(_QUEUE, timeout=10000)
            claim = await jobs_service.claim_dispatch(session, job_id=leased.id)
            assert claim.outcome == jobs_service.ClaimOutcome.CLAIMED
            assert (
                await jobs_service.settle_after_retries_exhausted(
                    session, job_id=leased.id
                )
                is None
            )
            leased_row = await session.get(Job, leased.id)
            assert leased_row is not None
            assert leased_row.status == JobStatus.RUNNING
            assert leased_row.error_code is None
    finally:
        await engine.dispose()


async def test_retries_exhausted_superseded_dispatch_is_stale(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """A superseded exhausted message cannot fail a newer queued dispatch (P2).

    A delayed exhausted message for dispatch A arrives after dispatch B became
    current (B queued, no live lease): ``settle_after_retries_exhausted``
    correlates the message's stamped dispatch (A) against the row's current
    dispatch (B), returns ``None`` and leaves B queued instead of failing it
    with the exhausted error.
    """
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

            # Dispatch A claims and then releases (transient), so its retries
            # exhaust while the row is queued under dispatch A.
            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert claim.dispatch_id is not None
            assert claim.owner_token is not None
            exhausted_dispatch = claim.dispatch_id
            exhausted_token = claim.owner_token
            await jobs_service.release_dispatch(
                session, job_id=job.id, owner_token=claim.owner_token
            )

            # Dispatch B becomes current (reconciliation, plan P4): a newer
            # owner takes the row while A's exhausted message is still in
            # flight. B is queued and has no live lease, so only the attempt
            # correlation can protect it.
            newer_dispatch = uuid.uuid4()
            newer_token = uuid.uuid4()
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(dispatch_id=newer_dispatch, owner_token=newer_token)
            )
            await session.commit()

            assert (
                await jobs_service.settle_after_retries_exhausted(
                    session,
                    job_id=job.id,
                    exhausted_dispatch_id=exhausted_dispatch,
                    exhausted_owner_token=exhausted_token,
                )
                is None
            )
            row = await session.get(Job, job.id)
            assert row is not None
            assert row.status == JobStatus.QUEUED
            assert row.dispatch_id == newer_dispatch
            assert row.error_code is None

            # The same message correlated against its own (still-current)
            # attempt settles normally — proving the correlation, not the
            # lease guard, produced the stale result above.
            settled = await jobs_service.settle_after_retries_exhausted(
                session,
                job_id=job.id,
                exhausted_dispatch_id=newer_dispatch,
                exhausted_owner_token=newer_token,
            )
            assert settled is not None
            assert settled.status == JobStatus.FAILED
            assert settled.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED
    finally:
        await engine.dispose()


async def test_retries_exhausted_rotated_token_same_dispatch_is_stale(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """A retry that rotated the token protects the newer queued attempt (P2).

    The claim keeps the dispatch identity while rotating the owner token on
    every re-claim (an outbox retry of the *same* dispatch), so a delayed
    exhausted message for attempt A (same dispatch, old token) arriving after
    attempt B re-claimed the same dispatch and released back to ``queued``
    must not fail B. Only the attempt correlation — not the dispatch — can
    distinguish the two, exactly the race the dispatch-only correlation left
    open.
    """
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

            # Attempt A claims dispatch D (token T1) and releases (transient).
            claim_a = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert claim_a.dispatch_id is not None
            assert claim_a.owner_token is not None
            await jobs_service.release_dispatch(
                session, job_id=job.id, owner_token=claim_a.owner_token
            )

            # Attempt B re-claims the SAME dispatch (the retry of the same
            # outbox event): the token rotates, the identity is retained.
            claim_b = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert claim_b.dispatch_id == claim_a.dispatch_id
            assert claim_b.owner_token != claim_a.owner_token
            assert claim_b.owner_token is not None
            await jobs_service.release_dispatch(
                session, job_id=job.id, owner_token=claim_b.owner_token
            )

            # A's exhausted finalizer arrives while B is queued with no live
            # lease: same dispatch, superseded token -> stale no-op.
            assert (
                await jobs_service.settle_after_retries_exhausted(
                    session,
                    job_id=job.id,
                    exhausted_dispatch_id=claim_a.dispatch_id,
                    exhausted_owner_token=claim_a.owner_token,
                )
                is None
            )
            row = await session.get(Job, job.id)
            assert row is not None
            assert row.status == JobStatus.QUEUED
            assert row.error_code is None

            # B's own exhausted finalizer (its stamped token) settles normally.
            settled = await jobs_service.settle_after_retries_exhausted(
                session,
                job_id=job.id,
                exhausted_dispatch_id=claim_b.dispatch_id,
                exhausted_owner_token=claim_b.owner_token,
            )
            assert settled is not None
            assert settled.status == JobStatus.FAILED
            assert settled.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED
    finally:
        await engine.dispose()


async def test_retries_exhausted_never_claimed_duplicate_is_stale(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """A duplicate that exhausted without claiming cannot fail the owner (P2).

    A duplicate deferred on every attempt never claims, so its exhausted
    message carries no stamp. If the live owner releases back to ``queued``
    just before that finalizer runs, the stamp-less message must be a stale
    no-op: the row now carries an owner credential (the released attempt), and
    only the attempt that stamped it may settle it. This closes the second
    half of the dispatch-only race — a never-claimed duplicate falling through
    to the legacy ``None`` path and failing the row around an owner release.
    """
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

            # The live owner claims, then releases back to queued (transient)
            # just before the never-claimed duplicate's finalizer runs.
            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert claim.owner_token is not None
            await jobs_service.release_dispatch(
                session, job_id=job.id, owner_token=claim.owner_token
            )

            # The never-claimed duplicate's exhausted message has no stamp.
            assert (
                await jobs_service.settle_after_retries_exhausted(
                    session, job_id=job.id
                )
                is None
            )
            row = await session.get(Job, job.id)
            assert row is not None
            assert row.status == JobStatus.QUEUED
            assert row.error_code is None
    finally:
        await engine.dispose()


async def test_retries_exhausted_legacy_unclaimed_row_settles(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """Explicit legacy behaviour: a stamp-less message settles a legacy row (P2).

    A pre-ownership row (never claimed, so it carries no dispatch id or owner
    credential) settled by a stamp-less exhausted message from an old release
    follows the legacy contract: the finalizer fails the current dispatch
    without an ownership credential, so jobs created before P2 still record
    their exhausted failure.
    """
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

            # The row has never been claimed: no dispatch id, no owner token.
            row = await session.get(Job, job.id)
            assert row is not None
            assert row.dispatch_id is None
            assert row.owner_token is None
            assert row.status == JobStatus.QUEUED

            settled = await jobs_service.settle_after_retries_exhausted(
                session, job_id=job.id
            )
            assert settled is not None
            assert settled.status == JobStatus.FAILED
            assert settled.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED
            assert settled.error_message == jobs_service.ERROR_MESSAGE_RETRIES_EXHAUSTED
    finally:
        await engine.dispose()


# --- Shared execution wrapper (durable delivery plan P2) ---


async def test_wrapper_releases_owned_attempt_on_transient_failure(
    migrated_database: str,
    stub_broker_and_worker: tuple[StubBroker, Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient failure releases the owned attempt back to queued (P2).

    The wrapper claims, the handler raises, and the wrapper owner-checked
    releases the attempt before the exception propagates, so the retry of the
    same dispatch can re-claim it.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(jobs_execution, "async_session_factory", session_factory)
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
            job_id = job.id

        async def _boom(
            context: jobs_execution.DurableJobContext, session: AsyncSession
        ) -> None:
            raise RuntimeError("storage temporarily unreachable")

        with pytest.raises(RuntimeError, match="storage temporarily unreachable"):
            await jobs_execution.run_claimed(job_id=job_id, handler=_boom)

        async with session_factory() as session:
            row = await session.get(Job, job_id)
            assert row is not None
            assert row.status == JobStatus.QUEUED
            assert row.execution_lease_expires_at is None
            assert row.dispatch_id is not None
            assert row.attempt_count == 1
    finally:
        await engine.dispose()


async def test_wrapper_defers_duplicate_until_winner_completes(
    migrated_database: str,
    stub_broker_and_worker: tuple[StubBroker, Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate waits for a live lease and then no-ops (plan P2, AC4).

    The losing copy never runs the business handler: once the winner finishes,
    the deferred duplicate's next claim sees the terminal row and returns.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(jobs_execution, "async_session_factory", session_factory)
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
            # The winner claims and holds a lease ~2 seconds into the future.
            winner = await jobs_service.claim_dispatch(session, job_id=job.id)
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(
                    execution_lease_expires_at=datetime.now(UTC) + timedelta(seconds=2)
                )
            )
            await session.commit()
            job_id = job.id
            winner_owner = winner.owner_token
            assert winner_owner is not None

        async def _winner_completes() -> None:
            await asyncio.sleep(0.5)
            async with session_factory() as session:
                await jobs_service.succeed(
                    session, job_id=job_id, owner_token=winner_owner
                )

        duplicate_ran: list[str] = []

        async def _duplicate_attempt(
            context: jobs_execution.DurableJobContext, session: AsyncSession
        ) -> None:
            duplicate_ran.append("ran")

        winner_task = asyncio.create_task(_winner_completes())
        await jobs_execution.run_claimed(job_id=job_id, handler=_duplicate_attempt)
        await winner_task

        assert duplicate_ran == []
        async with session_factory() as session:
            row = await session.get(Job, job_id)
            assert row is not None
            assert row.status == JobStatus.SUCCEEDED
            assert row.attempt_count == 1  # only the winner ever claimed
    finally:
        await engine.dispose()


async def test_wrapper_treats_stale_settlement_as_noop(
    migrated_database: str,
    stub_broker_and_worker: tuple[StubBroker, Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale attempt settling a newer owner is a no-op, never an overwrite (P2).

    The handler's owner-checked mutation raises :class:`StaleDispatchError`;
    the wrapper acknowledges the message and leaves the row untouched.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(jobs_execution, "async_session_factory", session_factory)
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
            job_id = job.id

        async def _stale_settlement(
            context: jobs_execution.DurableJobContext, session: AsyncSession
        ) -> None:
            raise jobs_service.StaleDispatchError(
                job_id, uuid.uuid4(), context.owner_token
            )

        await jobs_execution.run_claimed(job_id=job_id, handler=_stale_settlement)

        async with session_factory() as session:
            row = await session.get(Job, job_id)
            assert row is not None
            assert row.status == JobStatus.RUNNING
            assert row.error_code is None
    finally:
        await engine.dispose()


async def test_wrapper_raises_deferred_error_beyond_wait_budget(
    migrated_database: str,
    stub_broker_and_worker: tuple[StubBroker, Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease far beyond the in-process budget defers via the broker (P2).

    The wrapper raises the transient :class:`DispatchDeferredError` for the
    Retries middleware instead of blocking the worker for the full lease, and
    the live owner's row is untouched.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(jobs_execution, "async_session_factory", session_factory)
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
            await jobs_service.claim_dispatch(session, job_id=job.id)
            # A lease that cannot be waited out in-process.
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(
                    execution_lease_expires_at=datetime.now(UTC) + timedelta(hours=1)
                )
            )
            await session.commit()
            job_id = job.id

        async def _must_not_run(
            context: jobs_execution.DurableJobContext, session: AsyncSession
        ) -> None:
            raise AssertionError("a deferred duplicate must not run business work")

        with pytest.raises(jobs_service.DispatchDeferredError):
            await jobs_execution.run_claimed(job_id=job_id, handler=_must_not_run)

        async with session_factory() as session:
            row = await session.get(Job, job_id)
            assert row is not None
            assert row.status == JobStatus.RUNNING
            assert row.attempt_count == 1
    finally:
        await engine.dispose()


async def test_simultaneous_claims_serialize_to_one_owner(
    migrated_database: str, stub_broker_and_worker: tuple[StubBroker, Worker]
) -> None:
    """Two concurrent claims cannot both own the dispatch (plan P2, AC4).

    ``FOR UPDATE`` serialises the racing claims: exactly one wins the dispatch
    and the other is deferred with the winner's lease bound.
    """
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
            job_id = job.id

        outcomes = await asyncio.gather(
            *[
                _claim_in_own_session(session_factory, job_id)
                for _ in range(2)
            ]
        )
        claimed = [result for result in outcomes if result is not None]
        assert len(claimed) == 1
    finally:
        await engine.dispose()


async def _claim_in_own_session(session_factory: Any, job_id: uuid.UUID) -> uuid.UUID | None:
    """Claim ``job_id`` in a private session; return the dispatch id when owned."""
    async with session_factory() as session:
        result = await jobs_service.claim_dispatch(session, job_id=job_id)
        if result.outcome is jobs_service.ClaimOutcome.CLAIMED:
            assert result.dispatch_id is not None
            return result.dispatch_id
        if result.outcome is jobs_service.ClaimOutcome.DEFERRED:
            assert result.deferred_until is not None
        return None
