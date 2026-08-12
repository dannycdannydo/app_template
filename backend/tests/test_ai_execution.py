"""Durable ``ai.execute`` job tests (v0.7 Scope §6.6, blueprint §18, ADR-0017).

These tests run the real ``execute_ai_task`` handler against the real database
(migrated to head, same skip pattern as ``test_files_jobs.py``) on an in-process
StubBroker worker. They prove the §6.6 contract end to end:

- document-scale input enqueues a durable job whose message carries only the
  job id (no file bytes), and the worker drives it to ``succeeded`` with a
  settled ``ai_requests`` row and an audit event;
- the storage reference is resolved to a provider-neutral attachment by the
  service (not decoded to text by the worker), so binary documents (PDF/image)
  succeed through the document-capable model path (v0.7 Scope §2/§5.1);
- a re-delivered message reconciles to the winning attempt's terminal state
  instead of re-dispatching (idempotency, no double-charge, no duplicate
  output), including a multi-attempt success where attempt 1 fails and a later
  attempt wins (v0.7 Scope §6.4/§6.6);
- a vanished object fails the job permanently before dispatch; and
- the request row is org-scoped (a foreign row is invisible).

The handler is re-declared bound to the test's StubBroker (the same seam
``test_files_jobs.py`` uses).
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.execution import (
    JOB_TYPE_AI_EXECUTE,
    execute_ai_task,
    request_id_for_job,
)
from app.ai.persistence.models import AIRequestRecord, AIRequestStatus
from app.ai.persistence.service import create_default_settings
from app.modules.audit.models import AuditEvent
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatus
from app.modules.organisations.models import Organisation
from app.modules.users.models import User
from app.storage import FakeObjectStorage, get_storage

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_QUEUE = "test-ai-execute"


def _database_reachable(database_url: str) -> bool:
    async def _probe() -> bool:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(__import__("sqlalchemy").text("SELECT 1"))
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
    """A StubBroker + in-process Worker running the real ``execute_ai_task``."""
    from app.broker import worker_middleware

    broker = StubBroker(middleware=worker_middleware())
    dramatiq.set_broker(broker)
    ai_task = dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(execute_ai_task)
    worker = Worker(broker, worker_timeout=100, worker_threads=2)
    worker.start()
    yield broker, worker, ai_task
    worker.stop()
    broker.flush_all()
    from app.db.session import engine

    await engine.dispose()


def _session_factory(database_url: str) -> Any:
    engine = create_async_engine(database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


def _fake_storage() -> FakeObjectStorage:
    from typing import cast as typing_cast

    return typing_cast(FakeObjectStorage, get_storage())


async def _seed_organisation(session: AsyncSession) -> Organisation:
    organisation = Organisation(name=f"AI Job Org {uuid.uuid4().hex[:8]}")
    session.add(organisation)
    await session.commit()
    return organisation


async def _enable_ai(session: AsyncSession, organisation_id: uuid.UUID) -> None:
    settings_row = await create_default_settings(session, organisation_id=organisation_id)
    settings_row.enabled = True
    await session.commit()


async def _seed_user(session: AsyncSession) -> User:
    user = User(
        workos_user_id=f"ai_user_{uuid.uuid4().hex[:8]}",
        email="ai-demo@example.com",
        name="AI Demo User",
    )
    session.add(user)
    await session.commit()
    return user


async def _put_text_document(
    organisation_id: uuid.UUID, content: str = "A non-sensitive lease fixture."
) -> str:
    """Put a text object in the organisation's AI scratch namespace; return its key."""
    key = f"organisations/{organisation_id}/ai/scratch/doc-{uuid.uuid4().hex[:8]}.txt"
    await _fake_storage().put(key, content.encode("utf-8"), content_type="text/plain")
    return key


