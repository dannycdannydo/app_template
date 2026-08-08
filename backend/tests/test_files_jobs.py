"""File-processing job tests (Scope §6.5, blueprint §17, §18).

The ``process_file`` task closes the files<->jobs loop: intent -> signed PUT
(fake adapter) -> complete -> the job runs -> the file is ``ready`` and the
durable job ``succeeded`` with progress 100. These tests run the real task
handler against the real database (migrated to head, same skip pattern as
``test_files_db.py``) on an in-process StubBroker worker — the stub-broker
half of the acceptance journey. The real-broker (Redis) half lives in
``test_jobs_broker.py``, which reuses the same handler.

Two layers are proven here:

- the task semantics: progress 0 -> 100, uploaded -> processing -> ready,
  the failure path (the object vanished or its size drifted between
  completion and processing -> file ``failed`` + job ``failed`` with an
  ``error_code``), idempotent re-runs, and the org-scoped job list/detail
  contract at the SQL level;
- the journey end to end: ``complete_upload`` enqueues the durable job and
  returns its id, and the worker's attempt drives the file to ``ready``.

The handler is re-declared bound to the test's StubBroker (the same seam
``test_jobs_broker.py`` uses): ``Actor.send()`` enqueues on the actor's own
broker, so the module-level actor — bound to the default broker when the
module was first imported — must not be the one the test worker consumes.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import dramatiq
import pytest
from alembic import command
from alembic.config import Config
from dramatiq.brokers.stub import StubBroker
from dramatiq.worker import Worker
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import NotFoundError
from app.modules.audit.models import AuditEvent
from app.modules.files import service as files_service
from app.modules.files import tasks as files_tasks
from app.modules.files.models import File, FileStatus
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatus
from app.modules.organisations.models import Organisation
from app.storage import FakeObjectStorage, get_storage

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_QUEUE = "test-files-jobs"


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
async def broker_and_worker() -> AsyncIterator[tuple[StubBroker, Worker, Any]]:
    """A StubBroker + in-process Worker running the real ``process_file`` handler.

    The middleware stack is the same factory the worker process uses, so the
    async task runs on the AsyncIO event-loop thread exactly as in production.
    The actor is re-declared bound to this broker; the module-level actor keeps
    its default-broker binding and is only used to enqueue *inert* messages
    the failure-path tests can race against.
    """
    from app.workers import worker_middleware

    broker = StubBroker(middleware=worker_middleware())
    dramatiq.set_broker(broker)
    process_task = dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(
        files_tasks.process_file
    )
    worker = Worker(broker, worker_timeout=100, worker_threads=2)
    worker.start()
    yield broker, worker, process_task
    worker.stop()
    broker.flush_all()
    # The task handler runs on the worker's AsyncIO event-loop thread but uses
    # the process-wide engine (``async_session_factory``). Dispose the pool so
    # no loop-bound connection outlives this test's worker: the next test's
    # worker runs on a fresh loop and would otherwise reuse a connection
    # "attached to a different loop", failing every task.
    from app.db.session import engine

    await engine.dispose()


def _session_factory(database_url: str) -> Any:
    """A NullPool session factory safe to share across event loops."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


def _fake_storage() -> FakeObjectStorage:
    """Return the process-wide fake adapter as its concrete type (has ``put``)."""
    from typing import cast as typing_cast

    return typing_cast(FakeObjectStorage, get_storage())


async def _create_org(session: AsyncSession, name: str) -> Organisation:
    organisation = Organisation(name=name)
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


async def _audit_count(session_factory: Any, *, action: str, resource_id: str) -> int:
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == action, AuditEvent.resource_id == resource_id)
        )
        return count or 0


async def _upload_round_trip(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    content: bytes = b"the bytes that were uploaded",
    process_task: Any = None,
) -> tuple[File, uuid.UUID]:
    """Run intent -> direct PUT -> complete, returning the file and job id."""
    file, signed_url = await files_service.create_upload_intent(
        session,
        organisation_id=organisation_id,
        original_filename="report.pdf",
        content_type="application/pdf",
        size_bytes=len(content),
        actor_user_id=None,
    )
    assert signed_url.method == "PUT"
    await _fake_storage().put(file.object_key, content)
    completed, job_id = await files_service.complete_upload(
        session,
        organisation_id=organisation_id,
        file_id=file.id,
        process_task=process_task,
    )
    assert completed.status == FileStatus.UPLOADED
    assert job_id is not None
    return completed, job_id


