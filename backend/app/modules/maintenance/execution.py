"""Shared maintenance-actor execution wrapper (plan P4, blueprint §18-§19).

Every scheduled sweep runs its bounded work through :func:`run_maintenance`,
which owns the delivery concerns the sweeps share — the maintenance
counterpart of ``app.modules.jobs.execution.run_claimed``:

- **claim**: the durable run is claimed atomically, rotating an owner token
  and taking an execution lease. Duplicate broker deliveries defer, terminal
  buckets are no-ops and a run at the global ceiling settles terminally.
- **advisory lock**: the PostgreSQL session-level lock is retained as defence
  in depth, not as the execution record (plan P4). It also fences the legacy
  argument-free path against a claimed run during a rolling deployment.
- **outcome**: success and transient failure are settled in PostgreSQL under
  the captured owner token. A transient failure writes the next delayed
  dispatch in the same transaction, so a lost Redis message cannot strand a
  sweep in ``running``.
- **stale settlement**: a superseded attempt whose run was taken over cannot
  mark the bucket complete; the message is acknowledged as a no-op.

The handler receives a session and returns its existing safe count summary,
which is logged exactly as before. Summaries are *not* persisted: the run row
records status, attempts, ownership, timestamps and a bounded error code, so
the durable ledger stays free of anything resembling swept content (BP §28).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.db.session import async_session_factory
from app.modules.maintenance import service as maintenance_service
from app.modules.maintenance.models import MaintenanceRunStatus, MaintenanceTaskType
from app.observability.sentry import capture_exception

logger = structlog.get_logger()

#: The sweep returns a safe summary of counts only — never content.
Handler = Callable[[AsyncSession], Coroutine[Any, Any, dict[str, int]]]


def _heartbeat_interval_seconds() -> float:
    """Renew often enough to tolerate one delayed heartbeat safely."""
    return max(1.0, maintenance_service.lease_seconds() / 3)


async def _heartbeat(*, run_id: uuid.UUID, owner_token: uuid.UUID) -> None:
    """Renew one run until cancelled, failing if ownership was superseded."""
    while True:
        await asyncio.sleep(_heartbeat_interval_seconds())
        async with async_session_factory() as session:
            await maintenance_service.renew_lease(
                session,
                run_id=run_id,
                owner_token=owner_token,
            )


async def _run_with_heartbeat(
    *,
    run_id: uuid.UUID,
    owner_token: uuid.UUID,
    session: AsyncSession,
    handler: Handler,
) -> dict[str, int]:
    """Run bounded work while an independent owner-fenced heartbeat is live."""
    work = asyncio.create_task(handler(session))
    heartbeat = asyncio.create_task(_heartbeat(run_id=run_id, owner_token=owner_token))
    done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
    if heartbeat in done:
        # A heartbeat only completes by failing (normally because coordinator
        # recovery superseded this attempt). Stop further business work before
        # surfacing the stale/error outcome to the normal settlement path.
        work.cancel()
        with suppress(asyncio.CancelledError):
            await work
        await heartbeat
        raise AssertionError("maintenance heartbeat returned unexpectedly")
    heartbeat.cancel()
    with suppress(asyncio.CancelledError):
        await heartbeat
    return await work


def advisory_key(name: str) -> int:
    """Return a stable signed 64-bit PostgreSQL advisory-lock key."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


async def try_maintenance_lock(connection: AsyncConnection, name: str) -> bool:
    """Try a session-level lock so a duplicate sweep does no work."""
    return bool(await connection.scalar(select(func.pg_try_advisory_lock(advisory_key(name)))))


async def release_maintenance_lock(connection: AsyncConnection, name: str) -> None:
    """Release the session-level lock taken by :func:`try_maintenance_lock`."""
    await connection.execute(select(func.pg_advisory_unlock(advisory_key(name))))
    await connection.commit()


def _log(outcome: str, *, task_type: MaintenanceTaskType, **fields: object) -> None:
    """Emit one bounded, safe structured log line for a maintenance outcome.

    Fields carry only the closed task type, the opaque run id and stable
    reason codes — never swept object keys, organisation ids or error text.
    """
    logger.info(f"maintenance.{outcome}", task_type=task_type.value, **fields)


async def _run_locked(*, task_type: MaintenanceTaskType, handler: Handler) -> dict[str, int] | None:
    """Run ``handler`` under the advisory lock; ``None`` when it is held.

    The sweep session is bound to the lock connection (as the sweeps have
    always been), so the work and the lock share one connection lifetime and
    no transaction ever spans provider I/O.
    """
    async with async_session_factory() as lock_session:
        connection = await lock_session.connection()
        if not await try_maintenance_lock(connection, task_type.value):
            return None
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                return await handler(session)
        finally:
            await release_maintenance_lock(connection, task_type.value)


