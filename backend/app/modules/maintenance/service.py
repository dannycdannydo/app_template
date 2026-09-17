"""Durable maintenance-run services (plan P4, blueprint §18-§19).

Scheduled sweeps used to be ordinary Dramatiq actors: the outbox proved that a
bucket was *published*, and nothing proved it *ran*. This module gives
maintenance the same PostgreSQL-owned contract durable jobs received in plan
P1/P2, deliberately mirroring ``app.modules.jobs.service`` so there is one
state machine to reason about:

- :func:`create_scheduled_run` writes the run for one UTC bucket; the caller
  commits it together with its reference-only outbox dispatch, so a published
  event can never exist without the durable row it names.
- :func:`claim_run` atomically claims the next attempt under ``FOR UPDATE``:
  it rotates an ``owner_token``, sets an execution lease and increments the
  durable attempt count. A duplicate message that meets a live lease is
  deferred; one that meets an expired lease takes the dead attempt over; a
  terminal run is a no-op; a run at the global ceiling settles terminally.
- :func:`complete_run` and :func:`settle_retryable_failure` are owner-checked:
  a superseded worker cannot settle a run a newer attempt owns. A retryable
  failure closes the attempt and writes the next delayed dispatch in the same
  transaction, so the broker callback is never the durable boundary.
- :func:`enforce_attempt_ceiling_locked` is the coordinator's shared guard, so
  lease-expired recovery can never grant a fresh attempt past the ceiling.

Error codes come from the closed vocabulary below: bounded, stable strings an
operator can alert on. Exception text never reaches the database (BP §28).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.conventions import uuid7
from app.modules.maintenance.models import (
    MaintenanceRun,
    MaintenanceRunStatus,
    MaintenanceTaskType,
)
from app.modules.maintenance.queries import maintenance_run_by_id_statement
from app.modules.outbox.service import create_maintenance_event, maintenance_retry_key

# The total number of attempts one scheduled bucket may consume (first run
# plus retries), enforced from the durable count under a row lock — never from
# a message-local Dramatiq retry counter, which Redis loss would reset.
MAX_ATTEMPTS = 3
RETRY_MIN_BACKOFF_MS = 30_000
RETRY_MAX_BACKOFF_MS = 600_000

# The closed, bounded safe-error vocabulary persisted on a run.
ERROR_CODE_EXHAUSTED = "maintenance_retries_exhausted"
ERROR_CODE_TRANSIENT = "maintenance_transient_failure"
ERROR_CODE_LOCK_UNAVAILABLE = "maintenance_lock_unavailable"
ERROR_CODE_ABANDONED = "maintenance_attempt_abandoned"
ERROR_CODE_TASK_MISMATCH = "maintenance_task_type_mismatch"


class MaintenanceStaleRunError(Exception):
    """Raised when an attempt no longer owns the maintenance run.

    Settlement helpers verify the token captured at claim time against the
    row. A mismatch means the attempt was superseded — an expired-lease
    takeover or coordinator recovery rotated the token — so the execution
    wrapper treats the message as a no-op instead of settling over the newer
    owner.
    """

    def __init__(self, run_id: uuid.UUID, current: uuid.UUID | None, captured: uuid.UUID) -> None:
        super().__init__(
            f"attempt owned by token {captured} no longer owns maintenance run {run_id} "
            f"(current owner token: {current})"
        )
        self.run_id = run_id
        self.current_owner_token = current
        self.captured_owner_token = captured


class MaintenanceClaimOutcome(StrEnum):
    """Outcome of an atomic worker-side maintenance claim."""

    CLAIMED = "claimed"
    DEFERRED = "deferred"
    EXHAUSTED = "exhausted"
    MISMATCH = "mismatch"
    STALE = "stale"


@dataclass(frozen=True)
class MaintenanceClaimResult:
    """The outcome of :func:`claim_run`.

    ``task_type`` and ``owner_token`` are plain values rather than the ORM
    row: the claimed object is never carried across the claim commit as
    authority, so a handler cannot mistake a detached snapshot for ownership
    (the rule plan P2 established for durable jobs).
    """

    outcome: MaintenanceClaimOutcome
    run_id: uuid.UUID
    task_type: MaintenanceTaskType | None = None
    owner_token: uuid.UUID | None = None
    attempt_number: int = 0
    deferred_until: datetime | None = None
    taken_over: bool = False


def lease_seconds() -> int:
    """Return the configured maintenance execution-lease duration."""
    return get_settings().maintenance_execution_lease_seconds


def _retry_delay(attempt_number: int) -> timedelta:
    """Bound the durable retry delay for the next maintenance dispatch."""
    backoff_ms = min(RETRY_MIN_BACKOFF_MS * 2 ** (attempt_number - 1), RETRY_MAX_BACKOFF_MS)
    return timedelta(milliseconds=backoff_ms)


def _terminal(status: MaintenanceRunStatus) -> bool:
    return status in (MaintenanceRunStatus.SUCCEEDED, MaintenanceRunStatus.FAILED)


def is_terminal(status: MaintenanceRunStatus) -> bool:
    """True when a run reached a terminal state and must never run again."""
    return _terminal(status)


async def _get_run_locked(session: AsyncSession, *, run_id: uuid.UUID) -> MaintenanceRun | None:
    """Return the run under ``FOR UPDATE``, or ``None`` when it is missing.

    A missing row is not an error here: the coordinator refuses to publish a
    dispatch whose run does not exist, so the only way a worker sees one is a
    message that outlived its row. The caller treats that as a stale no-op.
    """
    return await session.scalar(maintenance_run_by_id_statement(run_id).with_for_update())


def _fail_locked(run: MaintenanceRun, *, now: datetime, error_code: str) -> None:
    """Settle a locked run terminally failed with a bounded safe error."""
    run.status = MaintenanceRunStatus.FAILED
    run.error_code = error_code
    run.completed_at = now
    run.owner_token = None
    run.lease_expires_at = None


async def create_scheduled_run(
    session: AsyncSession,
    *,
    task_type: MaintenanceTaskType,
    schedule_key: str,
    scheduled_for: datetime,
) -> MaintenanceRun:
    """Create the durable ``queued`` run for one UTC schedule bucket.

    The row is added to ``session`` but not committed: the coordinator writes
    this run and its reference-only outbox dispatch in the same transaction
    (plan P4), so an enqueued sweep always has a durable record and a crash
    between the two is impossible. The unique ``schedule_key`` is the
    concurrency boundary — a second coordinator ticking the same bucket
    collides instead of scheduling the sweep twice.
    """
    run = MaintenanceRun(
        task_type=task_type,
        schedule_key=schedule_key,
        scheduled_for=scheduled_for,
        status=MaintenanceRunStatus.QUEUED,
    )
    session.add(run)
    return run


async def claim_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    expected_task_type: MaintenanceTaskType | None = None,
) -> MaintenanceClaimResult:
    """Atomically claim the next execution attempt of a maintenance run.

    ``FOR UPDATE`` makes the claim atomic under concurrency: a duplicate that
    arrives while the first claim's transaction is open blocks on the row
    lock, then observes the fresh lease and returns ``DEFERRED``.

    - terminal or missing: ``STALE`` — a completed bucket is never re-run;
    - ``running`` with a live lease: ``DEFERRED`` with the lease bound, so two
      workers never sweep concurrently;
    - ``running`` with an expired lease: the dead attempt is taken over and
      the ``owner_token`` rotates, so its captured credential is superseded;
    - global attempt ceiling reached: the run settles ``failed`` with
      :data:`ERROR_CODE_EXHAUSTED` and returns ``EXHAUSTED``;
    - otherwise ``CLAIMED``: attempt count incremented, lease set, token
      rotated.
    """
    run = await _get_run_locked(session, run_id=run_id)
    if run is None:
        return MaintenanceClaimResult(outcome=MaintenanceClaimOutcome.STALE, run_id=run_id)
    if _terminal(run.status):
        return MaintenanceClaimResult(
            outcome=MaintenanceClaimOutcome.STALE, run_id=run_id, task_type=run.task_type
        )
    now = datetime.now(UTC)
    if expected_task_type is not None and run.task_type is not expected_task_type:
        # The broker carries only an opaque run id, so the worker repeats the
        # coordinator's event/run invariant under the row lock.  A crossed
        # actor must be terminally visible and must never execute either
        # workload against the wrong durable record.
        _fail_locked(run, now=now, error_code=ERROR_CODE_TASK_MISMATCH)
        await session.commit()
        return MaintenanceClaimResult(
            outcome=MaintenanceClaimOutcome.MISMATCH,
            run_id=run_id,
            task_type=run.task_type,
        )
    if (
        run.status == MaintenanceRunStatus.RUNNING
        and run.lease_expires_at is not None
        and run.lease_expires_at > now
    ):
        return MaintenanceClaimResult(
            outcome=MaintenanceClaimOutcome.DEFERRED,
            run_id=run_id,
            task_type=run.task_type,
            deferred_until=run.lease_expires_at,
        )
    if run.attempt_count >= MAX_ATTEMPTS:
        # A lost broker message or a restarted coordinator cannot reset the
        # budget: the locked PostgreSQL count is the global ceiling.
        _fail_locked(run, now=now, error_code=ERROR_CODE_EXHAUSTED)
        await session.commit()
        return MaintenanceClaimResult(
            outcome=MaintenanceClaimOutcome.EXHAUSTED, run_id=run_id, task_type=run.task_type
        )
    taken_over = run.status == MaintenanceRunStatus.RUNNING
    run.status = MaintenanceRunStatus.RUNNING
    run.attempt_count = run.attempt_count + 1
    run.owner_token = uuid7()
    run.lease_expires_at = now + timedelta(seconds=lease_seconds())
    # Once takeover/recovery has happened it remains durable evidence even
    # after a later claim and successful completion.
    run.taken_over = run.taken_over or taken_over
    if run.started_at is None:
        run.started_at = now
    task_type = run.task_type
    owner_token = run.owner_token
    attempt_number = run.attempt_count
    await session.commit()
    return MaintenanceClaimResult(
        outcome=MaintenanceClaimOutcome.CLAIMED,
        run_id=run_id,
        task_type=task_type,
        owner_token=owner_token,
        attempt_number=attempt_number,
        taken_over=taken_over,
    )


async def renew_lease(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    owner_token: uuid.UUID,
) -> datetime:
    """Extend an active attempt's lease under its ownership fence.

    Maintenance work can span many bounded batches (notably retention across
    organisations).  Heartbeats prevent coordinator recovery from replacing a
    healthy worker merely because the original lease elapsed.
    """
    run = await _get_run_locked(session, run_id=run_id)
    if run is None or run.owner_token != owner_token or run.status != MaintenanceRunStatus.RUNNING:
        raise MaintenanceStaleRunError(
            run_id, run.owner_token if run is not None else None, owner_token
        )
    lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds())
    run.lease_expires_at = lease_expires_at
    await session.commit()
    return lease_expires_at


async def complete_run(session: AsyncSession, *, run_id: uuid.UUID, owner_token: uuid.UUID) -> None:
    """Record the owned run as ``succeeded``.

    Owner-checked: a superseded worker that finished its local sweep after a
    takeover raises :class:`MaintenanceStaleRunError` instead of marking a
    bucket complete that a newer attempt now owns.
    """
    run = await _get_run_locked(session, run_id=run_id)
    if run is None or run.owner_token != owner_token or run.status != MaintenanceRunStatus.RUNNING:
        raise MaintenanceStaleRunError(
            run_id, run.owner_token if run is not None else None, owner_token
        )
    run.status = MaintenanceRunStatus.SUCCEEDED
    run.completed_at = datetime.now(UTC)
    run.error_code = None
    run.owner_token = None
    run.lease_expires_at = None
    await session.commit()


async def settle_retryable_failure(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    owner_token: uuid.UUID,
    error_code: str = ERROR_CODE_TRANSIENT,
) -> MaintenanceRunStatus:
    """Atomically schedule the next maintenance dispatch, or exhaust the run.

    One transaction either returns the run to ``queued`` *and* writes its
    delayed reference-only dispatch, or settles it ``failed`` at the global
    ceiling. Nothing about the outcome depends on a final Redis message, so
    losing the broker cannot strand a sweep in ``running`` (AC7). The retry
    event's unique key is derived from the attempt number, so a duplicate
    settlement of the same attempt collides instead of enqueueing twice.
    """
    run = await _get_run_locked(session, run_id=run_id)
    if run is None or run.owner_token != owner_token or run.status != MaintenanceRunStatus.RUNNING:
        raise MaintenanceStaleRunError(
            run_id, run.owner_token if run is not None else None, owner_token
        )
    now = datetime.now(UTC)
    if run.attempt_count >= MAX_ATTEMPTS:
        _fail_locked(run, now=now, error_code=ERROR_CODE_EXHAUSTED)
        await session.commit()
        return MaintenanceRunStatus.FAILED
    await create_maintenance_event(
        session,
        event_type=run.task_type.value,
        maintenance_run_id=run.id,
        deduplication_key=maintenance_retry_key(run.id, attempt_number=run.attempt_count),
        available_at=now + _retry_delay(run.attempt_count),
    )
    run.status = MaintenanceRunStatus.QUEUED
    run.error_code = error_code
    run.owner_token = None
    run.lease_expires_at = None
    await session.commit()
    return MaintenanceRunStatus.QUEUED


async def enforce_attempt_ceiling_locked(
    session: AsyncSession, run: MaintenanceRun, *, now: datetime
) -> bool:
    """Fail an already-locked run that reached the global attempt ceiling.

    Coordinator recovery calls this before granting a replacement dispatch, so
    a stranded run that already consumed every allowed attempt settles failed
    instead of receiving a fresh nominal budget. Returns ``True`` when the run
    was settled; the caller owns the commit.
    """
    if run.attempt_count < MAX_ATTEMPTS:
        return False
    _fail_locked(run, now=now, error_code=ERROR_CODE_EXHAUSTED)
    return True
