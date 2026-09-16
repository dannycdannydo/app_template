"""Real-broker integration tests for the durable job pipeline (Scope §6.4).

The service lifecycle is proven against the real database in
``test_jobs_db.py``, but its broker is an in-memory stub, so the wiring
between durable job scheduling, a real Redis broker and a real worker thread
could silently break. These tests run the whole pipeline against a real Redis
broker (unique namespace so they cannot touch a developer's worker) and a
reachable PostgreSQL, and skip when either is missing — the CI backend-test
job provides both services (Scope §6.4 CI item), so the journey runs on every
push and is skipped locally without them.

Two journeys are proven:

- the happy path: intent row ``queued`` -> the worker runs an async task that
  moves it through ``running`` with progress 0->50->100 to ``succeeded``;
- the retry-exhaustion path: a task that keeps failing with a transient error
  is retried up to ``MAX_ATTEMPTS``, then the real
  ``mark_job_failed_after_retries`` handler records ``failed`` with the
  exhausted error code, so a job never sits in ``running`` forever.

Scope §6.5 adds the real-broker half of the files<->jobs journey: the real
``process_file`` handler re-declared on the namespaced broker, enqueued by
``files_service.complete_upload``, runs against the real database and leaves
the file ``ready`` and the job ``succeeded`` with progress 100. The
stub-broker half of the same journey lives in ``test_files_jobs.py``.

The middleware stack is the same factory the worker process uses
(``app.broker.worker_middleware``), so the async tasks run exactly as they
do in production; only the broker (namespace) and the per-test backoff differ.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import dramatiq
import pytest
from alembic import command
from alembic.config import Config
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import CurrentMessage
from dramatiq.worker import Worker
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.audit.models import AuditEvent
from app.modules.files import service as files_service
from app.modules.files import tasks as files_tasks
from app.modules.files.models import FileStatus
from app.modules.jobs import execution as jobs_execution
from app.modules.jobs import service as jobs_service
from app.modules.jobs import tasks as jobs_tasks
from app.modules.jobs.models import Job, JobStatus
from app.modules.notifications import service as notifications_service
from app.modules.notifications import tasks as notifications_tasks
from app.modules.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationDeliveryStatus,
)
from app.modules.organisations.models import Organisation
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus
from app.modules.users.models import User
from app.observability.metrics import JOBS_STALE_MESSAGES_TOTAL
from app.storage import FakeObjectStorage, get_storage

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_QUEUE = "test-jobs-broker"

# The test overrides only the backoff (production default 1000 ms) so the
# retries complete in milliseconds; max_retries/throws/on_retry_exhausted come
# from the real policy.
_EXHAUST_MIN_BACKOFF_MS = 50


def _probe(url: str) -> bool:
    """Probe one external service with a short async connect."""

    async def _run() -> bool:
        if url.startswith("postgres"):
            engine = create_async_engine(url, poolclass=NullPool)
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                return True
            except Exception:
                return False
            finally:
                await engine.dispose()
        from redis.asyncio import Redis

        # redis-py's async client is not fully typed for from_url in strict
        # mode; the probe only needs ping() (same pattern as rate_limit.py).
        client = cast(Any, Redis).from_url(url, decode_responses=True)
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
async def broker_and_worker() -> AsyncIterator[tuple[RedisBroker, Worker]]:
    """A namespaced real Redis broker and a real worker, configured like prod.

    The namespace isolates the test from any developer's worker; the middleware
    stack comes from ``app.broker.worker_middleware`` (the same factory the
    ``dramatiq app.workers`` process uses), so async tasks run on the AsyncIO
    event-loop thread exactly as in production.
    """
    from app.broker import worker_middleware

    previous_broker = dramatiq.get_broker()
    broker = RedisBroker(
        url=_REDIS_URL,
        namespace=f"jobs-test-{uuid.uuid4().hex[:8]}",
        middleware=worker_middleware(),
        # Retried (delayed) messages only move from the delay queue when
        # maintenance runs, which production keeps rare (0.1% per command).
        # Running maintenance on every command keeps the exhaustion test in
        # milliseconds instead of minutes.
        maintenance_chance=1_000_000,
    )
    dramatiq.set_broker(broker)
    # Re-declare the real retries-exhausted handler bound to THIS broker: the
    # module-level actor is bound to whatever broker was global when the task
    # module was first imported, and Actor.send() enqueues on the actor's own
    # broker, so the exhaustion message must land in this test's namespace for
    # this worker to see it. The function is the same one the worker process
    # runs.
    handler = dramatiq.actor(
        actor_name=jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR,
        queue_name=jobs_tasks.HANDLER_QUEUE,
        max_retries=0,
        throws=(),
    )(jobs_tasks.mark_job_failed_after_retries)
    assert handler.actor_name == jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR
    worker = Worker(broker, worker_threads=2)
    worker.start()
    try:
        yield broker, worker
    finally:
        worker.stop()
        broker.flush_all()
        # ``flush_all`` removes declared queues but Dramatiq keeps broker
        # heartbeats in a namespace-level key. Remove that final test-owned key
        # so repeated integration runs do not leak Redis state.
        broker.client.delete(  # pyright: ignore[reportUnknownMemberType]
            f"{broker.namespace}:__heartbeats__"
        )
        dramatiq.set_broker(previous_broker)
        # The handlers (and v0.5 Scope §6.5's process_file) run on the worker's
        # AsyncIO event-loop thread but use the process-wide engine
        # (async_session_factory). Dispose the pool so no loop-bound connection
        # outlives this test's worker: the next test's worker runs on a fresh
        # loop and would otherwise reuse a connection "attached to a different
        # loop", failing every task.
        from app.db.session import engine

        await engine.dispose()


def _session_factory(database_url: str) -> Any:
    """A NullPool session factory safe to share across event loops.

    NullPool creates a fresh connection per checkout on the loop that does the
    checkout, so the pytest loop (setup/assert) and the worker's AsyncIO loop
    (task execution) never share a pooled connection.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


