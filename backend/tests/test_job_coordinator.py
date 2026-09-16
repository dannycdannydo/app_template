"""Real PostgreSQL + Redis coordinator tests (durable delivery plan P3).

The coordinator is the only production component that turns outbox rows into
Dramatiq messages. These tests run the real claim/publish/settle machinery
against a reachable PostgreSQL and a namespaced Redis broker (skip when either
is missing, the same pattern as ``test_jobs_broker.py``). They prove:

- two coordinators claim disjoint batches under ``FOR UPDATE SKIP LOCKED``;
- one cycle publishes a dispatch event and settles it ``published``, with the
  broker message carrying only the job id;
- a maintenance event publishes its argument-free message;
- a genuinely unreachable Redis broker releases the claim and a later cycle
  through a healthy broker recovers it (real broker-down retry and recovery
  publication);
- a released row carries a durable jittered retry time, so a second
  coordinator cannot reclaim it before ``available_at`` but can after it;
- a permanently invalid event (unknown contract, missing job aggregate)
  becomes ``dead`` with a bounded error and is never retried;
- a crash after the broker accepted the message republishes it once the claim
  lease expires (the documented at-least-once duplicate window), leaving the
  row ``published``; and
- settlement is a single owner-checked conditional ``UPDATE``: a stale
  publisher whose claim was taken over cannot overwrite the newer owner's
  state;
- an in-flight shutdown mid-batch claims no new work, leaves unprocessed
  intent pending/publishing, and a fresh coordinator recovers everything.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast

import dramatiq
import pytest
from alembic import command
from alembic.config import Config
from dramatiq.brokers.redis import RedisBroker
from dramatiq.worker import Worker
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from structlog.testing import capture_logs

from app.broker import worker_middleware
from app.job_coordinator.loop import (
    CycleStats,
    claim_due_events,
    cycle_log_due,
    publish_event,
    reclaim_stale_claims,
    run_coordinator,
    run_cycle,
    settle_published,
)
from app.job_coordinator.reconciliation import (
    reconcile_queued_jobs,
    reconcile_running_jobs,
    schedule_maintenance_events,
)
from app.job_coordinator.registry import DispatchRegistry
from app.modules.audit.models import AuditEvent
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobAttemptStatus, JobStatus
from app.modules.jobs.queries import job_attempt_history_statement
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationDelivery, NotificationDeliveryStatus
from app.modules.organisations.models import Organisation
from app.modules.outbox.contracts import (
    EVENT_TYPE_AI_RETENTION,
    EVENT_TYPE_JOB_DISPATCH,
    EVENT_TYPE_TRANSFER_RECONCILE,
    EVENT_VERSION_JOB_DISPATCH,
    OutboxContractError,
)
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus
from app.modules.outbox.queries import due_outbox_events_statement
from app.modules.outbox.service import create_schedule_event
from app.modules.users.models import User

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_QUEUE = "test-job-coordinator"
_MAINTENANCE_QUEUE = "test-job-coordinator-maintenance"


def _probe(url: str) -> bool:
    """Probe one external service with a short async connect.

    The connect itself is bounded (3 s) so a missing or restricted service
    produces a deterministic skip instead of an indefinite stall (review
    should-fix: the PostgreSQL connect attempt previously hung this file).
    """

    async def _run() -> bool:
        if url.startswith("postgres"):
            engine = create_async_engine(url, poolclass=NullPool, connect_args={"timeout": 3})
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                return True
            except Exception:
                return False
            finally:
                await engine.dispose()
        from redis.asyncio import Redis

        client = cast(Any, Redis).from_url(
            url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3
        )
        try:
            return bool(await client.ping())
        except Exception:
            return False
        finally:
            await client.aclose()

    return asyncio.run(_run())


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
    database_url = os.environ["DATABASE_URL"]
    if not _probe(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")
    if not _probe(_REDIS_URL):
        pytest.skip("no reachable Redis at REDIS_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


@pytest.fixture
async def broker_and_recording() -> AsyncIterator[
    tuple[RedisBroker, Worker, DispatchRegistry, list[str]]
]:
    """A namespaced Redis broker, a real worker and a recording registry.

    The recording actors run synchronously on the worker's thread pool, so a
    message send is observable as an appended ``(queue, job_id)`` record. The
    registry maps the durable job type and the maintenance event to these
    test-bound actors, exactly the seam production uses with the real actors.
    """
    from app.broker import worker_middleware

    previous_broker = dramatiq.get_broker()
    broker = RedisBroker(
        url=_REDIS_URL,
        namespace=f"coordinator-test-{uuid.uuid4().hex[:8]}",
        middleware=worker_middleware(),
        maintenance_chance=1_000_000,
    )
    dramatiq.set_broker(broker)
    received: list[str] = []

    def _record_job(job_id: str) -> None:
        received.append(job_id)

    def _record_maintenance() -> None:
        received.append("maintenance")

    job_actor = dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(_record_job)
    maintenance_actor = dramatiq.actor(queue_name=_MAINTENANCE_QUEUE)(_record_maintenance)
    registry = DispatchRegistry(
        job_actors={"file.processing": job_actor},
        maintenance_actors={EVENT_TYPE_AI_RETENTION: maintenance_actor},
    )
    worker = Worker(broker, worker_timeout=100, worker_threads=2)
    worker.start()
    yield broker, worker, registry, received
    worker.stop()
    broker.close()
    dramatiq.set_broker(previous_broker)


def _session_factory(database_url: str) -> Any:
    """A NullPool session factory safe to share across event loops."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _create_org(session: AsyncSession) -> Organisation:
    organisation = Organisation(name=f"Coordinator {uuid.uuid4().hex[:8]} Ltd")
    session.add(organisation)
    await session.commit()
    return organisation


async def _schedule_job(
    session_factory: Any, *, organisation_id: uuid.UUID
) -> tuple[Job, uuid.UUID]:
    """Schedule one durable job; return (job, outbox event id)."""
    async with session_factory() as session:
        job = await jobs_service.schedule_job(
            session,
            organisation_id=organisation_id,
            job_type="file.processing",
            input_reference="file-1",
        )
        assert job.dispatch_id is not None
        return job, job.dispatch_id


async def _outbox_row(session_factory: Any, event_id: uuid.UUID) -> OutboxEvent:
    async with session_factory() as session:
        row = await session.get(OutboxEvent, event_id)
        assert row is not None
        return row


