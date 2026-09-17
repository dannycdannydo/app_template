"""Real Redis worker journeys for durable maintenance runs (plan P4)."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import dramatiq
import pytest
from alembic import command
from alembic.config import Config
from dramatiq.brokers.redis import RedisBroker
from dramatiq.worker import Worker
from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.job_coordinator.reconciliation import recover_expired_maintenance_runs
from app.modules.maintenance import execution, service
from app.modules.maintenance.models import (
    MaintenanceRun,
    MaintenanceRunStatus,
    MaintenanceTaskType,
)
from app.modules.outbox.models import OutboxEvent

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_QUEUE = "test-maintenance-broker"


def _probe(url: str) -> bool:
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
    from app.broker import worker_middleware

    previous_broker = dramatiq.get_broker()
    broker = RedisBroker(
        url=_REDIS_URL,
        namespace=f"maintenance-test-{uuid.uuid4().hex[:8]}",
        middleware=worker_middleware(),
        maintenance_chance=1_000_000,
    )
    dramatiq.set_broker(broker)
    worker = Worker(broker, worker_threads=2, worker_timeout=100)
    worker.start()
    try:
        yield broker, worker
    finally:
        worker.stop()
        broker.flush_all()
        broker.client.delete(  # pyright: ignore[reportUnknownMemberType]
            f"{broker.namespace}:__heartbeats__"
        )
        dramatiq.set_broker(previous_broker)
        from app.db.session import engine

        await engine.dispose()


def _session_factory(database_url: str) -> Any:
    engine = create_async_engine(database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _create_run(session_factory: Any, task_type: MaintenanceTaskType) -> uuid.UUID:
    async with session_factory() as session:
        run = await service.create_scheduled_run(
            session,
            task_type=task_type,
            schedule_key=f"broker:{task_type.value}:{uuid.uuid4()}",
            scheduled_for=datetime.now(UTC),
        )
        await session.commit()
        return run.id


async def _wait_for(
    session_factory: Any,
    run_id: uuid.UUID,
    predicate: Callable[[MaintenanceRun], bool],
    *,
    timeout: float = 15,
) -> MaintenanceRun:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with session_factory() as session:
            run = await session.get(MaintenanceRun, run_id)
            if run is not None and predicate(run):
                return run
        await asyncio.sleep(0.05)
    raise AssertionError(f"maintenance run {run_id} did not reach the expected state")


async def _cleanup(session_factory: Any, run_id: uuid.UUID) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.payload["maintenance_run_id"].as_string() == str(run_id)
            )
        )
        await session.execute(delete(MaintenanceRun).where(MaintenanceRun.id == run_id))
        await session.commit()


async def test_worker_crash_and_redis_loss_eventually_rerun_successfully(
    migrated_database: str,
    broker_and_worker: tuple[RedisBroker, Worker],
) -> None:
    """A consumed message can vanish; lease recovery creates the rerun truth."""
    broker, _worker = broker_and_worker
    session_factory = _session_factory(migrated_database)
    run_id = await _create_run(session_factory, MaintenanceTaskType.AI_RETENTION)

    async def _claim_then_vanish(maintenance_run_id: str) -> None:
        async with session_factory() as session:
            claim = await service.claim_run(
                session,
                run_id=uuid.UUID(maintenance_run_id),
                expected_task_type=MaintenanceTaskType.AI_RETENTION,
            )
            assert claim.outcome is service.MaintenanceClaimOutcome.CLAIMED

    crash_actor = dramatiq.actor(
        actor_name=f"maintenance_crash_{uuid.uuid4().hex}",
        queue_name=_QUEUE,
        max_retries=0,
    )(_claim_then_vanish)
    crash_actor.send(maintenance_run_id=str(run_id))
    await _wait_for(
        session_factory,
        run_id,
        lambda run: run.status is MaintenanceRunStatus.RUNNING,
    )

    # The worker consumed the only message, then vanished before settlement;
    # clearing the namespaced broker proves Redis contains no recovery truth.
    broker.flush_all()
    async with session_factory() as session:
        await session.execute(
            update(MaintenanceRun)
            .where(MaintenanceRun.id == run_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    async with session_factory() as session:
        assert await recover_expired_maintenance_runs(
            session,
            now=datetime.now(UTC),
            cooldown_seconds=60,
            limit=10,
        ) == [run_id]

    async def _successful_rerun(maintenance_run_id: str) -> None:
        async def _handler(_session: Any) -> dict[str, int]:
            return {"processed": 1}

        await execution.run_maintenance(
            task_type=MaintenanceTaskType.AI_RETENTION,
            maintenance_run_id=maintenance_run_id,
            handler=_handler,
        )

    success_actor = dramatiq.actor(
        actor_name=f"maintenance_success_{uuid.uuid4().hex}",
        queue_name=_QUEUE,
        max_retries=0,
    )(_successful_rerun)
    success_actor.send(maintenance_run_id=str(run_id))
    finished = await _wait_for(
        session_factory,
        run_id,
        lambda run: run.status is MaintenanceRunStatus.SUCCEEDED,
    )
    assert finished.attempt_count == 2
    assert finished.taken_over is True
    await _cleanup(session_factory, run_id)


async def test_real_worker_retries_to_postgres_ceiling(
    migrated_database: str,
    broker_and_worker: tuple[RedisBroker, Worker],
) -> None:
    _broker, _worker = broker_and_worker
    session_factory = _session_factory(migrated_database)
    run_id = await _create_run(session_factory, MaintenanceTaskType.TRANSFER_RECONCILE)

    async def _fail(maintenance_run_id: str) -> None:
        async def _handler(_session: Any) -> dict[str, int]:
            raise RuntimeError("temporary test outage")

        await execution.run_maintenance(
            task_type=MaintenanceTaskType.TRANSFER_RECONCILE,
            maintenance_run_id=maintenance_run_id,
            handler=_handler,
        )

    actor = dramatiq.actor(
        actor_name=f"maintenance_fail_{uuid.uuid4().hex}",
        queue_name=_QUEUE,
        max_retries=0,
    )(_fail)
    for attempt in range(1, service.MAX_ATTEMPTS + 1):
        actor.send(maintenance_run_id=str(run_id))
        expected = (
            MaintenanceRunStatus.FAILED
            if attempt == service.MAX_ATTEMPTS
            else MaintenanceRunStatus.QUEUED
        )
        await _wait_for(
            session_factory,
            run_id,
            lambda run, attempt=attempt, expected=expected: (
                run.attempt_count == attempt and run.status is expected
            ),
        )

    finished = await _wait_for(
        session_factory,
        run_id,
        lambda run: run.status is MaintenanceRunStatus.FAILED,
    )
    assert finished.error_code == service.ERROR_CODE_EXHAUSTED
    await _cleanup(session_factory, run_id)