def _make_round_trip_task(session_factory: Any) -> Any:
    """An async task that drives a job through running -> succeeded.

    The handler runs through the shared execution wrapper (plan P2), so the
    worker-side claim/ownership path is exercised on the real broker.
    """

    async def _run(job_id: str) -> None:
        job_id_uuid = uuid.UUID(job_id)

        async def _drive(context: Any, session: Any) -> None:
            await jobs_service.update_progress(
                session,
                job_id=job_id_uuid,
                progress=50,
                owner_token=context.owner_token,
            )
            await jobs_service.update_progress(
                session,
                job_id=job_id_uuid,
                progress=100,
                owner_token=context.owner_token,
            )
            await jobs_service.succeed(
                session,
                job_id=job_id_uuid,
                result_reference="file-1",
                owner_token=context.owner_token,
            )

        await jobs_execution.run_claimed(job_id=job_id_uuid, handler=_drive)

    return dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(_run)


def _make_transient_failure_task(session_factory: Any) -> Any:
    """An async task that claims the job, then fails transiently.

    Claiming first mirrors what every real task does (plan P2: the execution
    wrapper claims before the domain work), so the durable ``attempt_count``
    reflects all ``MAX_ATTEMPTS`` attempts and each transient failure releases
    the owned attempt back to ``queued`` for the next retry.
    """

    async def _run(job_id: str) -> None:
        job_id_uuid = uuid.UUID(job_id)

        async def _fail_transient(context: Any, session: Any) -> None:
            raise RuntimeError("storage temporarily unreachable")

        await jobs_execution.run_claimed(job_id=job_id_uuid, handler=_fail_transient)

    options: dict[str, Any] = {
        **jobs_service.retry_policy(),
        "min_backoff": _EXHAUST_MIN_BACKOFF_MS,
    }
    return dramatiq.actor(queue_name=_QUEUE, **options)(_run)


def _make_process_file_task() -> Any:
    """The real ``process_file`` handler re-declared bound to this broker.

    ``complete_upload`` only schedules the durable job (plan P3); the tests
    publish the reference-only message through this re-declared actor so the
    enqueue lands on the namespaced Redis broker this worker consumes — the
    same seam the coordinator's registry uses in production.
    """
    return dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(
        files_tasks.process_file
    )


async def _create_org(session_factory: Any) -> Organisation:
    async with session_factory() as session:
        organisation = Organisation(name=f"Jobs Broker {uuid.uuid4().hex[:8]} Ltd")
        session.add(organisation)
        await session.commit()
        return organisation