async def test_complete_enqueues_job_and_worker_drives_file_to_ready(
    migrated_database: str, broker_and_worker: tuple[StubBroker, Worker, Any]
) -> None:
    """Acceptance §5.8: intent -> PUT -> complete -> job -> file ready."""
    broker, _worker, process_task = broker_and_worker
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session, "File Jobs Ltd")

        file, job_id = await _upload_round_trip(session, organisation.id, process_task=process_task)
        # The durable row was written queued before the worker picked it up.
        queued = await session.get(Job, job_id)
        assert queued is not None
        assert queued.status == JobStatus.QUEUED
        assert queued.job_type == files_tasks.JOB_TYPE_FILE_PROCESSING
        assert queued.input_reference == str(file.id)
        assert queued.organisation_id == organisation.id

    broker.join(_QUEUE, timeout=10000)
    finished = await _wait_for_status(session_factory, job_id, JobStatus.SUCCEEDED)
    assert finished.progress == 100
    assert finished.result_reference == str(file.id)
    assert finished.attempt_count == 1
    assert finished.error_code is None

    async with session_factory() as session:
        ready = await files_service.get_file(
            session, organisation_id=organisation.id, file_id=file.id
        )
        assert ready.status == FileStatus.READY

    assert (
        await _audit_count(session_factory, action="file.processing", resource_id=str(file.id)) == 1
    )
    assert await _audit_count(session_factory, action="file.ready", resource_id=str(file.id)) == 1
    assert await _audit_count(session_factory, action="job.succeeded", resource_id=str(job_id)) == 1


async def test_processing_failure_fails_file_and_job_with_error_code(
    migrated_database: str, broker_and_worker: tuple[StubBroker, Worker, Any]
) -> None:
    """Failure path: an object that vanished before processing fails both.

    The job's worker-side verification (BP §17 second check) finds the object
    missing -> the file is marked ``failed`` and the durable job ``failed``
    with ``file_verification_failed`` (a permanent error, never retried).
    """
    _broker, _worker, process_task = broker_and_worker
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session, "File Jobs Failure Ltd")
        # process_task=None enqueues on the module-level actor (default broker,
        # inert here), so the durable row is written without a message this
        # worker can consume before the object is tampered with.
        file, job_id = await _upload_round_trip(session, organisation.id, process_task=None)
        await _fake_storage().delete_object(file.object_key)  # bytes vanished

    process_task.send(job_id=str(job_id))  # enqueue the attempt on this broker
    await _wait_for_status(session_factory, job_id, JobStatus.FAILED)

    async with session_factory() as session:
        failed_file = await files_service.get_file(
            session, organisation_id=organisation.id, file_id=file.id
        )
        assert failed_file.status == FileStatus.FAILED
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error_code == files_tasks.ERROR_CODE_VERIFICATION_FAILED
        assert job.error_message is not None

    assert (
        await _audit_count(session_factory, action="file.upload_failed", resource_id=str(file.id))
        == 1
    )
    assert await _audit_count(session_factory, action="job.failed", resource_id=str(job_id)) == 1


async def test_processing_size_mismatch_fails_file(
    migrated_database: str, broker_and_worker: tuple[StubBroker, Worker, Any]
) -> None:
    """Failure path (acceptance §5.5/§5.8): a size drift fails the file.

    The stored object is replaced with different-size content between
    completion and processing; the worker-side head finds the size mismatch
    and marks the file failed (``reason=size_mismatch`` in the audit row).
    """
    _broker, _worker, process_task = broker_and_worker
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session, "File Jobs Mismatch Ltd")
        content = b"original bytes"
        file, job_id = await _upload_round_trip(
            session, organisation.id, content=content, process_task=None
        )
        # Replace the object with content of a different size, simulating a
        # race or tampering between completion and processing. The fake enforces
        # the declared size at put time, so re-declare the key first (the same
        # seam test_files_db.py uses for the completion-time mismatch).
        await _fake_storage().create_upload_url(
            file_id=file.id,
            object_key=file.object_key,
            content_type="application/pdf",
            size_bytes=6,
        )
        await _fake_storage().put(file.object_key, b"six...")

    process_task.send(job_id=str(job_id))
    await _wait_for_status(session_factory, job_id, JobStatus.FAILED)

    async with session_factory() as session:
        failed_file = await files_service.get_file(
            session, organisation_id=organisation.id, file_id=file.id
        )
        assert failed_file.status == FileStatus.FAILED
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error_code == files_tasks.ERROR_CODE_VERIFICATION_FAILED

    async with session_factory() as session:
        failed_event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "file.upload_failed",
                AuditEvent.resource_id == str(file.id),
            )
        )
        assert failed_event is not None
        assert failed_event.event_metadata["reason"] == "size_mismatch"