def _complete_test_registry(received: list[str]) -> DispatchRegistry:
    """A registry covering the full plan catalogue for ``run_coordinator``.

    ``run_coordinator`` validates completeness at startup (plan P3), so tests
    that start the real loop must register all three durable job types and
    both maintenance events. The recording actors bind to whatever broker is
    process-global (the fixture's namespaced Redis broker).
    """

    def _make_job_actor(name: str):
        def _record_job(job_id: str) -> None:
            received.append(job_id)

        return dramatiq.actor(actor_name=name, queue_name=_QUEUE, **jobs_service.retry_policy())(
            _record_job
        )

    def _make_maintenance_actor(name: str):
        def _record_maintenance() -> None:
            received.append("maintenance")

        return dramatiq.actor(actor_name=name, queue_name=_MAINTENANCE_QUEUE)(_record_maintenance)

    return DispatchRegistry(
        job_actors={
            "file.processing": _make_job_actor("coord_file"),
            "notification.email": _make_job_actor("coord_email"),
            "ai.execute": _make_job_actor("coord_ai"),
        },
        maintenance_actors={
            "ai.retention": _make_maintenance_actor("coord_retention"),
            "ai.transfer_reconcile": _make_maintenance_actor("coord_transfer"),
        },
    )


def _closed_redis_url() -> str:
    """A Redis URL for a port nothing is listening on.

    Binds an ephemeral port, closes it, and reuses the port: a genuine
    connection-refused seam for the real broker-down test (no fake target).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"redis://127.0.0.1:{port}/0"


def _actor_on_broker(broker: RedisBroker, fn: Any, *, actor_name: str, queue_name: str) -> Any:
    """Declare an actor on a specific broker (temporarily process-global)."""
    previous = dramatiq.get_broker()
    dramatiq.set_broker(broker)
    try:
        return dramatiq.actor(actor_name=actor_name, queue_name=queue_name)(fn)
    finally:
        dramatiq.set_broker(previous)


def _slow_test_registry(received: list[str], *, send_delay: float) -> DispatchRegistry:
    """A full-catalogue registry whose publishes block (interruption tests).

    ``run_coordinator`` validates completeness at startup (plan P3), so all
    five targets are present. Job targets carry the shared retry-policy
    marker the validation requires, and their ``send`` blocks the coordinator
    loop for ``send_delay`` seconds so a shutdown can land while a multi-row
    batch is still in flight. Messages are recorded synchronously so the test
    can observe exactly which rows were published before the stop.
    """

    class _SlowJobTarget:
        options: ClassVar[dict[str, str]] = {
            "on_retry_exhausted": jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR
        }

        def send(self, job_id: str) -> None:
            time.sleep(send_delay)
            received.append(job_id)

    class _MaintenanceTarget:
        def send(self) -> None:
            time.sleep(send_delay)
            received.append("maintenance")

    return DispatchRegistry(
        job_actors={
            "file.processing": _SlowJobTarget(),
            "notification.email": _SlowJobTarget(),
            "ai.execute": _SlowJobTarget(),
        },
        maintenance_actors={
            "ai.retention": _MaintenanceTarget(),
            "ai.transfer_reconcile": _MaintenanceTarget(),
        },
    )


async def _wait_for_received(received: list[str], count: int, *, timeout: float = 10.0) -> None:
    """Wait until the recording worker has handled ``count`` messages."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(received) >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"recording worker handled {len(received)} of {count} messages")


def _zero_jitter(_lo: float, _hi: float) -> float:
    """Deterministic jitter returning 0.0 (immediate retry)."""
    return 0.0


def _midpoint_jitter(lo: float, hi: float) -> float:
    """Deterministic jitter returning the midpoint of the delay range."""
    return (lo + hi) / 2


def _max_jitter(_lo: float, hi: float) -> float:
    """Deterministic jitter returning the upper bound of the delay range."""
    return hi


async def test_reconciliation_creates_one_replacement_for_a_stale_queued_job(
    migrated_database: str,
) -> None:
    """P4: a lost published dispatch becomes one new durable outbox intent."""
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
        job = await jobs_service.schedule_job(
            session,
            organisation_id=organisation.id,
            job_type="file.processing",
            input_reference="file-1",
        )
        initial_dispatch = job.dispatch_id
        assert initial_dispatch is not None
        await session.execute(
            text(
                "UPDATE outbox_events SET status = 'published', processed_at = now() - interval '901 seconds' "
                "WHERE id = :event_id"
            ),
            {"event_id": initial_dispatch},
        )
        await session.commit()

    async with session_factory() as session:
        reconciled = await reconcile_queued_jobs(
            session,
            now=datetime.now(UTC),
            threshold_seconds=900,
            cooldown_seconds=900,
            limit=10,
        )
    assert reconciled == [job.id]
    async with session_factory() as session:
        events = (
            await session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.aggregate_id == job.id)
                .order_by(OutboxEvent.created_at.asc())
            )
        ).all()
        refreshed = await session.get(Job, job.id)
        assert len(events) == 2
        assert events[-1].status is OutboxEventStatus.PENDING
        assert refreshed is not None and refreshed.dispatch_id == events[-1].id

    # The active replacement prevents a second concurrent/cooled-down pass.
    async with session_factory() as session:
        assert not await reconcile_queued_jobs(
            session,
            now=datetime.now(UTC),
            threshold_seconds=900,
            cooldown_seconds=900,
            limit=10,
        )

    # Clean up so later tests' due-event counts stay independent.
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM outbox_events WHERE aggregate_id = :jid"),
            {"jid": job.id},
        )
        await session.execute(text("DELETE FROM jobs WHERE id = :jid"), {"jid": job.id})
        await session.commit()


async def test_maintenance_schedule_tick_is_deduplicated(migrated_database: str) -> None:
    """P4: duplicate coordinator ticks create one event per maintenance kind."""
    session_factory = _session_factory(migrated_database)
    now = datetime.now(UTC)
    async with session_factory() as session:
        assert (
            await schedule_maintenance_events(
                session,
                now=now,
                ai_retention_interval_hours=24,
                transfer_reconcile_interval_hours=1,
            )
            == 2
        )
    async with session_factory() as session:
        assert (
            await schedule_maintenance_events(
                session,
                now=now,
                ai_retention_interval_hours=24,
                transfer_reconcile_interval_hours=1,
            )
            == 0
        )

    # Clean up so later tests remain independent.
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM outbox_events WHERE event_type IN (:a, :t)"),
            {"a": EVENT_TYPE_AI_RETENTION, "t": EVENT_TYPE_TRANSFER_RECONCILE},
        )
        await session.commit()