async def _wait_for_status(
    session_factory: Any, job_id: uuid.UUID, expected: JobStatus, *, timeout: float = 20.0
) -> Job:
    """Poll the durable row until it reaches the expected terminal status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with session_factory() as session:
            job = await session.get(Job, job_id)
            if job is not None and job.status == expected:
                return job
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached {expected.value!r} within {timeout}s")


async def _publish_durable_retry(
    session_factory: Any,
    task: Any,
    job_id: uuid.UUID,
    *,
    after_attempt: int,
) -> None:
    """Wait for PostgreSQL's retry intent, mark it published, then deliver it."""
    deadline = time.monotonic() + 20
    current: Job | None = None
    while time.monotonic() < deadline:
        async with session_factory() as session:
            current = await session.get(Job, job_id)
        if (
            current is not None
            and current.attempt_count == after_attempt
            and current.status is JobStatus.QUEUED
        ):
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("worker did not write a durable retry dispatch")
    assert current is not None and current.dispatch_id is not None
    async with session_factory() as session:
        event = await session.get(OutboxEvent, current.dispatch_id)
        assert event is not None and event.status is OutboxEventStatus.PENDING
        delay = max(0.0, (event.available_at - datetime.now(UTC)).total_seconds())
    await asyncio.sleep(delay + 0.05)
    async with session_factory() as session:
        event = await session.get(OutboxEvent, current.dispatch_id)
        assert event is not None
        event.status = OutboxEventStatus.PUBLISHED
        await session.commit()
    task.send(job_id=str(job_id))


async def test_worker_completes_job_lifecycle_with_progress(
    migrated_database: str, broker_and_worker: tuple[RedisBroker, Worker]
) -> None:
    """Acceptance §5.6/§5.8: enqueue -> worker -> running with progress -> succeeded."""
    session_factory = _session_factory(migrated_database)
    organisation = await _create_org(session_factory)
    task = _make_round_trip_task(session_factory)
    async with session_factory() as session:
        job = await jobs_service.schedule_job(
            session,
            organisation_id=organisation.id,
            job_type="file.processing",
            input_reference="file-1",
            actor_user_id=None,
        )
    task.send(job_id=str(job.id))  # the coordinator publishes

    finished = await _wait_for_status(session_factory, job.id, JobStatus.SUCCEEDED)
    assert finished.progress == 100
    assert finished.attempt_count == 1
    assert finished.started_at is not None
    assert finished.completed_at is not None
    assert finished.result_reference == "file-1"
    assert finished.error_code is None


async def test_postgres_owned_retry_exhaustion_records_failed_status(
    migrated_database: str, broker_and_worker: tuple[RedisBroker, Worker]
) -> None:
    """Real Redis loss cannot strand the PostgreSQL-owned retry decision."""
    session_factory = _session_factory(migrated_database)
    organisation = await _create_org(session_factory)
    task = _make_transient_failure_task(session_factory)
    async with session_factory() as session:
        job = await jobs_service.schedule_job(
            session,
            organisation_id=organisation.id,
            job_type="file.processing",
            input_reference="file-1",
        )
    task.send(job_id=str(job.id))  # the coordinator publishes

    # The first worker failure writes a delayed outbox event. Flush its old
    # broker message before the next dispatch to prove the retry is durable.
    broker, _worker = broker_and_worker
    for attempt_number in range(1, jobs_service.MAX_ATTEMPTS):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            async with session_factory() as session:
                current = await session.get(Job, job.id)
                if (
                    current is not None
                    and current.attempt_count == attempt_number
                    and current.status == JobStatus.QUEUED
                ):
                    break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("worker did not write a durable retry dispatch")
        assert current is not None and current.dispatch_id is not None
        async with session_factory() as session:
            event = await session.get(OutboxEvent, current.dispatch_id)
            assert event is not None and event.status == OutboxEventStatus.PENDING
            assert event.payload == {"job_id": str(job.id)}
            # A coordinator publication changes the event state. Its due time
            # is already enforced in claim_dispatch, so wait until it is due.
            due_in = max(0.0, (event.available_at - datetime.now(UTC)).total_seconds())
        broker.flush_all()
        await asyncio.sleep(due_in + 0.05)
        async with session_factory() as session:
            event = await session.get(OutboxEvent, current.dispatch_id)
            assert event is not None
            event.status = OutboxEventStatus.PUBLISHED
            await session.commit()
        task.send(job_id=str(job.id))

    failed = await _wait_for_status(session_factory, job.id, JobStatus.FAILED)
    assert failed.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED
    assert failed.error_message == jobs_service.ERROR_MESSAGE_RETRIES_EXHAUSTED
    assert failed.completed_at is not None
    # Three global PostgreSQL attempts ran without the exhausted-handler actor.
    assert failed.attempt_count == jobs_service.MAX_ATTEMPTS


