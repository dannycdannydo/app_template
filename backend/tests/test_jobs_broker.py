"""Real-broker integration tests for the durable job pipeline (Scope §6.4).

The service lifecycle is proven against the real database in
``test_jobs_db.py``, but its broker is an in-memory stub, so the wiring
between ``create_and_enqueue``, a real Redis broker and a real worker thread
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
(``app.workers.worker_middleware``), so the async tasks run exactly as they
do in production; only the broker (namespace) and the per-test backoff differ.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import dramatiq
import pytest
from alembic import command
from alembic.config import Config
from dramatiq.brokers.redis import RedisBroker
from dramatiq.worker import Worker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.files import service as files_service
from app.modules.files import tasks as files_tasks
from app.modules.files.models import FileStatus
from app.modules.jobs import service as jobs_service
from app.modules.jobs import tasks as jobs_tasks
from app.modules.jobs.models import Job, JobStatus
from app.modules.organisations.models import Organisation
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
    stack comes from ``app.workers.worker_middleware`` (the same factory the
    ``dramatiq app.workers`` process uses), so async tasks run on the AsyncIO
    event-loop thread exactly as in production.
    """
    from app.workers import worker_middleware

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
    yield broker, worker
    worker.stop()
    broker.flush_all()
    # The handlers (and Scope §6.5's process_file) run on the worker's AsyncIO
    # event-loop thread but use the process-wide engine (async_session_factory).
    # Dispose the pool so no loop-bound connection outlives this test's worker:
    # the next test's worker runs on a fresh loop and would otherwise reuse a
    # connection "attached to a different loop", failing every task.
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
    """An async task that drives a job through running -> succeeded."""

    async def _run(job_id: str) -> None:
        async with session_factory() as session:
            job_id_uuid = uuid.UUID(job_id)
            await jobs_service.mark_running(session, job_id=job_id_uuid)
            await jobs_service.update_progress(session, job_id=job_id_uuid, progress=50)
            await jobs_service.update_progress(session, job_id=job_id_uuid, progress=100)
            await jobs_service.succeed(session, job_id=job_id_uuid, result_reference="file-1")

    return dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(_run)


def _make_transient_failure_task(session_factory: Any) -> Any:
    """An async task that marks the job running, then fails transiently.

    Marking running first mirrors what every real task does (Scope §6.5's
    ``process_file`` starts with ``mark_running``), so the durable
    ``attempt_count`` reflects all ``MAX_ATTEMPTS`` attempts.
    """

    async def _run(job_id: str) -> None:
        async with session_factory() as session:
            job_id_uuid = uuid.UUID(job_id)
            await jobs_service.mark_running(session, job_id=job_id_uuid)
        raise RuntimeError("storage temporarily unreachable")

    options: dict[str, Any] = {
        **jobs_service.retry_policy(),
        "min_backoff": _EXHAUST_MIN_BACKOFF_MS,
    }
    return dramatiq.actor(queue_name=_QUEUE, **options)(_run)


def _make_process_file_task() -> Any:
    """The real ``process_file`` handler re-declared bound to this broker.

    ``files_service.complete_upload`` enqueues whatever actor it is passed
    (defaulting to the module-level one, which is bound to the default broker
    from import time); passing the re-declared actor makes the journey test's
    enqueue land on the namespaced Redis broker this worker consumes.
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


async def test_worker_completes_job_lifecycle_with_progress(
    migrated_database: str, broker_and_worker: tuple[RedisBroker, Worker]
) -> None:
    """Acceptance §5.6/§5.8: enqueue -> worker -> running with progress -> succeeded."""
    session_factory = _session_factory(migrated_database)
    organisation = await _create_org(session_factory)
    task = _make_round_trip_task(session_factory)
    async with session_factory() as session:
        job = await jobs_service.create_and_enqueue(
            session,
            organisation_id=organisation.id,
            job_type="file.processing",
            input_reference="file-1",
            actor_user_id=None,
            task=task,
        )

    finished = await _wait_for_status(session_factory, job.id, JobStatus.SUCCEEDED)
    assert finished.progress == 100
    assert finished.attempt_count == 1
    assert finished.started_at is not None
    assert finished.completed_at is not None
    assert finished.result_reference == "file-1"
    assert finished.error_code is None


async def test_transient_exhaustion_records_failed_status(
    migrated_database: str, broker_and_worker: tuple[RedisBroker, Worker]
) -> None:
    """Acceptance §5.6: exhausted retries leave a failed row, never a running one."""
    session_factory = _session_factory(migrated_database)
    organisation = await _create_org(session_factory)
    task = _make_transient_failure_task(session_factory)
    async with session_factory() as session:
        job = await jobs_service.create_and_enqueue(
            session,
            organisation_id=organisation.id,
            job_type="file.processing",
            input_reference="file-1",
            task=task,
        )

    failed = await _wait_for_status(session_factory, job.id, JobStatus.FAILED)
    assert failed.error_code == jobs_service.ERROR_CODE_RETRIES_EXHAUSTED
    assert failed.error_message == jobs_service.ERROR_MESSAGE_RETRIES_EXHAUSTED
    assert failed.completed_at is not None
    # Three attempts ran (one per MAX_ATTEMPTS) before the handler recorded it.
    assert failed.attempt_count == jobs_service.MAX_ATTEMPTS


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
            process_task=process_task,
        )
        assert completed.status == FileStatus.UPLOADED
        assert job_id is not None
        queued = await session.get(Job, job_id)
        assert queued is not None
        assert queued.status == JobStatus.QUEUED
        assert queued.job_type == files_tasks.JOB_TYPE_FILE_PROCESSING

    finished = await _wait_for_status(session_factory, job_id, JobStatus.SUCCEEDED)
    assert finished.progress == 100
    assert finished.result_reference == str(file.id)
    assert finished.error_code is None

    async with session_factory() as session:
        ready = await files_service.get_file(
            session, organisation_id=organisation.id, file_id=file.id
        )
        assert ready.status == FileStatus.READY