async def test_concurrent_reconciliation_enforces_cooldown(migrated_database: str) -> None:
    """P4: concurrent coordinators reconcile disjoint jobs and honour cooldown.

    Two coordinators race on ``reconcile_queued_jobs`` which locks candidate
    rows with ``FOR UPDATE SKIP LOCKED``. Neither can create a replacement
    for the same job, and the cooldown-scoped deduplication key prevents a
    second recovery dispatch within the same bucket.
    """
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
        job_ids: list[uuid.UUID] = []
        for _ in range(4):
            job = await jobs_service.schedule_job(
                session,
                organisation_id=organisation.id,
                job_type="file.processing",
                input_reference="file-1",
            )
            job_ids.append(job.id)
            await session.execute(
                text(
                    "UPDATE outbox_events SET status = 'published', processed_at = now() - interval '901 seconds' "
                    "WHERE id = :event_id"
                ),
                {"event_id": job.dispatch_id},
            )
        await session.commit()

    now = datetime.now(UTC)
    results_a: list[uuid.UUID] = []
    results_b: list[uuid.UUID] = []
    barrier = asyncio.Event()

    async def _reconcile_a() -> None:
        barrier.set()
        async with session_factory() as session:
            results_a.extend(
                await reconcile_queued_jobs(
                    session,
                    now=now,
                    threshold_seconds=900,
                    cooldown_seconds=900,
                    limit=10,
                )
            )

    async def _reconcile_b() -> None:
        await barrier.wait()
        async with session_factory() as session:
            results_b.extend(
                await reconcile_queued_jobs(
                    session,
                    now=now,
                    threshold_seconds=900,
                    cooldown_seconds=900,
                    limit=10,
                )
            )

    await asyncio.gather(_reconcile_a(), _reconcile_b())
    # The two coordinators must never reconcile the same job.
    assert set(results_a).isdisjoint(set(results_b))
    # Every stale job was recovered by exactly one coordinator.
    assert set(results_a) | set(results_b) == set(job_ids)

    # A third pass within the cooldown reconciles nothing.
    async with session_factory() as session:
        assert not await reconcile_queued_jobs(
            session,
            now=now,
            threshold_seconds=900,
            cooldown_seconds=900,
            limit=10,
        )

    # Clean up all outbox events and jobs created by this test so later
    # tests' due-event assertions remain independent.
    async with session_factory() as session:
        for jid in job_ids:
            await session.execute(
                text("DELETE FROM outbox_events WHERE aggregate_id = :jid"),
                {"jid": jid},
            )
            await session.execute(text("DELETE FROM jobs WHERE id = :jid"), {"jid": jid})
        await session.commit()


# --- P2: expired-running recovery (empty-Redis backstop) ----------------------