# --- P2 attempt-correlation: the exhausted message bridge on the real broker ---
#
# The finalizer correlates the exhausted message with the attempt it actually
# claimed via a stamp (dispatch id + owner token) the execution wrapper writes
# into the message options at claim time. These tests pin that bridge on the
# real broker: the stamp must survive the Retries middleware's forwarding and
# must be what decides between a genuine exhausted settlement and a stale
# no-op, including the two races the dispatch-only correlation left open.


async def _create_queued_job(session_factory: Any, organisation_id: uuid.UUID) -> uuid.UUID:
    """Create a durable ``queued`` job row directly (no task enqueued)."""
    async with session_factory() as session:
        job = Job(
            organisation_id=organisation_id,
            job_type="file.processing",
            status=JobStatus.QUEUED,
            progress=0,
            input_reference="file-1",
        )
        session.add(job)
        await session.commit()
        return job.id


def _enqueue_exhausted_message(
    broker: RedisBroker,
    job_id: uuid.UUID,
    *,
    dispatch_id: uuid.UUID | None,
    owner_token: uuid.UUID | None,
) -> None:
    """Enqueue an exhausted-handler message exactly as the Retries middleware would.

    The middleware calls ``target_actor.send(message.asdict(), retry_info)``
    after the last failed attempt, so the handler message carries the task
    message dict (with the wrapper's claim stamp in ``options``) and the retry
    info as its two positional arguments.
    """
    options: dict[str, Any] = {}
    if dispatch_id is not None and owner_token is not None:
        options = {
            "dispatch_id": str(dispatch_id),
            "owner_token": str(owner_token),
        }
    broker.enqueue(  # pyright: ignore[reportUnknownMemberType]
        dramatiq.Message(
            queue_name=jobs_tasks.HANDLER_QUEUE,
            actor_name=jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR,
            args=(
                {"kwargs": {"job_id": str(job_id)}, "options": options},
                {
                    "retries": jobs_service.MAX_ATTEMPTS - 1,
                    "max_retries": jobs_service.MAX_ATTEMPTS - 1,
                },
            ),
            kwargs={},
            options={},
        )
    )


def _stale_messages_count() -> float:
    """Current value of the stale-messages operator counter (public API)."""
    for metric in JOBS_STALE_MESSAGES_TOTAL.collect():
        for sample in metric.samples:
            return float(sample.value)
    return 0.0


async def _wait_for_stale_count(previous: float, *, timeout: float = 20.0) -> None:
    """Wait until the real finalizer acknowledged a message as stale."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _stale_messages_count() > previous:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("exhausted handler never processed the stale message")


async def test_durable_retry_does_not_depend_on_exhausted_handler(
    migrated_database: str, broker_and_worker: tuple[RedisBroker, Worker]
) -> None:
    """Redis loss before settlement cannot erase PostgreSQL's retry intent."""
    broker, _worker = broker_and_worker
    session_factory = _session_factory(migrated_database)
    organisation = await _create_org(session_factory)
    capture_actor_name = f"capture-exhausted-{uuid.uuid4().hex[:8]}"
    captured: dict[str, Any] = {}

    async def _run(job_id: str) -> None:
        job_id_uuid = uuid.UUID(job_id)

        async def _fail_transient(context: Any, session: Any) -> None:
            # CurrentMessage is visible inside the async actor and carries the
            # stamp the wrapper wrote at claim time.
            message = CurrentMessage.get_current_message()
            assert message is not None
            assert message.options.get("dispatch_id") == str(context.dispatch_id)
            assert message.options.get("owner_token") == str(context.owner_token)
            # Drop every broker key before the handler fails. The wrapper must
            # still record the next action because settlement touches only
            # PostgreSQL, not the exhausted callback or the current message.
            broker.flush_all()
            raise RuntimeError("storage temporarily unreachable")

        await jobs_execution.run_claimed(job_id=job_id_uuid, handler=_fail_transient)

    async def _capture_exhausted(message_dict: dict[str, Any], retry_info: dict[str, Any]) -> None:
        captured["message_dict"] = message_dict
        captured["retry_info"] = retry_info

    task = dramatiq.actor(
        queue_name=_QUEUE,
        max_retries=1,
        min_backoff=_EXHAUST_MIN_BACKOFF_MS,
        throws=(),
        on_retry_exhausted=capture_actor_name,
    )(_run)
    dramatiq.actor(
        actor_name=capture_actor_name,
        queue_name=jobs_tasks.HANDLER_QUEUE,
        max_retries=0,
        throws=(),
    )(_capture_exhausted)

    async with session_factory() as session:
        job = await jobs_service.schedule_job(
            session,
            organisation_id=organisation.id,
            job_type="file.processing",
            input_reference="file-1",
        )
        job_id = job.id
    task.send(job_id=str(job_id))  # the coordinator publishes

    deadline = time.monotonic() + 5.0
    row: Job | None = None
    while time.monotonic() < deadline:
        async with session_factory() as session:
            row = await session.get(Job, job_id)
        if row is not None and row.status is JobStatus.QUEUED and row.attempt_count == 1:
            break
        await asyncio.sleep(0.1)
    assert row is not None and row.status is JobStatus.QUEUED
    async with session_factory() as session:
        event = await session.get(OutboxEvent, row.dispatch_id)
        assert event is not None
        assert event.status is OutboxEventStatus.PENDING
    assert captured == {}