async def _enqueue(
    session_factory: Any,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    storage_key: str,
    ai_task: Any,
) -> Job:
    """Write the durable row and enqueue the test-bound actor on its broker."""
    async with session_factory() as session:
        return await jobs_service.create_and_enqueue(
            session,
            organisation_id=organisation_id,
            job_type=JOB_TYPE_AI_EXECUTE,
            input_reference=storage_key,
            actor_user_id=user_id,
            task=ai_task,
        )


async def _wait_for_status(
    session_factory: Any, job_id: uuid.UUID, expected: JobStatus, *, timeout: float = 20.0
) -> Job:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with session_factory() as session:
            job = await session.get(Job, job_id)
            if job is not None and job.status == expected:
                return job
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} never reached {expected.value!r} within {timeout}s")


async def _request_row(
    session_factory: Any, organisation_id: uuid.UUID, request_id: str
) -> AIRequestRecord | None:
    async with session_factory() as session:
        return await session.scalar(
            select(AIRequestRecord).where(
                AIRequestRecord.organisation_id == organisation_id,
                AIRequestRecord.request_id == request_id,
                AIRequestRecord.attempt_number == 1,
            )
        )


async def _audit_count(session_factory: Any, *, resource_id: str, action: str) -> int:
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == action, AuditEvent.resource_id == resource_id)
        )
        return count or 0


async def test_worker_classifies_text_document_and_records_request(
    migrated_database: str, broker_and_worker: tuple[StubBroker, Worker, Any]
) -> None:
    """Enqueue -> worker -> job succeeded + settled ai_requests row + audit."""
    _broker, _worker, ai_task = broker_and_worker
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _seed_organisation(session)
        await _enable_ai(session, organisation.id)
        user = await _seed_user(session)
        storage_key = await _put_text_document(organisation.id)

    job = await _enqueue(
        session_factory,
        organisation_id=organisation.id,
        user_id=user.id,
        storage_key=storage_key,
        ai_task=ai_task,
    )
    request_id = request_id_for_job(job.id)

    await _wait_for_status(session_factory, job.id, JobStatus.SUCCEEDED)
    record = await _request_row(session_factory, organisation.id, request_id)
    assert record is not None
    assert record.status == AIRequestStatus.SUCCEEDED
    assert record.task == "document.classify"
    assert record.provider == "fake"
    assert record.user_id == user.id
    assert record.input_reference == storage_key
    assert record.input_tokens >= 0
    assert record.output_tokens >= 0
    from app.modules.audit.service import ACTION_AI_REQUEST_COMPLETED

    assert (
        await _audit_count(
            session_factory, resource_id=request_id, action=ACTION_AI_REQUEST_COMPLETED
        )
        == 1
    )