async def _make_running_expired_job(
    session_factory: Any, *, organisation_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a claimed job whose worker died and execution lease expired.

    The initial dispatch is marked published with an old ``processed_at`` so
    the recovery cooldown is satisfied; the job is then claimed and its lease
    moved into the past, modelling a worker killed before settlement with no
    Redis copy of its message left. Returns ``(job_id, initial_dispatch,
    stale_owner_token)``.
    """
    async with session_factory() as session:
        job = await jobs_service.schedule_job(
            session,
            organisation_id=organisation_id,
            job_type="file.processing",
            input_reference="file-1",
        )
        assert job.dispatch_id is not None
        initial_dispatch = job.dispatch_id
        event = await session.get(OutboxEvent, initial_dispatch)
        assert event is not None
        event.status = OutboxEventStatus.PUBLISHED
        event.processed_at = datetime.now(UTC) - timedelta(seconds=3600)
        await session.commit()
        claim = await jobs_service.claim_dispatch(session, job_id=job.id)
        assert claim.owner_token is not None
        stale_owner = claim.owner_token
        await session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        await session.commit()
        return job.id, initial_dispatch, stale_owner


async def _cleanup_jobs(session_factory: Any, job_ids: list[uuid.UUID]) -> None:
    """Delete test-owned jobs, attempts and outbox rows (attempts before jobs)."""
    async with session_factory() as session:
        for jid in job_ids:
            await session.execute(
                text("DELETE FROM outbox_events WHERE aggregate_id = :jid"), {"jid": jid}
            )
            await session.execute(
                text("DELETE FROM job_attempts WHERE job_id = :jid"), {"jid": jid}
            )
            await session.execute(text("DELETE FROM jobs WHERE id = :jid"), {"jid": jid})
        await session.commit()


async def test_expired_running_job_recovers_into_one_durable_dispatch(
    migrated_database: str,
) -> None:
    """AC4: a dead worker is recovered from PostgreSQL with no Redis copy.

    The expired-running sweep locks the candidate, closes the dead attempt
    ``abandoned``, rotates the dispatch/owner boundary, returns the job to
    ``queued`` and creates exactly one cooldown-keyed outbox intent — never a
    direct Redis publish. The dead attempt's captured token is fenced out.
    """
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    job_id, initial_dispatch, stale_owner = await _make_running_expired_job(
        session_factory, organisation_id=organisation.id
    )

    async with session_factory() as session:
        recovered = await reconcile_running_jobs(
            session,
            now=datetime.now(UTC),
            threshold_seconds=60,
            cooldown_seconds=60,
            limit=10,
        )
    assert recovered == [job_id]

    async with session_factory() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        assert row.status is JobStatus.QUEUED
        assert row.dispatch_id is not None and row.dispatch_id != initial_dispatch
        assert row.owner_token is not None and row.owner_token != stale_owner
        assert row.execution_lease_expires_at is None
        replacement = await session.get(OutboxEvent, row.dispatch_id)
        assert replacement is not None
        assert replacement.status is OutboxEventStatus.PENDING
        attempts = list((await session.scalars(job_attempt_history_statement(job_id))).all())
        assert [attempt.status for attempt in attempts] == [JobAttemptStatus.ABANDONED]
        assert attempts[0].completed_at is not None
        # The dead attempt's captured credential is fenced out (AC5).
        with pytest.raises(jobs_service.StaleDispatchError):
            await jobs_service.verify_ownership(
                session,
                jobs_service.JobOwnership(
                    job_id=job_id,
                    owner_token=stale_owner,
                    organisation_id=organisation.id,
                ),
            )

    # A second pass within the cooldown recovers nothing: the job is queued and
    # its replacement dispatch is active.
    async with session_factory() as session:
        assert not await reconcile_running_jobs(
            session,
            now=datetime.now(UTC),
            threshold_seconds=60,
            cooldown_seconds=60,
            limit=10,
        )

    await _cleanup_jobs(session_factory, [job_id])


async def test_expired_running_job_at_global_ceiling_settles_failed(
    migrated_database: str,
) -> None:
    """AC3: recovery never grants an attempt beyond the global ceiling."""
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    job_id, initial_dispatch, _stale = await _make_running_expired_job(
        session_factory, organisation_id=organisation.id
    )
    async with session_factory() as session:
        await session.execute(
            update(Job).where(Job.id == job_id).values(attempt_count=jobs_service.MAX_ATTEMPTS)
        )
        await session.commit()

    async with session_factory() as session:
        recovered = await reconcile_running_jobs(
            session,
            now=datetime.now(UTC),
            threshold_seconds=60,
            cooldown_seconds=60,
            limit=10,
        )
    assert recovered == []

    async with session_factory() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        assert row.status is JobStatus.FAILED
        assert row.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED
        events = (
            await session.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == job_id))
        ).all()
        assert [event.id for event in events] == [initial_dispatch]  # no new dispatch
        attempts = list((await session.scalars(job_attempt_history_statement(job_id))).all())
        assert [attempt.status for attempt in attempts] == [JobAttemptStatus.EXHAUSTED]

    await _cleanup_jobs(session_factory, [job_id])


async def test_queued_reconciliation_at_global_ceiling_settles_failed(
    migrated_database: str,
) -> None:
    """AC3: queued reconciliation exhausts instead of resetting the budget."""
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
        job = await jobs_service.schedule_job(
            session,
            organisation_id=organisation.id,
            job_type="file.processing",
            input_reference="file-1",
        )
        assert job.dispatch_id is not None
        initial_dispatch = job.dispatch_id
        await session.execute(
            update(Job).where(Job.id == job.id).values(attempt_count=jobs_service.MAX_ATTEMPTS)
        )
        await session.execute(
            text(
                "UPDATE outbox_events SET status = 'published', "
                "processed_at = now() - interval '901 seconds' WHERE id = :event_id"
            ),
            {"event_id": initial_dispatch},
        )
        await session.commit()
        job_id = job.id

    async with session_factory() as session:
        reconciled = await reconcile_queued_jobs(
            session,
            now=datetime.now(UTC),
            threshold_seconds=900,
            cooldown_seconds=900,
            limit=10,
        )
    assert reconciled == []

    async with session_factory() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        assert row.status is JobStatus.FAILED
        assert row.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED
        events = (
            await session.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == job_id))
        ).all()
        assert [event.id for event in events] == [initial_dispatch]

    await _cleanup_jobs(session_factory, [job_id])


async def test_concurrent_running_recovery_never_double_recovers(
    migrated_database: str,
) -> None:
    """AC4: two coordinators recover disjoint expired-running jobs.

    ``FOR UPDATE SKIP LOCKED`` plus the per-job cooldown key mean concurrent
    coordinators never create two replacement dispatches for one job.
    """
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    job_ids: list[uuid.UUID] = []
    for _ in range(4):
        job_id, _initial, _stale = await _make_running_expired_job(
            session_factory, organisation_id=organisation.id
        )
        job_ids.append(job_id)

    now = datetime.now(UTC)
    results_a: list[uuid.UUID] = []
    results_b: list[uuid.UUID] = []
    barrier = asyncio.Event()

    async def _recover(target: list[uuid.UUID], *, wait: bool) -> None:
        if wait:
            await barrier.wait()
        else:
            barrier.set()
        async with session_factory() as session:
            target.extend(
                await reconcile_running_jobs(
                    session,
                    now=now,
                    threshold_seconds=60,
                    cooldown_seconds=60,
                    limit=10,
                )
            )

    await asyncio.gather(_recover(results_a, wait=False), _recover(results_b, wait=True))
    assert set(results_a).isdisjoint(set(results_b))
    assert set(results_a) | set(results_b) == set(job_ids)

    # Every recovered job ended with exactly one replacement dispatch.
    async with session_factory() as session:
        for job_id in job_ids:
            events = (
                await session.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == job_id))
            ).all()
            assert len(events) == 2  # initial + one recovery

    await _cleanup_jobs(session_factory, job_ids)


async def test_concurrent_maintenance_ticks_create_one_event_each(
    migrated_database: str,
) -> None:
    """P4: concurrent schedule ticks converge through the unique key.

    Two coordinators race ``schedule_maintenance_events`` simultaneously;
    the unique deduplication key is the concurrency boundary, so each
    event type gets exactly one row regardless of how many ticks overlap.
    Uses a distinct ``now`` from ``test_maintenance_schedule_tick_is_deduplicated``
    so the unique keys do not collide.
    """
    session_factory = _session_factory(migrated_database)
    now = datetime.now(UTC) + timedelta(days=1)
    counts: list[int] = []
    barrier = asyncio.Event()

    async def _tick() -> None:
        barrier.set()
        async with session_factory() as session:
            counts.append(
                await schedule_maintenance_events(
                    session,
                    now=now,
                    ai_retention_interval_hours=24,
                    transfer_reconcile_interval_hours=1,
                )
            )

    await asyncio.gather(_tick(), _tick())
    # The two ticks together created exactly 2 events (one per type).
    assert sum(counts) == 2

    # Clean up the schedule events so later tests are independent.
    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM outbox_events WHERE event_type IN (:a, :t)"),
            {"a": EVENT_TYPE_AI_RETENTION, "t": EVENT_TYPE_TRANSFER_RECONCILE},
        )
        await session.commit()


# --- Pure helpers -------------------------------------------------------------


def test_backoff_delay_is_capped_and_jittered() -> None:
    """The capped exponential backoff never exceeds its max (plan P3)."""
    from app.job_coordinator.loop import backoff_delay

    for failures in range(1, 12):
        delay = backoff_delay(consecutive_failures=failures, initial_seconds=1.0, max_seconds=300.0)
        assert 0 <= delay <= 300.0
    # The cap binds even for very long failure streaks.
    for _ in range(50):
        delay = backoff_delay(consecutive_failures=30, initial_seconds=60.0, max_seconds=90.0)
        assert 0 <= delay <= 90.0
    # An extreme attempt count cannot overflow: the exponent is clamped before
    # exponentiation (review regression; 2000 attempts previously raised).
    for _ in range(20):
        delay = backoff_delay(consecutive_failures=2000, initial_seconds=1.0, max_seconds=300.0)
        assert 0 <= delay <= 300.0


def test_cycle_log_is_immediate_for_work_and_throttled_when_idle() -> None:
    """Idle polls retain a one-minute liveness log without console spam."""
    idle = CycleStats()
    assert cycle_log_due(idle, now=10.0, last_log_at=None)
    assert not cycle_log_due(idle, now=69.9, last_log_at=10.0)
    assert cycle_log_due(idle, now=70.0, last_log_at=10.0)

    active = CycleStats(claimed=1, published=1)
    assert cycle_log_due(active, now=10.1, last_log_at=10.0)


def test_bounded_error_truncates_to_column_width() -> None:
    """Dead-event errors are bounded and never carry payload content (plan P3)."""
    from app.job_coordinator.loop import bounded_error

    assert bounded_error(RuntimeError("TOP_SECRET_SENTINEL")) == (
        "transient_infrastructure_failure"
    )
    assert bounded_error(OutboxContractError("TOP_SECRET_SENTINEL")) == ("invalid_outbox_contract")


async def test_publish_errors_never_log_or_return_exception_content() -> None:
    """Rejected payloads and transient credentials stay out of safe errors/logs."""
    sentinel = "TOP_SECRET_SENTINEL"
    invalid = OutboxEvent(
        organisation_id=None,
        event_type=EVENT_TYPE_AI_RETENTION,
        event_version=1,
        aggregate_type=None,
        aggregate_id=None,
        payload={"unexpected": sentinel},
        status=OutboxEventStatus.PUBLISHING,
    )
    with capture_logs() as permanent_logs:
        outcome, error = await publish_event(
            cast(Any, None), event=invalid, registry=DispatchRegistry()
        )
    assert outcome == "dead"
    assert error == "invalid_outbox_contract"
    assert sentinel not in str(permanent_logs)

    class _CredentialLeakingTarget:
        def send(self) -> None:
            raise RuntimeError(f"redis://user:{sentinel}@broker/0")

    valid = OutboxEvent(
        organisation_id=None,
        event_type=EVENT_TYPE_AI_RETENTION,
        event_version=1,
        aggregate_type=None,
        aggregate_id=None,
        payload={},
        status=OutboxEventStatus.PUBLISHING,
    )
    registry = DispatchRegistry(
        maintenance_actors={EVENT_TYPE_AI_RETENTION: _CredentialLeakingTarget()}
    )
    with capture_logs() as transient_logs:
        outcome, error = await publish_event(cast(Any, None), event=valid, registry=registry)
    assert outcome == "transient"
    assert error is None
    assert sentinel not in str(transient_logs)


# --- Claim concurrency -------------------------------------------------------


async def test_two_coordinators_claim_disjoint_batches(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """Two concurrent claimers never own the same row (plan P3, AC3)."""
    _broker, _worker, _registry, _received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    event_ids: list[uuid.UUID] = []
    for _ in range(6):
        _job, event_id = await _schedule_job(session_factory, organisation_id=organisation.id)
        event_ids.append(event_id)

    # Coordinator A acquires a bounded set of row locks and deliberately keeps
    # its transaction open. Coordinator B must skip those locks and claim a
    # different bounded set without waiting for A to commit.
    async with session_factory() as first_session:
        first_rows = (
            await first_session.scalars(
                due_outbox_events_statement(at_or_before=datetime.now(), limit=3).with_for_update(
                    skip_locked=True
                )
            )
        ).all()
        first = {event.id for event in first_rows}
        assert len(first) == 3

        second_started = asyncio.Event()

        async def _claim_second() -> set[uuid.UUID]:
            second_started.set()
            async with session_factory() as session:
                claimed = await claim_due_events(session, limit=3, now=datetime.now())
                return {claimed_event.event.id for claimed_event in claimed}

        second_task = asyncio.create_task(_claim_second())
        await second_started.wait()
        second = await asyncio.wait_for(second_task, timeout=3)

        for event in first_rows:
            event.status = OutboxEventStatus.PUBLISHING
            event.claimed_at = datetime.now()
            event.claim_token = uuid.uuid4().hex
            event.attempt_count += 1
        await first_session.commit()

    assert first.isdisjoint(second)
    assert first | second == set(event_ids)

    # Leave no `publishing` rows behind: this test only exercises the claim
    # step, so the claimed rows are deleted rather than settled (their jobs
    # were never meant to publish), keeping later tests' stale-claim
    # assertions independent.
    async with session_factory() as session:
        for event_id in first | second:
            row = await session.get(OutboxEvent, event_id)
            if row is not None:
                await session.delete(row)
        await session.commit()


# --- Publication and settlement ----------------------------------------------


async def test_cycle_publishes_dispatch_event_and_settles_published(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """One cycle publishes the reference-only message and settles the row (AC3)."""
    _broker, _worker, registry, received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    job, event_id = await _schedule_job(session_factory, organisation_id=organisation.id)

    stats = await run_cycle(
        session_factory, registry=registry, batch_size=50, publication_lease_seconds=60
    )
    assert stats.claimed == 1
    assert stats.published == 1
    await _wait_for_received(received, 1)
    assert received == [str(job.id)]

    row = await _outbox_row(session_factory, event_id)
    assert row.status is OutboxEventStatus.PUBLISHED
    assert row.processed_at is not None
    assert row.claim_token is None


async def test_cycle_publishes_maintenance_event(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """A maintenance event publishes its argument-free message (AC3)."""
    _broker, _worker, registry, received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        await _create_org(session)  # maintenance events carry no tenant context
        event = await create_schedule_event(
            session,
            event_type=EVENT_TYPE_AI_RETENTION,
            schedule_key=f"ai-retention:{uuid.uuid4().hex}",
        )
        await session.commit()
        event_id = event.id

    stats = await run_cycle(
        session_factory, registry=registry, batch_size=50, publication_lease_seconds=60
    )
    assert stats.claimed == 1
    assert stats.published == 1
    await _wait_for_received(received, 1)
    assert "maintenance" in received

    row = await _outbox_row(session_factory, event_id)
    assert row.status is OutboxEventStatus.PUBLISHED
    assert row.organisation_id is None


# --- Transient failures and permanent death ----------------------------------


async def test_real_redis_broker_down_releases_then_recovers(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely unreachable Redis releases the claim; a healthy cycle recovers (AC3).

    P3's real-Redis broker-down evidence: the first cycle publishes through a
    real ``RedisBroker`` pointed at a port nothing is listening on, so the
    transient path is exercised by a genuine connection failure rather than a
    fake target whose ``send`` raises. The row returns to ``pending`` (never
    dead, never lost) with a deterministic zero-delay retry, and the second
    cycle — through the fixture's healthy Redis broker — proves the recovery
    publication.
    """
    _broker, _worker, registry, received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    job, event_id = await _schedule_job(session_factory, organisation_id=organisation.id)

    monkeypatch.setattr("app.job_coordinator.loop.uniform", _zero_jitter)

    down_broker = RedisBroker(url=_closed_redis_url(), middleware=worker_middleware())

    def _record_down(job_id: str) -> None:
        received.append(job_id)

    down_actor = _actor_on_broker(
        down_broker, _record_down, actor_name="coordinator_down", queue_name="coordinator-down"
    )
    down_registry = DispatchRegistry(
        job_actors={"file.processing": down_actor}, maintenance_actors={}
    )
    try:
        stats = await run_cycle(
            session_factory,
            registry=down_registry,
            batch_size=50,
            publication_lease_seconds=60,
            backoff_initial_seconds=0.05,
            backoff_max_seconds=0.05,
        )
        assert stats.released == 1
        assert stats.published == 0
        row = await _outbox_row(session_factory, event_id)
        assert row.status is OutboxEventStatus.PENDING
        assert row.claim_token is None
        assert received == []  # nothing reached the healthy broker

        # Redis is reachable again: a working coordinator publishes the row.
        stats = await run_cycle(
            session_factory,
            registry=registry,
            batch_size=50,
            publication_lease_seconds=60,
            backoff_initial_seconds=0.05,
            backoff_max_seconds=0.05,
        )
        assert stats.published == 1
        await _wait_for_received(received, 1)
        assert received == [str(job.id)]
        row = await _outbox_row(session_factory, event_id)
        assert row.status is OutboxEventStatus.PUBLISHED
    finally:
        down_broker.close()


async def test_released_row_is_not_reclaimable_before_its_backoff_expires(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable retry time gates re-claims by any coordinator (plan P3).

    A transient failure releases the row with ``available_at`` advanced by
    the jittered capped backoff derived from the row's attempt count, so the
    retry is durable: a second coordinator cannot reclaim the row before
    ``available_at``, and can once it is due again. (The seam uses a target
    whose ``send`` raises; the real broker-down path is proven by the test
    above.)
    """
    _broker, _worker, _fixture_registry, _received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    _job, event_id = await _schedule_job(session_factory, organisation_id=organisation.id)

    # Fixed mid-range jitter so the first-attempt delay is deterministic:
    # cap = initial = 60s, delay = 30s, far beyond any local clock skew.
    monkeypatch.setattr("app.job_coordinator.loop.uniform", _midpoint_jitter)

    class _FlakyTarget:
        def send(self, job_id: str) -> None:
            raise RuntimeError("broker temporarily unavailable")

    flaky = DispatchRegistry(job_actors={"file.processing": _FlakyTarget()}, maintenance_actors={})

    stats = await run_cycle(
        session_factory,
        registry=flaky,
        batch_size=50,
        publication_lease_seconds=60,
        backoff_initial_seconds=60.0,
        backoff_max_seconds=60.0,
    )
    assert stats.released == 1
    row = await _outbox_row(session_factory, event_id)
    assert row.status is OutboxEventStatus.PENDING
    assert row.claim_token is None
    assert row.attempt_count == 1

    # A second coordinator cannot reclaim the released row before its
    # persisted retry time.
    async with session_factory() as session:
        claimed = await claim_due_events(session, limit=50, now=datetime.now())
    assert claimed == []

    # Once the backoff has elapsed the row is due again and claimable.
    async with session_factory() as session:
        row = await session.get(OutboxEvent, event_id)
        assert row is not None
        row.available_at = datetime.now() - timedelta(seconds=1)
        await session.commit()
    async with session_factory() as session:
        claimed = await claim_due_events(session, limit=50, now=datetime.now())
    assert len(claimed) == 1

    # The claimed row is back in the publication path (fresh token).
    assert claimed[0].event.id == event_id
    assert claimed[0].event.status is OutboxEventStatus.PUBLISHING
    assert claimed[0].event.claim_token is not None

    # Leave no `publishing` rows behind: later tests' stale-claim assertions
    # reclaim every stale row, so an un-settled claim here would bleed into
    # them (same pattern as ``test_two_coordinators_claim_disjoint_batches``).
    async with session_factory() as session:
        row = await session.get(OutboxEvent, event_id)
        if row is not None:
            await session.delete(row)
        await session.commit()


async def test_persisted_backoff_starts_at_release_not_cycle_start(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persisted retry time is measured from release, not the cycle start.

    A slow transient failure consumes wall-clock time between the cycle's
    claim (its ``now`` snapshot) and the release settlement. ``available_at``
    must still be a full backoff ahead of the database clock at the moment of
    release; a cycle-start origin would let the slow publish eat part (or
    all) of the delay and make the row reclaimable immediately after release
    (review regression).
    """
    _broker, _worker, _fixture_registry, _received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    _job, event_id = await _schedule_job(session_factory, organisation_id=organisation.id)

    # Deterministic delay: cap = 60 s, and the jitter returns the cap itself.
    monkeypatch.setattr("app.job_coordinator.loop.uniform", _max_jitter)

    class _SlowFlakyTarget:
        """A publish that fails only after consuming real wall-clock time."""

        def send(self, job_id: str) -> None:
            time.sleep(0.5)
            raise RuntimeError("broker temporarily unavailable")

    slow_flaky = DispatchRegistry(
        job_actors={"file.processing": _SlowFlakyTarget()}, maintenance_actors={}
    )

    stats = await run_cycle(
        session_factory,
        registry=slow_flaky,
        batch_size=50,
        publication_lease_seconds=60,
        backoff_initial_seconds=60.0,
        backoff_max_seconds=60.0,
    )
    assert stats.released == 1

    row = await _outbox_row(session_factory, event_id)
    assert row is not None
    assert row.status is OutboxEventStatus.PENDING
    async with session_factory() as session:
        after = await session.scalar(select(func.now()))
    assert row.available_at is not None
    # The full 60 s delay still lies ahead of the release-time clock. The
    # tolerance only absorbs the gap between the release UPDATE and this
    # assertion's clock read; the cycle-start origin would be short by the
    # 0.5 s slow publish and fail this check.
    assert row.available_at >= after + timedelta(seconds=59.8)


async def test_unknown_event_becomes_dead_with_bounded_error(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """A permanently invalid event becomes ``dead``, never retried (AC3)."""
    _broker, _worker, registry, _received = broker_and_recording
    sentinel = "TOP_SECRET_SENTINEL"
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        await _create_org(session)
        event = OutboxEvent(
            organisation_id=None,
            event_type="unknown.event",
            event_version=1,
            aggregate_type=None,
            aggregate_id=None,
            payload={"credential": sentinel},
            status=OutboxEventStatus.PENDING,
        )
        session.add(event)
        await session.commit()
        event_id = event.id

    with capture_logs() as logs:
        stats = await run_cycle(
            session_factory, registry=registry, batch_size=50, publication_lease_seconds=60
        )
    assert stats.dead == 1
    assert stats.published == 0
    row = await _outbox_row(session_factory, event_id)
    assert row.status is OutboxEventStatus.DEAD
    assert row.last_error == "invalid_outbox_contract"
    assert sentinel not in row.last_error
    assert sentinel not in str(logs)

    # A second cycle does not touch the dead row.
    stats = await run_cycle(
        session_factory, registry=registry, batch_size=50, publication_lease_seconds=60
    )
    assert stats.claimed == 0 and stats.dead == 0


async def test_dispatch_event_for_missing_job_becomes_dead(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """A dispatch event whose job aggregate vanished is dead, not retried."""
    _broker, _worker, registry, _received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
        missing_job_id = uuid.uuid4()
        event = OutboxEvent(
            organisation_id=organisation.id,
            event_type=EVENT_TYPE_JOB_DISPATCH,
            event_version=EVENT_VERSION_JOB_DISPATCH,
            aggregate_type="job",
            aggregate_id=missing_job_id,
            payload={"job_id": str(missing_job_id)},
            status=OutboxEventStatus.PENDING,
        )
        session.add(event)
        await session.commit()
        event_id = event.id

    stats = await run_cycle(
        session_factory, registry=registry, batch_size=50, publication_lease_seconds=60
    )
    assert stats.dead == 1
    row = await _outbox_row(session_factory, event_id)
    assert row.status is OutboxEventStatus.DEAD
    assert row.last_error == "invalid_outbox_contract"


async def test_dead_current_initial_dispatch_fails_job_atomically(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """A permanently invalid current initial dispatch cannot strand its job."""
    _broker, _worker, _registry, _received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
        job = await jobs_service.schedule_job(
            session,
            organisation_id=organisation.id,
            job_type="unregistered.job",
            input_reference="opaque-reference",
        )
        job_id = job.id
        event_id = job.dispatch_id
    registry = DispatchRegistry(job_actors={}, maintenance_actors={})
    stats = await run_cycle(
        session_factory, registry=registry, batch_size=50, publication_lease_seconds=60
    )
    assert stats.dead == 1
    async with session_factory() as session:
        event = await session.get(OutboxEvent, event_id)
        failed = await session.get(Job, job_id)
        assert event is not None and event.status is OutboxEventStatus.DEAD
        assert event.last_error == "invalid_dispatch_target"
        assert failed is not None and failed.status is JobStatus.FAILED
        assert failed.error_code == jobs_service.ERROR_CODE_DISPATCH_INVALID


async def test_dead_notification_dispatch_records_dispatch_failure_reason(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """A never-attempted email is audited as undeliverable, not exhausted."""
    _broker, _worker, _registry, _received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
        user = User(
            workos_user_id=f"user_{uuid.uuid4().hex}",
            email="ada@example.com",
            name="Ada Lovelace",
        )
        session.add(user)
        await session.commit()
        notification, delivery, job = await notifications_service.send_test_notification(
            session,
            organisation_id=organisation.id,
            user_id=user.id,
            recipient_email=user.email,
            actor_user_id=user.id,
        )
        delivery_id = delivery.id
        notification_id = notification.id
        job_id = job.id

    registry = DispatchRegistry(job_actors={}, maintenance_actors={})
    stats = await run_cycle(
        session_factory, registry=registry, batch_size=50, publication_lease_seconds=60
    )
    assert stats.dead == 1
    async with session_factory() as session:
        failed_job = await session.get(Job, job_id)
        failed_delivery = await session.get(NotificationDelivery, delivery_id)
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "notification.delivery_failed",
                AuditEvent.resource_id == str(notification_id),
            )
        )
        assert failed_job is not None
        assert failed_job.error_code == jobs_service.ERROR_CODE_DISPATCH_INVALID
        assert failed_delivery is not None
        assert failed_delivery.status is NotificationDeliveryStatus.FAILED
        assert audit is not None
        assert audit.event_metadata["error_code"] == (
            notifications_service.DELIVERY_ERROR_DISPATCH_FAILED
        )


async def test_dead_stale_initial_dispatch_does_not_fail_newer_dispatch(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """A dead event is harmless once a newer dispatch owns the queued job."""
    _broker, _worker, _registry, _received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
        job = await jobs_service.schedule_job(
            session,
            organisation_id=organisation.id,
            job_type="unregistered.job",
            input_reference="opaque-reference",
        )
        stale_event_id = job.dispatch_id
        newer_dispatch_id = uuid.uuid4()
        job.dispatch_id = newer_dispatch_id
        await session.commit()
        job_id = job.id

    registry = DispatchRegistry(job_actors={}, maintenance_actors={})
    stats = await run_cycle(
        session_factory, registry=registry, batch_size=50, publication_lease_seconds=60
    )
    assert stats.dead == 1
    async with session_factory() as session:
        stale_event = await session.get(OutboxEvent, stale_event_id)
        current = await session.get(Job, job_id)
        assert stale_event is not None
        assert stale_event.status is OutboxEventStatus.DEAD
        assert current is not None
        assert current.status is JobStatus.QUEUED
        assert current.dispatch_id == newer_dispatch_id
        assert current.error_code is None


# --- Crash window and graceful restart ---------------------------------------


async def test_late_settlement_after_takeover_cannot_overwrite_the_new_owner(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """Settlement is one owner-checked UPDATE; a stale publisher loses (AC3).

    Coordinator A claims the row (token A) and crashes after the broker
    accepted the message. Coordinator B reclaims the stale claim (token B).
    A's late settlement must affect zero rows — its predicate still requires
    its own token and the ``publishing`` status — so it can never overwrite
    B's state; only B's own settlement publishes the row.
    """
    _broker, _worker, _registry, _received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    _job, event_id = await _schedule_job(session_factory, organisation_id=organisation.id)

    # Coordinator A claims the row with token A.
    async with session_factory() as session:
        claimed_a = await claim_due_events(session, limit=50, now=datetime.now())
        assert len(claimed_a) == 1
        token_a = claimed_a[0].claim_token

    # Coordinator B reclaims the now-stale claim with a fresh token B.
    async with session_factory() as session:
        reclaimed_b = await reclaim_stale_claims(
            session,
            claimed_before=datetime.now() + timedelta(seconds=1),
            limit=50,
            now=datetime.now(),
        )
        assert len(reclaimed_b) == 1
        token_b = reclaimed_b[0].claim_token
        assert token_b != token_a

    # A's late settlement is rejected: zero rows affected, B's ownership intact.
    async with session_factory() as session:
        settled = await settle_published(session, event_id=event_id, claim_token=token_a)
        assert settled is False
        row = await session.get(OutboxEvent, event_id)
        assert row is not None
        assert row.status is OutboxEventStatus.PUBLISHING
        assert row.claim_token == token_b

    # B settles with its own token and owns the published row.
    async with session_factory() as session:
        settled = await settle_published(session, event_id=event_id, claim_token=token_b)
        assert settled is True
    row = await _outbox_row(session_factory, event_id)
    assert row.status is OutboxEventStatus.PUBLISHED
    assert row.claim_token is None


async def test_crash_after_send_republishes_duplicate_message(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """A crash after Redis accepted the message republishes once (AC4).

    The documented at-least-once window: the first claim publishes message 1
    but never settles (the coordinator "crashed"); once the publication lease
    expires the stale-claim recovery republishes the same event. The broker
    therefore receives two messages for one job — the duplicate is made
    harmless by the P2 execution ownership, proven in ``test_files_jobs.py``
    and ``test_jobs_db.py``.
    """
    _broker, _worker, registry, received = broker_and_recording
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    job, event_id = await _schedule_job(session_factory, organisation_id=organisation.id)

    # Cycle 1 publishes but settlement is skipped — the crash window. The
    # claim lease is 0 seconds so the row is immediately stale-claimable.
    async with session_factory() as session:
        claimed = await claim_due_events(session, limit=50, now=datetime.now())
        assert len(claimed) == 1
    outcome, _error = await publish_event(
        session_factory, event=claimed[0].event, registry=registry
    )
    assert outcome == "published"
    await _wait_for_received(received, 1)
    assert received == [str(job.id)]

    # Cycle 2: the stale claim is reclaimed and republished, then settled.
    stats = await run_cycle(
        session_factory, registry=registry, batch_size=50, publication_lease_seconds=0
    )
    assert stats.stale_reclaimed == 1
    assert stats.published == 1
    await _wait_for_received(received, 2)
    assert received == [str(job.id), str(job.id)]

    row = await _outbox_row(session_factory, event_id)
    assert row.status is OutboxEventStatus.PUBLISHED


async def test_shutdown_during_batch_leaves_intent_for_a_fresh_coordinator(
    migrated_database: str,
    broker_and_recording: tuple[RedisBroker, Worker, DispatchRegistry, list[str]],
) -> None:
    """An in-flight stop claims no new work; a fresh coordinator recovers.

    The coordinator threads its shutdown signal through batch processing.
    Here a deliberately slow multi-row batch is still in flight when the stop
    lands: the in-flight event is allowed to settle, but no fresh work is
    claimed afterwards, so unprocessed intent stays ``pending``/``publishing``
    in PostgreSQL (never lost). A fresh coordinator — whose claim lease is
    expired — recovers and publishes everything left behind (plan P3).
    """
    _broker, _worker, _fixture_registry, received = broker_and_recording
    registry = _complete_test_registry(received)
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session)
    scheduled = [
        await _schedule_job(session_factory, organisation_id=organisation.id) for _ in range(3)
    ]
    event_ids = [event_id for _job, event_id in scheduled]

    # A slow registry (0.4s per publish) with a batch bound of 2: the first
    # cycle can never claim the third row, so a shutdown mid-cycle must leave
    # at least one row unprocessed.
    slow_registry = _slow_test_registry(received, send_delay=0.4)

    shutdown_event = asyncio.Event()
    runner = asyncio.create_task(
        run_coordinator(
            session_factory,
            registry=slow_registry,
            batch_size=2,
            idle_poll_seconds=0.05,
            publication_lease_seconds=60,
            backoff_initial_seconds=0.05,
            backoff_max_seconds=0.2,
            shutdown_event=shutdown_event,
        )
    )
    # Wait until at least one in-flight publish completed, then stop.
    await _wait_for_received(received, 1)
    shutdown_event.set()
    await asyncio.wait_for(runner, timeout=15)

    statuses = [await _outbox_row(session_factory, event_id) for event_id in event_ids]
    published = [row for row in statuses if row.status is OutboxEventStatus.PUBLISHED]
    unprocessed = [
        row
        for row in statuses
        if row.status in (OutboxEventStatus.PENDING, OutboxEventStatus.PUBLISHING)
    ]
    assert published, "the in-flight batch should have delivered some work"
    assert unprocessed, "shutdown must leave unprocessed intent durable"

    # A fresh coordinator recovers everything: expired-leave stale claims are
    # reclaimed, still-pending rows are claimed, and all of them publish.
    stats = await run_cycle(
        session_factory,
        registry=registry,
        batch_size=50,
        publication_lease_seconds=0,
        backoff_initial_seconds=0.05,
        backoff_max_seconds=0.2,
    )
    assert stats.published + stats.stale_reclaimed >= len(unprocessed)
    await _wait_for_received(received, len(scheduled))
    assert sorted(received) == sorted(str(job.id) for job, _event_id in scheduled)
    for event_id in event_ids:
        row = await _outbox_row(session_factory, event_id)
        assert row.status is OutboxEventStatus.PUBLISHED