async def test_exhausted_message_with_superseded_token_is_stale(
    migrated_database: str, broker_and_worker: tuple[RedisBroker, Worker]
) -> None:
    """Broker race 1: a rotated token protects the newer queued attempt (P2).

    Attempt A of dispatch D exhausts while attempt B (a retry of the *same*
    dispatch, which rotated the owner token) sits queued with no live lease.
    A's exhausted message travels the real broker with A's stamp; the finalizer
    must acknowledge it without failing B.
    """
    broker, _worker = broker_and_worker
    session_factory = _session_factory(migrated_database)
    organisation = await _create_org(session_factory)
    job_id = await _create_queued_job(session_factory, organisation.id)

    async with session_factory() as session:
        claim_a = await jobs_service.claim_dispatch(session, job_id=job_id)
        assert claim_a.dispatch_id is not None
        assert claim_a.owner_token is not None
        await jobs_service.settle_retryable_failure(
            session, job_id=job_id, owner_token=claim_a.owner_token
        )
        row = await session.get(Job, job_id)
        assert row is not None and row.dispatch_id is not None
        retry_event = await session.get(OutboxEvent, row.dispatch_id)
        assert retry_event is not None
        retry_event.status = OutboxEventStatus.PUBLISHED
        retry_event.available_at = datetime.now(UTC)
        await session.commit()
        claim_b = await jobs_service.claim_dispatch(session, job_id=job_id)
        assert claim_b.dispatch_id != claim_a.dispatch_id
        assert claim_b.owner_token != claim_a.owner_token
        assert claim_b.owner_token is not None
        await jobs_service.settle_retryable_failure(
            session, job_id=job_id, owner_token=claim_b.owner_token
        )
        a_dispatch = claim_a.dispatch_id
        a_token = claim_a.owner_token
        assert a_dispatch is not None and a_token is not None

    baseline_stale = _stale_messages_count()
    _enqueue_exhausted_message(broker, job_id, dispatch_id=a_dispatch, owner_token=a_token)
    await _wait_for_stale_count(baseline_stale)

    async with session_factory() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        assert row.status == JobStatus.QUEUED
        assert row.error_code is None
        assert row.dispatch_id != a_dispatch


async def test_exhausted_message_without_stamp_is_stale_for_claimed_row(
    migrated_database: str, broker_and_worker: tuple[RedisBroker, Worker]
) -> None:
    """Broker race 2: a never-claimed duplicate cannot fail around a release (P2).

    A duplicate deferred on every attempt never claims, so its exhausted
    message carries no stamp. If the live owner released back to ``queued``
    just before the finalizer runs, the stamp-less message must be a stale
    no-op: the row now carries an owner credential and only the attempt that
    stamped it may settle it.
    """
    broker, _worker = broker_and_worker
    session_factory = _session_factory(migrated_database)
    organisation = await _create_org(session_factory)
    job_id = await _create_queued_job(session_factory, organisation.id)

    async with session_factory() as session:
        claim = await jobs_service.claim_dispatch(session, job_id=job_id)
        assert claim.owner_token is not None
        await jobs_service.settle_retryable_failure(
            session, job_id=job_id, owner_token=claim.owner_token
        )

    baseline_stale = _stale_messages_count()
    _enqueue_exhausted_message(broker, job_id, dispatch_id=None, owner_token=None)
    await _wait_for_stale_count(baseline_stale)

    async with session_factory() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        assert row.status == JobStatus.QUEUED
        assert row.error_code is None