async def test_broker_message_carries_job_id_only_no_bytes(migrated_database: str) -> None:
    """The broker message carries only the job id — never file bytes (Scope §6.6)."""
    from app.broker import worker_middleware

    broker = StubBroker(middleware=worker_middleware())
    dramatiq.set_broker(broker)
    ai_task = dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(execute_ai_task)
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _seed_organisation(session)
        await _enable_ai(session, organisation.id)
        user = await _seed_user(session)
        storage_key = await _put_text_document(organisation.id, content="sensitive lease content")

    captured: list[dict[str, Any]] = []
    original_send = ai_task.send

    def _capture_send(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return original_send(**kwargs)

    ai_task.send = _capture_send  # type: ignore[method-assign]
    try:
        await _enqueue(
            session_factory,
            organisation_id=organisation.id,
            user_id=user.id,
            storage_key=storage_key,
            ai_task=ai_task,
        )
    finally:
        ai_task.send = original_send  # type: ignore[method-assign]
    assert len(captured) == 1
    kwargs = captured[0]
    # The only carried data is the job id; the storage reference and any bytes
    # stay in the durable row / private storage, off the broker.
    assert set(kwargs) == {"job_id"}
    assert "content" not in kwargs and "bytes" not in kwargs and "storage_reference" not in kwargs


async def test_redelivered_message_does_not_redispatch(
    migrated_database: str, broker_and_worker: tuple[StubBroker, Worker, Any]
) -> None:
    """A re-delivered message reconciles to the existing terminal state (Scope §6.5/§6.6)."""
    _broker, _worker, ai_task = broker_and_worker
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _seed_organisation(session)
        await _enable_ai(session, organisation.id)
        user = await _seed_user(session)
        storage_key = await _put_text_document(organisation.id)

    job = await _enqueue(
        session_factory,
        organisation_id=organisation.id,
        user_id=user.id,
        storage_key=storage_key,
        ai_task=ai_task,
    )
    request_id = request_id_for_job(job.id)
    await _wait_for_status(session_factory, job.id, JobStatus.SUCCEEDED)

    # Re-deliver through the broker so the worker reconciles on its own event
    # loop (the execution id already has a terminal row, so the worker must
    # reconcile instead of re-dispatching — never a second provider charge).
    ai_task.send(job_id=str(job.id))
    await asyncio.sleep(1.0)

    # Still exactly one attempt row (no double-charge, no duplicate output).
    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(AIRequestRecord).where(
                        AIRequestRecord.organisation_id == organisation.id,
                        AIRequestRecord.request_id == request_id,
                    )
                )
            ).all()
        )
    assert len(rows) == 1
    assert rows[0].status == AIRequestStatus.SUCCEEDED
    job_after = await _wait_for_status(session_factory, job.id, JobStatus.SUCCEEDED)
    assert job_after.status == JobStatus.SUCCEEDED


async def test_worker_re_reads_storage_each_attempt(migrated_database: str) -> None:
    """Every attempt re-reads the object: a vanished object fails before dispatch."""
    from app.broker import worker_middleware

    broker = StubBroker(middleware=worker_middleware())
    dramatiq.set_broker(broker)
    ai_task = dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(execute_ai_task)
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _seed_organisation(session)
        await _enable_ai(session, organisation.id)
        user = await _seed_user(session)
        storage_key = await _put_text_document(organisation.id)

    # Enqueue with no worker running, then delete the object before the worker
    # starts. The worker re-reads on its attempt, so the missing object fails
    # the job permanently rather than dispatching stale content.
    job = await _enqueue(
        session_factory,
        organisation_id=organisation.id,
        user_id=user.id,
        storage_key=storage_key,
        ai_task=ai_task,
    )
    await _fake_storage().delete_object(storage_key)
    worker = Worker(broker, worker_timeout=100, worker_threads=2)
    worker.start()
    try:
        job_after = await _wait_for_status(session_factory, job.id, JobStatus.FAILED, timeout=25.0)
    finally:
        worker.stop()
        from app.db.session import engine

        await engine.dispose()
    assert job_after.error_code == "ai_input_invalid"


async def test_worker_classifies_binary_document(
    migrated_database: str, broker_and_worker: tuple[StubBroker, Worker, Any]
) -> None:
    """A PDF document is resolved as an attachment and classified successfully.

    v0.7 Scope §2/§5.1: the storage reference becomes a provider-neutral
    attachment routed to a document-capable model — binary documents are
    first-class inputs, not rejected.
    """
    _broker, _worker, ai_task = broker_and_worker
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _seed_organisation(session)
        await _enable_ai(session, organisation.id)
        user = await _seed_user(session)
        key = f"organisations/{organisation.id}/ai/scratch/doc-{uuid.uuid4().hex[:8]}.pdf"
        await _fake_storage().put(key, b"%PDF-1.4 fixture", content_type="application/pdf")

    job = await _enqueue(
        session_factory,
        organisation_id=organisation.id,
        user_id=user.id,
        storage_key=key,
        ai_task=ai_task,
    )
    job_after = await _wait_for_status(session_factory, job.id, JobStatus.SUCCEEDED)
    assert job_after.status == JobStatus.SUCCEEDED
    request_id = request_id_for_job(job.id)
    record = await _request_row(session_factory, organisation.id, request_id)
    assert record is not None
    assert record.status == AIRequestStatus.SUCCEEDED


