"""Real-PostgreSQL lifecycle tests for durable maintenance runs (plan P4)."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text, update
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


def _database_reachable(database_url: str) -> bool:
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
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


@pytest.fixture(autouse=True)
async def dispose_process_engine_between_event_loops() -> AsyncIterator[None]:
    """The execution wrapper uses the process engine; pytest gives tests new loops."""
    from app.db.session import engine

    await engine.dispose()
    yield
    await engine.dispose()


def _session_factory(database_url: str) -> Any:
    engine = create_async_engine(database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _create_run(session_factory: Any, task_type: MaintenanceTaskType) -> uuid.UUID:
    async with session_factory() as session:
        run = await service.create_scheduled_run(
            session,
            task_type=task_type,
            schedule_key=f"test:{task_type.value}:{uuid.uuid4()}",
            scheduled_for=datetime.now(UTC),
        )
        await session.commit()
        return run.id


async def _cleanup(session_factory: Any, run_ids: list[uuid.UUID]) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.payload["maintenance_run_id"]
                .as_string()
                .in_([str(run_id) for run_id in run_ids])
            )
        )
        await session.execute(delete(MaintenanceRun).where(MaintenanceRun.id.in_(run_ids)))
        await session.commit()


@pytest.mark.parametrize(
    ("stored_type", "actor_type"),
    [
        (MaintenanceTaskType.AI_RETENTION, MaintenanceTaskType.TRANSFER_RECONCILE),
        (MaintenanceTaskType.TRANSFER_RECONCILE, MaintenanceTaskType.AI_RETENTION),
    ],
)
async def test_wrong_actor_never_executes_and_fails_run(
    migrated_database: str,
    stored_type: MaintenanceTaskType,
    actor_type: MaintenanceTaskType,
) -> None:
    session_factory = _session_factory(migrated_database)
    run_id = await _create_run(session_factory, stored_type)
    called = False

    async def _handler(_session: Any) -> dict[str, int]:
        nonlocal called
        called = True
        return {}

    await execution.run_maintenance(
        task_type=actor_type,
        maintenance_run_id=str(run_id),
        handler=_handler,
    )

    async with session_factory() as session:
        run = await session.get(MaintenanceRun, run_id)
        assert run is not None
        assert run.status is MaintenanceRunStatus.FAILED
        assert run.error_code == service.ERROR_CODE_TASK_MISMATCH
        assert run.attempt_count == 0
    assert called is False
    await _cleanup(session_factory, [run_id])


async def test_retry_exhaustion_is_postgres_owned(migrated_database: str) -> None:
    session_factory = _session_factory(migrated_database)
    run_id = await _create_run(session_factory, MaintenanceTaskType.AI_RETENTION)

    async def _fail(_session: Any) -> dict[str, int]:
        raise RuntimeError("transient test failure")

    for _ in range(service.MAX_ATTEMPTS):
        await execution.run_maintenance(
            task_type=MaintenanceTaskType.AI_RETENTION,
            maintenance_run_id=str(run_id),
            handler=_fail,
        )

    async with session_factory() as session:
        run = await session.get(MaintenanceRun, run_id)
        assert run is not None
        assert run.status is MaintenanceRunStatus.FAILED
        assert run.attempt_count == service.MAX_ATTEMPTS
        assert run.error_code == service.ERROR_CODE_EXHAUSTED
        retries = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.payload["maintenance_run_id"].as_string() == str(run_id)
                )
            )
        ).all()
        assert len(retries) == service.MAX_ATTEMPTS - 1
    await _cleanup(session_factory, [run_id])


async def test_advisory_lock_contention_schedules_durable_retry(
    migrated_database: str,
) -> None:
    session_factory = _session_factory(migrated_database)
    run_id = await _create_run(session_factory, MaintenanceTaskType.AI_RETENTION)
    lock_engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with lock_engine.connect() as connection:
        assert await execution.try_maintenance_lock(
            connection, MaintenanceTaskType.AI_RETENTION.value
        )
        await execution.run_maintenance(
            task_type=MaintenanceTaskType.AI_RETENTION,
            maintenance_run_id=str(run_id),
            handler=lambda _session: asyncio.sleep(0, result={}),
        )
        await execution.release_maintenance_lock(connection, MaintenanceTaskType.AI_RETENTION.value)
    await lock_engine.dispose()

    async with session_factory() as session:
        run = await session.get(MaintenanceRun, run_id)
        assert run is not None
        assert run.status is MaintenanceRunStatus.QUEUED
        assert run.error_code == service.ERROR_CODE_LOCK_UNAVAILABLE
        assert run.attempt_count == 1
    await _cleanup(session_factory, [run_id])


async def test_worker_crash_redis_loss_recovery_then_success_is_visible(
    migrated_database: str,
) -> None:
    """A vanished message/worker is recovered solely from PostgreSQL state."""
    session_factory = _session_factory(migrated_database)
    run_id = await _create_run(session_factory, MaintenanceTaskType.TRANSFER_RECONCILE)
    async with session_factory() as session:
        claim = await service.claim_run(
            session,
            run_id=run_id,
            expected_task_type=MaintenanceTaskType.TRANSFER_RECONCILE,
        )
        assert claim.outcome is service.MaintenanceClaimOutcome.CLAIMED
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

    async def _succeed(_session: Any) -> dict[str, int]:
        return {"processed": 1}

    await execution.run_maintenance(
        task_type=MaintenanceTaskType.TRANSFER_RECONCILE,
        maintenance_run_id=str(run_id),
        handler=_succeed,
    )
    async with session_factory() as session:
        run = await session.get(MaintenanceRun, run_id)
        assert run is not None
        assert run.status is MaintenanceRunStatus.SUCCEEDED
        assert run.attempt_count == 2
        assert run.taken_over is True
        assert run.error_code is None
    await _cleanup(session_factory, [run_id])


async def test_heartbeat_prevents_false_recovery_of_healthy_worker(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory = _session_factory(migrated_database)
    run_id = await _create_run(session_factory, MaintenanceTaskType.AI_RETENTION)
    monkeypatch.setattr(service, "lease_seconds", lambda: 1)
    monkeypatch.setattr(execution, "_heartbeat_interval_seconds", lambda: 0.1)

    started = asyncio.Event()

    async def _slow_success(_session: Any) -> dict[str, int]:
        started.set()
        await asyncio.sleep(1.25)
        return {"processed": 1}

    work = asyncio.create_task(
        execution.run_maintenance(
            task_type=MaintenanceTaskType.AI_RETENTION,
            maintenance_run_id=str(run_id),
            handler=_slow_success,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(1.05)
    async with session_factory() as session:
        assert (
            await recover_expired_maintenance_runs(
                session,
                now=datetime.now(UTC),
                cooldown_seconds=60,
                limit=10,
            )
            == []
        )
    await asyncio.wait_for(work, timeout=5)

    async with session_factory() as session:
        run = await session.get(MaintenanceRun, run_id)
        assert run is not None
        assert run.status is MaintenanceRunStatus.SUCCEEDED
        assert run.attempt_count == 1
        assert run.taken_over is False
    await _cleanup(session_factory, [run_id])