async def test_exhausted_message_settles_legacy_unclaimed_row(
    migrated_database: str, broker_and_worker: tuple[RedisBroker, Worker]
) -> None:
    """Broker positive control: the stamp-less legacy settlement still works (P2).

    A row that has never been claimed (no dispatch id, no owner credential) is
    the explicit legacy case: a stamp-less exhausted message settles it failed
    through the real broker, so pre-ownership jobs still record their
    exhausted failure.
    """
    broker, _worker = broker_and_worker
    session_factory = _session_factory(migrated_database)
    organisation = await _create_org(session_factory)
    job_id = await _create_queued_job(session_factory, organisation.id)

    _enqueue_exhausted_message(broker, job_id, dispatch_id=None, owner_token=None)
    settled = await _wait_for_status(session_factory, job_id, JobStatus.FAILED)
    assert settled.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED
    assert settled.error_message == jobs_service.ERROR_MESSAGE_RETRIES_EXHAUSTED


async def test_upload_complete_process_job_runs_on_real_broker(
    migrated_database: str, broker_and_worker: tuple[RedisBroker, Worker]
) -> None:
    """Acceptance §5.8: the files<->jobs journey against real Redis + Postgres.

    The Scope §6.5 acceptance journey runs against the real broker and worker:
    intent -> signed PUT (fake adapter) -> complete (which writes the durable
    row and enqueues the re-declared ``process_file`` task) -> the worker runs
    it -> the file is ``ready`` and the job ``succeeded`` with progress 100.
    """
    session_factory = _session_factory(migrated_database)
    organisation = await _create_org(session_factory)
    process_task = _make_process_file_task()
    content = b"the bytes that were uploaded"

    async with session_factory() as session:
        file, signed_url = await files_service.create_upload_intent(
            session,
            organisation_id=organisation.id,
            original_filename="report.pdf",
            content_type="application/pdf",
            size_bytes=len(content),
            actor_user_id=None,
        )
        assert signed_url.method == "PUT"
        fake = cast(FakeObjectStorage, get_storage())
        await fake.put(file.object_key, content)
        completed, job_id = await files_service.complete_upload(
            session,
            organisation_id=organisation.id,
            file_id=file.id,
        )
        assert completed.status == FileStatus.UPLOADED
        assert job_id is not None
        queued = await session.get(Job, job_id)
        assert queued is not None
        assert queued.status == JobStatus.QUEUED
        assert queued.job_type == files_tasks.JOB_TYPE_FILE_PROCESSING

    process_task.send(job_id=str(job_id))  # the coordinator publishes
    finished = await _wait_for_status(session_factory, job_id, JobStatus.SUCCEEDED)
    assert finished.progress == 100
    assert finished.result_reference == str(file.id)
    assert finished.error_code is None

    async with session_factory() as session:
        ready = await files_service.get_file(
            session, organisation_id=organisation.id, file_id=file.id
        )
        assert ready.status == FileStatus.READY


# --- Scope §6.4: the files -> notifications -> email loop on real Redis ---