async def test_request_row_is_org_scoped(
    migrated_database: str, broker_and_worker: tuple[StubBroker, Worker, Any]
) -> None:
    """A request row from another organisation is invisible (BP §9)."""
    _broker, _worker, ai_task = broker_and_worker
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _seed_organisation(session)
        await _enable_ai(session, organisation.id)
        user = await _seed_user(session)
        storage_key = await _put_text_document(organisation.id)

    job = await _enqueue(
        session_factory,
        organisation_id=organisation.id,
        user_id=user.id,
        storage_key=storage_key,
        ai_task=ai_task,
    )
    request_id = request_id_for_job(job.id)
    await _wait_for_status(session_factory, job.id, JobStatus.SUCCEEDED)

    # A different organisation cannot see the request row.
    other_org_id = uuid.uuid4()
    foreign = await _request_row(session_factory, other_org_id, request_id)
    assert foreign is None


async def test_crash_window_reconciles_winning_attempt(
    migrated_database: str,
) -> None:
    """A crash between AI success and job settlement is reconciled correctly.

    Simulates a worker that dispatched, settled the AI request rows (attempt 1
    failed transiently, attempt 2 succeeded) but crashed before
    ``jobs_service.succeed``. The job is still ``running``. A re-delivered
    message reconciles to the **winning** attempt — not attempt 1 — so the job
    succeeds (v0.7 Scope §6.4/§6.6).
    """
    from app.broker import worker_middleware

    broker = StubBroker(middleware=worker_middleware())
    dramatiq.set_broker(broker)
    ai_task = dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(execute_ai_task)
    session_factory = _session_factory(migrated_database)
    async with session_factory() as session:
        organisation = await _seed_organisation(session)
        await _enable_ai(session, organisation.id)
        user = await _seed_user(session)
        storage_key = await _put_text_document(organisation.id)

    # Enqueue with no worker running so we can set up the crash-window state
    # before the message is consumed.
    job = await _enqueue(
        session_factory,
        organisation_id=organisation.id,
        user_id=user.id,
        storage_key=storage_key,
        ai_task=ai_task,
    )
    request_id = request_id_for_job(job.id)

    # Manually simulate the crash window: the AI execution settled two attempt
    # rows (1 failed, 2 succeeded) but the job never reached ``succeeded``.
    async with session_factory() as session:
        job_row = await session.get(Job, job.id)
        assert job_row is not None
        job_row.status = JobStatus.RUNNING
        session.add_all(
            [
                AIRequestRecord(
                    organisation_id=organisation.id,
                    user_id=user.id,
                    request_id=request_id,
                    attempt_number=1,
                    task="document.classify",
                    provider="fake",
                    model="fake-model-document.classify",
                    prompt_name="document.classify",
                    prompt_version=1,
                    status=AIRequestStatus.FAILED,
                    error_code="provider_unavailable",
                    input_reference=storage_key,
                ),
                AIRequestRecord(
                    organisation_id=organisation.id,
                    user_id=user.id,
                    request_id=request_id,
                    attempt_number=2,
                    task="document.classify",
                    provider="fake",
                    model="fake-model-document.classify",
                    prompt_name="document.classify",
                    prompt_version=1,
                    status=AIRequestStatus.SUCCEEDED,
                    input_reference=storage_key,
                ),
            ]
        )
        await session.commit()

    # Now start the worker. It picks up the enqueued message, sees a non-terminal
    # job, dispatches, hits the replay signal, and reconciles to the winning
    # (succeeded) attempt.
    worker = Worker(broker, worker_timeout=100, worker_threads=2)
    worker.start()
    try:
        job_after = await _wait_for_status(
            session_factory, job.id, JobStatus.SUCCEEDED, timeout=25.0
        )
    finally:
        worker.stop()
        from app.db.session import engine

        await engine.dispose()
    assert job_after.status == JobStatus.SUCCEEDED
