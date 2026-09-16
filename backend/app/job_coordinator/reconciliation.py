"""Queued-job recovery and UTC-bucket maintenance scheduling (plan P4).

All mutations remain PostgreSQL transactions.  Redis is deliberately absent:
the coordinator creates a replacement outbox intent and its normal publication
loop later turns that intent into a reference-only broker message.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.conventions import uuid7
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatus
from app.modules.outbox.contracts import (
    EVENT_TYPE_AI_RETENTION,
    EVENT_TYPE_OUTBOX_CLEANUP_COMPLETED,
    EVENT_TYPE_TRANSFER_RECONCILE,
)
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus
from app.modules.outbox.queries import (
    published_events_retention_statement,
    queued_jobs_for_reconciliation_statement,
    running_jobs_for_reconciliation_statement,
)
from app.modules.outbox.service import (
    create_dispatch_event,
    create_schedule_event,
    reconciliation_dispatch_key,
)
from app.observability.metrics import JOBS_FAILED_TOTAL


@dataclass(frozen=True)
class ReconciliationStats:
    """Opaque aggregate outcomes for a bounded coordinator maintenance pass."""

    reconciled_jobs: int = 0
    recovered_running_jobs: int = 0
    scheduled_events: int = 0
    cleaned_events: int = 0


def schedule_key(event_type: str, *, now: datetime, interval_hours: int) -> str:
    """Return the unique key for one UTC schedule bucket."""
    timestamp = int(now.astimezone(UTC).timestamp())
    return f"{event_type}:schedule:{timestamp // (interval_hours * 3600)}"


def reconciliation_cutoff(
    *, now: datetime, threshold_seconds: int, cooldown_seconds: int
) -> datetime:
    """Return the shared eligibility cutoff for reconciliation inspection/apply."""
    return now - timedelta(seconds=max(threshold_seconds, cooldown_seconds))


async def reconcile_queued_jobs(
    session: AsyncSession,
    *,
    now: datetime,
    threshold_seconds: int,
    cooldown_seconds: int,
    limit: int,
) -> list[uuid.UUID]:
    """Create bounded replacement dispatch intents for stranded queued jobs.

    The candidate query excludes terminal/running jobs and any active outbox
    request.  Locking selected job rows with ``SKIP LOCKED`` means concurrent
    coordinators never create competing replacements.  The latest published
    event must be older than both the loss threshold and cooldown, so a job
    receives no more than one recovery dispatch per cooldown period.

    ``of=Job`` scopes the lock (and ``SKIP LOCKED``) to the ``jobs`` row the
    statement selects; the joined ``outbox_events`` row is read-only here, so a
    momentary lock on it must not silently drop a candidate from the pass.
    """
    cutoff = reconciliation_cutoff(
        now=now,
        threshold_seconds=threshold_seconds,
        cooldown_seconds=cooldown_seconds,
    )
    rows = (
        await session.scalars(
            queued_jobs_for_reconciliation_statement(
                published_before=cutoff, limit=limit
            ).with_for_update(skip_locked=True, of=Job)
        )
    ).all()
    reconciled: list[uuid.UUID] = []
    exhausted_job_types: list[str] = []
    bucket = int(now.timestamp()) // cooldown_seconds
    for job in rows:
        # A job that already consumed every allowed attempt settles failed in
        # PostgreSQL instead of receiving a fresh nominal retry budget (plan
        # P2, AC3).
        if await jobs_service.enforce_attempt_ceiling_locked(session, job):
            exhausted_job_types.append(job.job_type)
            continue
        event_id = uuid7()
        await create_dispatch_event(
            session,
            organisation_id=job.organisation_id,
            job_id=job.id,
            event_id=event_id,
            deduplication_key=reconciliation_dispatch_key(job.id, cooldown_bucket=bucket),
        )
        job.dispatch_id = event_id
        reconciled.append(job.id)
    await session.commit()
    for job_type in exhausted_job_types:
        JOBS_FAILED_TOTAL.labels(job_type=job_type).inc()
    return reconciled


async def reconciliation_candidates(
    session: AsyncSession,
    *,
    now: datetime,
    threshold_seconds: int,
    cooldown_seconds: int,
    limit: int,
) -> list[uuid.UUID]:
    """Return the same bounded candidate ids as reconciliation, without mutation."""
    cutoff = reconciliation_cutoff(
        now=now,
        threshold_seconds=threshold_seconds,
        cooldown_seconds=cooldown_seconds,
    )
    rows = (
        await session.scalars(
            queued_jobs_for_reconciliation_statement(published_before=cutoff, limit=limit)
        )
    ).all()
    return [job.id for job in rows]


async def reconcile_running_jobs(
    session: AsyncSession,
    *,
    now: datetime,
    threshold_seconds: int,
    cooldown_seconds: int,
    limit: int,
) -> list[uuid.UUID]:
    """Recover bounded, lease-expired ``running`` jobs into new dispatches.

    A worker that died after claiming can otherwise be stranded forever when
    Redis holds no copy of its message: this PostgreSQL-owned sweep is the
    backstop (plan P2, AC4). The candidate query excludes terminal jobs, jobs
    whose current dispatch is not yet published, and jobs inside the recovery
    cooldown. Each selected row is locked with ``FOR UPDATE SKIP LOCKED`` and
    the lease is re-checked under the lock; the dead attempt is closed
    ``abandoned``, the dispatch/owner boundary is rotated, the job returns to
    ``queued`` and exactly one cooldown-keyed outbox intent is created in the
    same transaction — never a direct Redis publish. A job already at the
    global attempt ceiling is settled failed instead (plan P2, AC3).

    Eligibility uses the shared :func:`reconciliation_cutoff` (the greater of
    the loss threshold and the cooldown, measured from the current dispatch's
    publication), so effective recovery latency is
    ``max(lease expiry, dispatch publication + max(threshold, cooldown))``
    rather than strictly "lease expiry + cooldown". With the default 900 s
    lease/threshold/cooldown these coincide, but lowering
    ``job_execution_lease_seconds`` alone does not shorten recovery below the
    publication-based bound.

    ``of=Job`` scopes the lock (and ``SKIP LOCKED``) to the ``jobs`` row; the
    joined dispatch ``outbox_events`` row is read-only, so a momentary lock on
    it (e.g. by the published-outbox cleanup batch) must not silently drop a
    candidate from the pass.
    """
    published_before = reconciliation_cutoff(
        now=now,
        threshold_seconds=threshold_seconds,
        cooldown_seconds=cooldown_seconds,
    )
    rows = (
        await session.scalars(
            running_jobs_for_reconciliation_statement(
                lease_expired_before=now,
                published_before=published_before,
                limit=limit,
            ).with_for_update(skip_locked=True, of=Job)
        )
    ).all()
    recovered: list[uuid.UUID] = []
    exhausted_job_types: list[str] = []
    bucket = int(now.timestamp()) // cooldown_seconds
    for job in rows:
        # The owner may have completed or renewed the lease while this
        # coordinator waited for the row lock; only a still-expired running job
        # is recovered.
        if job.status != JobStatus.RUNNING:
            continue
        if job.execution_lease_expires_at is None or job.execution_lease_expires_at > now:
            continue
        if await jobs_service.enforce_attempt_ceiling_locked(session, job):
            exhausted_job_types.append(job.job_type)
            continue
        await jobs_service.close_running_attempt_abandoned(session, job, now=now)
        event_id = uuid7()
        await create_dispatch_event(
            session,
            organisation_id=job.organisation_id,
            job_id=job.id,
            event_id=event_id,
            deduplication_key=reconciliation_dispatch_key(job.id, cooldown_bucket=bucket),
        )
        job.status = JobStatus.QUEUED
        job.dispatch_id = event_id
        # Rotate the attempt-distinguishing credential so the dead attempt's
        # captured owner token can never mutate the recovered job (plan P2).
        job.owner_token = uuid7()
        job.execution_lease_expires_at = None
        recovered.append(job.id)
    await session.commit()
    for job_type in exhausted_job_types:
        JOBS_FAILED_TOTAL.labels(job_type=job_type).inc()
    return recovered


async def schedule_maintenance_events(
    session: AsyncSession,
    *,
    now: datetime,
    ai_retention_interval_hours: int,
    transfer_reconcile_interval_hours: int,
) -> int:
    """Persist missing global maintenance intents for this UTC schedule tick.

    The unique deduplication key is the concurrency boundary.  A nested
    transaction isolates a concurrent insert collision so one scheduler tick
    never rolls back the other event type.
    """
    created = 0
    for event_type, interval_hours in (
        (EVENT_TYPE_AI_RETENTION, ai_retention_interval_hours),
        (EVENT_TYPE_TRANSFER_RECONCILE, transfer_reconcile_interval_hours),
    ):
        key = schedule_key(event_type, now=now, interval_hours=interval_hours)
        try:
            async with session.begin_nested():
                await create_schedule_event(session, event_type=event_type, schedule_key=key)
                await session.flush()
        except IntegrityError:
            continue
        created += 1
    await session.commit()
    return created


async def cleanup_published_events(
    session: AsyncSession,
    *,
    now: datetime,
    retention_days: int,
    limit: int,
    interval_hours: int,
) -> int:
    """Delete one bounded batch once per durable UTC cleanup bucket.

    A completed cleanup is recorded as an internal, already-published outbox
    ledger row. Its unique UTC-bucket key is the cross-replica/restart cadence
    boundary; writing it and deleting rows in the same transaction means a
    crash cannot incorrectly suppress a later cleanup. The marker is never
    dispatched and is itself retained/deleted by the same published retention
    policy after 30 days.
    """
    bucket = int(now.astimezone(UTC).timestamp()) // (interval_hours * 3600)
    marker = OutboxEvent(
        event_type=EVENT_TYPE_OUTBOX_CLEANUP_COMPLETED,
        event_version=1,
        aggregate_type="outbox_cleanup",
        payload={},
        deduplication_key=f"outbox.cleanup:{bucket}",
        status=OutboxEventStatus.PUBLISHED,
        created_at=now,
        available_at=now,
        processed_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(marker)
            await session.flush()
    except IntegrityError:
        await session.rollback()
        return 0
    rows = (
        await session.scalars(
            published_events_retention_statement(
                created_before=now - timedelta(days=retention_days), limit=limit
            ).with_for_update(skip_locked=True)
        )
    ).all()
    ids = [row.id for row in rows]
    if ids:
        await session.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(ids)))
    await session.commit()
    return len(ids)


async def run_maintenance_pass(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    reconciliation_threshold_seconds: int,
    reconciliation_cooldown_seconds: int,
    reconciliation_limit: int,
    ai_retention_interval_hours: int,
    transfer_reconcile_interval_hours: int,
    outbox_retention_days: int | None = None,
    outbox_cleanup_batch_size: int | None = None,
    outbox_cleanup_interval_hours: int | None = None,
) -> ReconciliationStats:
    """Run bounded reconciliation, scheduling and cleanup transactions."""
    async with session_factory() as session:
        jobs = await reconcile_queued_jobs(
            session,
            now=now,
            threshold_seconds=reconciliation_threshold_seconds,
            cooldown_seconds=reconciliation_cooldown_seconds,
            limit=reconciliation_limit,
        )
    async with session_factory() as session:
        recovered = await reconcile_running_jobs(
            session,
            now=now,
            threshold_seconds=reconciliation_threshold_seconds,
            cooldown_seconds=reconciliation_cooldown_seconds,
            limit=reconciliation_limit,
        )
    async with session_factory() as session:
        scheduled = await schedule_maintenance_events(
            session,
            now=now,
            ai_retention_interval_hours=ai_retention_interval_hours,
            transfer_reconcile_interval_hours=transfer_reconcile_interval_hours,
        )
    cleaned = 0
    if (
        outbox_retention_days is not None
        and outbox_cleanup_batch_size is not None
        and outbox_cleanup_interval_hours is not None
    ):
        async with session_factory() as session:
            cleaned = await cleanup_published_events(
                session,
                now=now,
                retention_days=outbox_retention_days,
                limit=outbox_cleanup_batch_size,
                interval_hours=outbox_cleanup_interval_hours,
            )
    return ReconciliationStats(
        reconciled_jobs=len(jobs),
        recovered_running_jobs=len(recovered),
        scheduled_events=scheduled,
        cleaned_events=cleaned,
    )