async def test_file_ready_loop_runs_on_real_broker(
    migrated_database: str,
    broker_and_worker: tuple[RedisBroker, Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance §5.5/§5.6: the full §6.4 loop on real Redis + Postgres.

    The Scope §6.4 integration journey against the real broker and worker: a
    file uploaded by an internal user completes, ``process_file`` runs on the
    namespaced Redis broker and leaves the file ``ready``, an in-app
    ``file.ready`` notification exists for the uploader, and the
    ``notification.email`` delivery job was enqueued with the delivery id as
    its ``input_reference``; the real email handler (fake provider, pinned by
    conftest) then advances the delivery to ``succeeded`` and records
    ``provider_message_id``. The notification enqueue happens inside the worker
    on the same broker, so the loop is proven end to end.
    """
    session_factory = _session_factory(migrated_database)
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    task_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(notifications_tasks, "async_session_factory", task_factory)
    # The email task drives its domain work through the shared execution
    # wrapper (plan P2), which opens its sessions through the wrapper's module
    # factory; point it at the same per-test NullPool engine so the direct
    # handler call below stays on this test's event loop.
    monkeypatch.setattr(jobs_execution, "async_session_factory", task_factory)
    organisation = await _create_org(session_factory)
    async with session_factory() as session:
        uploader = User(
            workos_user_id=f"user_{uuid.uuid4().hex}",
            email="uploader@example.com",
            name="Ada Lovelace",
        )
        session.add(uploader)
        await session.commit()
        uploader_id = uploader.id
        org_id = organisation.id

    process_task = _make_process_file_task()
    content = b"the bytes that were uploaded"
    async with session_factory() as session:
        file, signed_url = await files_service.create_upload_intent(
            session,
            organisation_id=organisation.id,
            original_filename="report.pdf",
            content_type="application/pdf",
            size_bytes=len(content),
            actor_user_id=uploader_id,
        )
        assert signed_url.method == "PUT"
        fake = cast(FakeObjectStorage, get_storage())
        await fake.put(file.object_key, content)
        completed, job_id = await files_service.complete_upload(
            session,
            organisation_id=organisation.id,
            file_id=file.id,
        )
        assert completed.status == FileStatus.UPLOADED
        assert job_id is not None
        file_id = file.id

    process_task.send(job_id=str(job_id))  # the coordinator publishes
    finished = await _wait_for_status(session_factory, job_id, JobStatus.SUCCEEDED)
    assert finished.progress == 100

    try:
        async with session_factory() as session:
            ready = await files_service.get_file(
                session, organisation_id=organisation.id, file_id=file_id
            )
            assert ready.status == FileStatus.READY

            notification = await session.scalar(
                select(Notification).where(
                    Notification.organisation_id == org_id,
                    Notification.user_id == uploader_id,
                    Notification.type == "file.ready",
                )
            )
            assert notification is not None
            assert notification.resource_type == "file"
            assert notification.resource_id == str(file_id)

            delivery = await session.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.notification_id == notification.id
                )
            )
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.QUEUED
            email_job = await session.scalar(
                select(Job).where(
                    Job.job_type == notifications_tasks.JOB_TYPE_NOTIFICATION_EMAIL,
                    Job.input_reference == str(delivery.id),
                )
            )
            assert email_job is not None
            email_job_id = email_job.id

        # Drive the durable email job on this loop (fake provider): the
        # delivery succeeds and the provider message id is recorded.
        await notifications_tasks.send_notification_email(str(email_job_id))

        async with session_factory() as session:
            delivery = await session.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.notification_id == notification.id
                )
            )
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.SUCCEEDED
            assert delivery.provider_message_id is not None
            assert delivery.provider_message_id.startswith("fake-")
            email_job = await session.get(Job, email_job_id)
            assert email_job is not None
            assert email_job.status == JobStatus.SUCCEEDED
    finally:
        await engine.dispose()


# --- P4: real-broker notification.email eventual-success and exhaustion journeys ---


async def test_notification_email_eventual_success_on_real_broker(
    migrated_database: str,
    broker_and_worker: tuple[RedisBroker, Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P4: a transiently failing notification email eventually succeeds on real Redis.

    The notification email task is declared on the test broker with its full
    retry policy. The provider fails once then succeeds; the delivery advances
    from ``queued`` through ``running``/``queued`` (retry release) to
    ``succeeded``, and the job reaches ``succeeded`` with the provider message
    id. The real broker and real exhausted handler are exercised end to end.
    """
    from app.modules.jobs import execution as jobs_execution

    session_factory = _session_factory(migrated_database)
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    task_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(notifications_tasks, "async_session_factory", task_factory)
    monkeypatch.setattr(jobs_execution, "async_session_factory", task_factory)

    call_count = 0

    class _EventuallySucceedsProvider:
        async def send_email(self, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            from app.email import EMAIL_DELIVERY_STATUS_SENT
            from app.email.base import TransientEmailSendError
            from app.email.types import EmailDeliveryResult

            if call_count == 1:
                raise TransientEmailSendError("temporary relay outage")

            return EmailDeliveryResult(
                provider_message_id="provider-123",
                status=EMAIL_DELIVERY_STATUS_SENT,
            )

    monkeypatch.setattr(notifications_tasks, "get_email_provider", _EventuallySucceedsProvider)

    try:
        organisation = await _create_org(session_factory)
        async with session_factory() as session:
            user = User(
                workos_user_id=f"user_{uuid.uuid4().hex}",
                email="ada@example.com",
                name="Ada Lovelace",
            )
            session.add(user)
            await session.commit()
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=organisation.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job_id = job.id
            delivery_id = delivery.id

        # Re-declare the notification actor on this broker so the message lands
        # in the test namespace.
        email_task = dramatiq.actor(
            queue_name=_QUEUE,
            **jobs_service.retry_policy(),
        )(notifications_tasks.send_notification_email)
        email_task.send(job_id=str(job_id))
        await _publish_durable_retry(session_factory, email_task, job_id, after_attempt=1)

        finished = await _wait_for_status(session_factory, job_id, JobStatus.SUCCEEDED)
        assert finished.result_reference == "provider-123"

        async with session_factory() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.SUCCEEDED
            assert delivery.provider_message_id == "provider-123"
    finally:
        await engine.dispose()


async def test_notification_email_acceptance_unknown_on_real_broker(
    migrated_database: str,
    broker_and_worker: tuple[RedisBroker, Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambiguous send is terminal and duplicate broker delivery cannot resend it."""
    from app.email.base import AcceptanceUnknownEmailSendError

    session_factory = _session_factory(migrated_database)
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    task_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(notifications_tasks, "async_session_factory", task_factory)
    monkeypatch.setattr(jobs_execution, "async_session_factory", task_factory)
    calls = 0

    class _AmbiguousProvider:
        async def send_email(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            raise AcceptanceUnknownEmailSendError("connection lost after submission")

    monkeypatch.setattr(notifications_tasks, "get_email_provider", _AmbiguousProvider)
    try:
        organisation = await _create_org(session_factory)
        async with session_factory() as session:
            user = User(
                workos_user_id=f"user_{uuid.uuid4().hex}",
                email="ada@example.com",
                name="Ada Lovelace",
            )
            session.add(user)
            await session.commit()
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=organisation.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job_id = job.id
            delivery_id = delivery.id

        email_task = dramatiq.actor(
            queue_name=_QUEUE,
            **jobs_service.retry_policy(),
        )(notifications_tasks.send_notification_email)
        email_task.send(job_id=str(job_id))
        failed = await _wait_for_status(session_factory, job_id, JobStatus.FAILED)
        assert failed.error_code == "email_delivery_acceptance_unknown"

        email_task.send(job_id=str(job_id))
        await asyncio.sleep(0.2)
        assert calls == 1
        async with session_factory() as session:
            delivery_row = await session.get(NotificationDelivery, delivery_id)
            assert delivery_row is not None
            assert delivery_row.status is NotificationDeliveryStatus.ATTENTION_REQUIRED
            assert delivery_row.error_code == "acceptance_unknown"
    finally:
        await engine.dispose()


async def test_notification_email_exhaustion_fails_delivery_on_real_broker(
    migrated_database: str,
    broker_and_worker: tuple[RedisBroker, Worker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P4: exhausted notification retries leave both job and delivery failed.

    The notification email task always fails transiently on the real broker.
    After ``MAX_ATTEMPTS`` the exhausted handler runs the registered
    ``notification.email`` hook, which marks the delivery ``failed`` with an
    audit row. Both the job and delivery are consistently terminal.
    """
    from app.email.base import TransientEmailSendError
    from app.modules.jobs import execution as jobs_execution

    session_factory = _session_factory(migrated_database)
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    task_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(notifications_tasks, "async_session_factory", task_factory)
    monkeypatch.setattr(jobs_execution, "async_session_factory", task_factory)

    class _AlwaysTransientProvider:
        async def send_email(self, **kwargs: Any) -> Any:
            raise TransientEmailSendError("relay permanently down")

    monkeypatch.setattr(notifications_tasks, "get_email_provider", _AlwaysTransientProvider)

    try:
        organisation = await _create_org(session_factory)
        async with session_factory() as session:
            user = User(
                workos_user_id=f"user_{uuid.uuid4().hex}",
                email="ada@example.com",
                name="Ada Lovelace",
            )
            session.add(user)
            await session.commit()
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=organisation.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job_id = job.id
            delivery_id = delivery.id
            notification_id = _notification.id

        email_task = dramatiq.actor(
            queue_name=_QUEUE,
            **jobs_service.retry_policy(),
        )(notifications_tasks.send_notification_email)
        email_task.send(job_id=str(job_id))
        for attempt_number in range(1, jobs_service.MAX_ATTEMPTS):
            await _publish_durable_retry(
                session_factory,
                email_task,
                job_id,
                after_attempt=attempt_number,
            )

        failed_job = await _wait_for_status(session_factory, job_id, JobStatus.FAILED)
        assert failed_job.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED

        async with session_factory() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.FAILED

            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "notification.delivery_failed",
                    AuditEvent.resource_id == str(notification_id),
                )
            )
            assert audit is not None
            assert audit.organisation_id == organisation.id
            assert audit.event_metadata["error_code"] == (
                notifications_service.DELIVERY_ERROR_SAFE_RETRIES_EXHAUSTED
            )
    finally:
        await engine.dispose()