async def test_redelivered_message_of_finished_job_is_a_noop(
    migrated_database: str, broker_and_worker: tuple[StubBroker, Worker, Any]
) -> None:
    """Idempotency: a re-delivered message for a succeeded job changes nothing.

    Terminal states are never re-run (acceptance §5.7): a second message with
    the same job id leaves the job succeeded, the file ready, and writes no
    second audit rows.
    """
    broker, _worker, process_task = broker_and_worker
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _create_org(session, "File Jobs Idempotent Ltd")
        file, job_id = await _upload_round_trip(session, organisation.id, process_task=process_task)

    broker.join(_QUEUE, timeout=10000)
    await _wait_for_status(session_factory, job_id, JobStatus.SUCCEEDED)

    process_task.send(job_id=str(job_id))  # simulate a re-delivered message
    broker.join(_QUEUE, timeout=10000)
    await asyncio.sleep(0.5)  # give the no-op attempt time to land

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED
        assert job.attempt_count == 1  # the no-op never called mark_running
        file_row = await session.get(File, file.id)
        assert file_row is not None
        assert file_row.status == FileStatus.READY

    assert await _audit_count(session_factory, action="file.ready", resource_id=str(file.id)) == 1
    assert await _audit_count(session_factory, action="job.succeeded", resource_id=str(job_id)) == 1


def _make_noop_task() -> Any:
    """A task that writes nothing; used to create durable rows without work."""

    def _noop(job_id: str) -> None:
        return None

    return dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(_noop)


async def test_job_list_and_detail_are_org_scoped(
    migrated_database: str, broker_and_worker: tuple[StubBroker, Worker, Any]
) -> None:
    """Acceptance §5.7: org-scoping, status/job_type filters and the 404 rule."""
    _broker, _worker, _process_task = broker_and_worker
    session_factory = _session_factory(migrated_database)
    noop = _make_noop_task()
    async with session_factory() as session:
        org_a = await _create_org(session, "Jobs Scoping A Ltd")
        org_b = await _create_org(session, "Jobs Scoping B Ltd")
        job_a_1 = await jobs_service.create_and_enqueue(
            session,
            organisation_id=org_a.id,
            job_type="file.processing",
            input_reference="file-a-1",
            task=noop,
        )
        job_a_2 = await jobs_service.create_and_enqueue(
            session,
            organisation_id=org_a.id,
            job_type="file.processing",
            input_reference="file-a-2",
            task=noop,
        )
        job_b = await jobs_service.create_and_enqueue(
            session,
            organisation_id=org_b.id,
            job_type="file.processing",
            input_reference="file-b-1",
            task=noop,
        )
        # Move one of org A's jobs out of the default queued state so the
        # status filter has something to select (the no-op task never runs).
        await session.execute(
            update(Job).where(Job.id == job_a_2.id).values(status=JobStatus.RUNNING)
        )
        await session.commit()

        # List: only the caller's organisation's jobs, newest first.
        a_jobs, a_total = await jobs_service.list_jobs(
            session, organisation_id=org_a.id, page=1, page_size=50
        )
        assert a_total == 2
        assert {job.id for job in a_jobs} == {job_a_1.id, job_a_2.id}
        b_jobs, b_total = await jobs_service.list_jobs(
            session, organisation_id=org_b.id, page=1, page_size=50
        )
        assert b_total == 1
        assert [job.id for job in b_jobs] == [job_b.id]

        # Status and job_type filters select at the SQL level.
        running, running_total = await jobs_service.list_jobs(
            session,
            organisation_id=org_a.id,
            page=1,
            page_size=50,
            status=JobStatus.RUNNING,
        )
        assert running_total == 1
        assert [job.id for job in running] == [job_a_2.id]
        typed, typed_total = await jobs_service.list_jobs(
            session,
            organisation_id=org_a.id,
            page=1,
            page_size=50,
            job_type="not.a.job.type",
        )
        assert typed_total == 0
        assert typed == []

        # Detail: own org resolves, another org's job id is a 404.
        fetched = await jobs_service.get_job(session, organisation_id=org_a.id, job_id=job_a_1.id)
        assert fetched.id == job_a_1.id
        with pytest.raises(NotFoundError):
            await jobs_service.get_job(session, organisation_id=org_b.id, job_id=job_a_1.id)
