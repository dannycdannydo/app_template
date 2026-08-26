"""Queued-job recovery and UTC-bucket maintenance scheduling (plan P4).

All mutations remain PostgreSQL transactions.  Redis is deliberately absent:
the coordinator creates a replacement outbox intent and its normal publication
loop later turns that intent into a reference-only broker message.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.conventions import uuid7
from app.modules.outbox.contracts import EVENT_TYPE_AI_RETENTION, EVENT_TYPE_TRANSFER_RECONCILE
from app.modules.outbox.queries import queued_jobs_for_reconciliation_statement
from app.modules.outbox.service import (
    create_dispatch_event,
    create_schedule_event,
    reconciliation_dispatch_key,
)


@dataclass(frozen=True)
class ReconciliationStats:
    """Opaque aggregate outcomes for a bounded coordinator maintenance pass."""

    reconciled_jobs: int = 0
    scheduled_events: int = 0


def schedule_key(event_type: str, *, now: datetime, interval_hours: int) -> str:
    """Return the unique key for one UTC schedule bucket."""
    timestamp = int(now.astimezone(UTC).timestamp())
    return f"{event_type}:schedule:{timestamp // (interval_hours * 3600)}"


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
    """
    cutoff = now - timedelta(seconds=max(threshold_seconds, cooldown_seconds))
    rows = (
        await session.scalars(
            queued_jobs_for_reconciliation_statement(
                published_before=cutoff, limit=limit
            ).with_for_update(skip_locked=True)
        )
    ).all()
    reconciled: list[uuid.UUID] = []
    bucket = int(now.timestamp()) // cooldown_seconds
    for job in rows:
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
    return reconciled


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


async def run_maintenance_pass(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    reconciliation_threshold_seconds: int,
    reconciliation_cooldown_seconds: int,
    reconciliation_limit: int,
    ai_retention_interval_hours: int,
    transfer_reconcile_interval_hours: int,
) -> ReconciliationStats:
    """Run the two bounded P4 maintenance writes in separate transactions."""
    async with session_factory() as session:
        jobs = await reconcile_queued_jobs(
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
    return ReconciliationStats(reconciled_jobs=len(jobs), scheduled_events=scheduled)