async def _run_legacy(*, task_type: MaintenanceTaskType, handler: Handler) -> None:
    """Run a sweep delivered by a pre-P4 argument-free broker message.

    Rolling deployments can leave version-1 maintenance messages in flight
    when the new workers start. Rather than crashing them (which would look
    like a broken sweep), they run under the advisory lock alone — the exact
    behaviour of the previous release — and are logged as legacy so an
    operator can see the old path draining. Nothing produces these messages
    any more; the coordinator always publishes a run reference.
    """
    _log("legacy_started", task_type=task_type)
    summary = await _run_locked(task_type=task_type, handler=handler)
    if summary is None:
        _log("skipped", task_type=task_type, reason="duplicate_sweep")
        return
    _log("legacy_completed", task_type=task_type, **summary)


async def run_maintenance(
    *,
    task_type: MaintenanceTaskType,
    maintenance_run_id: str | None,
    handler: Handler,
) -> None:
    """Claim ``maintenance_run_id`` and run ``handler`` under its ownership."""
    if maintenance_run_id is None:
        await _run_legacy(task_type=task_type, handler=handler)
        return
    run_id = uuid.UUID(maintenance_run_id)

    async with async_session_factory() as session:
        claim = await maintenance_service.claim_run(
            session,
            run_id=run_id,
            expected_task_type=task_type,
        )

    if claim.outcome is maintenance_service.MaintenanceClaimOutcome.STALE:
        _log("skipped", task_type=task_type, run_id=str(run_id), reason="terminal_state")
        return
    if claim.outcome is maintenance_service.MaintenanceClaimOutcome.EXHAUSTED:
        _log("exhausted", task_type=task_type, run_id=str(run_id), reason="global_attempt_limit")
        return
    if claim.outcome is maintenance_service.MaintenanceClaimOutcome.MISMATCH:
        _log(
            "rejected",
            task_type=task_type,
            run_id=str(run_id),
            reason=maintenance_service.ERROR_CODE_TASK_MISMATCH,
        )
        return
    if claim.outcome is maintenance_service.MaintenanceClaimOutcome.DEFERRED:
        # A live lease means another worker owns this bucket. A scheduled
        # sweep has no caller waiting on it, so the duplicate is acknowledged
        # rather than retried: the owner's settlement is the durable outcome.
        _log("skipped", task_type=task_type, run_id=str(run_id), reason="duplicate_sweep")
        return

    owner_token = claim.owner_token
    assert owner_token is not None  # CLAIMED always carries the rotated token
    _log(
        "taken_over" if claim.taken_over else "claimed",
        task_type=task_type,
        run_id=str(run_id),
        attempt=claim.attempt_number,
    )
    try:

        async def _owned_handler(session: AsyncSession) -> dict[str, int]:
            return await _run_with_heartbeat(
                run_id=run_id,
                owner_token=owner_token,
                session=session,
                handler=handler,
            )

        summary = await _run_locked(task_type=task_type, handler=_owned_handler)
    except maintenance_service.MaintenanceStaleRunError:
        _log("settled_stale", task_type=task_type, run_id=str(run_id), reason="run_superseded")
        return
    except Exception as exc:
        logger.error(
            "maintenance.attempt_failed",
            task_type=task_type.value,
            run_id=str(run_id),
            exc_info=True,
        )
        capture_exception(exc)
        await _settle_failure(
            task_type=task_type,
            run_id=run_id,
            owner_token=owner_token,
            error_code=maintenance_service.ERROR_CODE_TRANSIENT,
        )
        return
    if summary is None:
        # Defence in depth held the sweep off: another process (a legacy
        # message, or a worker whose lease this claim took over) still owns
        # the advisory lock. Settle for a durable delayed retry rather than
        # reporting a bucket that never ran as complete.
        _log("deferred", task_type=task_type, run_id=str(run_id), reason="lock_unavailable")
        await _settle_failure(
            task_type=task_type,
            run_id=run_id,
            owner_token=owner_token,
            error_code=maintenance_service.ERROR_CODE_LOCK_UNAVAILABLE,
        )
        return
    try:
        async with async_session_factory() as session:
            await maintenance_service.complete_run(session, run_id=run_id, owner_token=owner_token)
    except maintenance_service.MaintenanceStaleRunError:
        _log("settled_stale", task_type=task_type, run_id=str(run_id), reason="run_superseded")
        return
    _log("completed", task_type=task_type, run_id=str(run_id), **summary)


async def _settle_failure(
    *,
    task_type: MaintenanceTaskType,
    run_id: uuid.UUID,
    owner_token: uuid.UUID,
    error_code: str,
) -> None:
    """Record the owned attempt's failure: delayed retry or terminal exhaustion."""
    try:
        async with async_session_factory() as session:
            decision = await maintenance_service.settle_retryable_failure(
                session,
                run_id=run_id,
                owner_token=owner_token,
                error_code=error_code,
            )
    except maintenance_service.MaintenanceStaleRunError:
        _log("settled_stale", task_type=task_type, run_id=str(run_id), reason="run_superseded")
        return
    if decision is MaintenanceRunStatus.QUEUED:
        _log("retry_scheduled", task_type=task_type, run_id=str(run_id), reason=error_code)
    else:
        _log("exhausted", task_type=task_type, run_id=str(run_id), reason=error_code)
