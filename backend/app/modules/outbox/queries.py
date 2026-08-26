"""Outbox query helpers (durable delivery plan P1, blueprint §19).

Outbox rows are internal infrastructure, not an API resource: every statement
here serves the coordinator's claim/publish/reconcile loop and the published
retention cleanup, never a request handler. The complex SQL lives in this
module so the service layer stays thin and the coordinator (plan P3) can
compose these statements with its own transaction and locking boundaries
(``FOR UPDATE SKIP LOCKED`` claims are added by the coordinator, not baked
into the base statements).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, exists, select
from sqlalchemy.orm import aliased

from app.modules.jobs.models import Job, JobStatus
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus


def outbox_event_by_id_statement(event_id: uuid.UUID) -> Select[tuple[OutboxEvent]]:
    """Select one outbox row by primary key (owner-checked settlement)."""
    return select(OutboxEvent).where(OutboxEvent.id == event_id)


def outbox_event_by_dedup_key_statement(
    deduplication_key: str,
) -> Select[tuple[OutboxEvent]]:
    """Select a row by its unique deduplication key.

    Used by the scheduler to make duplicate schedule ticks converge on one
    row and by tests proving the deduplication constraint.
    """
    return select(OutboxEvent).where(OutboxEvent.deduplication_key == deduplication_key)


def due_outbox_events_statement(
    *,
    at_or_before: datetime,
    limit: int,
) -> Select[tuple[OutboxEvent]]:
    """Select rows due for publication, oldest available first.

    Only ``pending`` rows whose ``available_at`` has passed are eligible;
    publishing, published and dead rows are never re-selected. The
    coordinator (plan P3) claims these with ``FOR UPDATE SKIP LOCKED`` in a
    short transaction, so this statement deliberately has no locking.
    """
    return (
        select(OutboxEvent)
        .where(
            OutboxEvent.status == OutboxEventStatus.PENDING,
            OutboxEvent.available_at <= at_or_before,
        )
        .order_by(OutboxEvent.available_at.asc(), OutboxEvent.id.asc())
        .limit(limit)
    )


def outbox_events_for_aggregate_statement(
    aggregate_type: str,
    aggregate_id: uuid.UUID,
) -> Select[tuple[OutboxEvent]]:
    """Select an aggregate's outbox history, newest first.

    Reconciliation (plan P4) reads this history to enforce the per-job
    dispatch cooldown before creating a new dispatch event.
    """
    return (
        select(OutboxEvent)
        .where(
            OutboxEvent.aggregate_type == aggregate_type,
            OutboxEvent.aggregate_id == aggregate_id,
        )
        .order_by(OutboxEvent.id.desc())
    )


def stale_claim_events_statement(
    *,
    claimed_before: datetime,
    limit: int,
) -> Select[tuple[OutboxEvent]]:
    """Select rows whose publication claim expired before ``claimed_before``.

    A row stuck in ``publishing`` past the publication lease (crashed
    coordinator) may be reclaimed by another coordinator (plan P3).
    """
    return (
        select(OutboxEvent)
        .where(
            OutboxEvent.status == OutboxEventStatus.PUBLISHING,
            OutboxEvent.claimed_at.is_not(None),
            OutboxEvent.claimed_at < claimed_before,
        )
        .order_by(OutboxEvent.claimed_at.asc())
        .limit(limit)
    )


def published_events_retention_statement(
    *,
    created_before: datetime,
    limit: int,
) -> Select[tuple[OutboxEvent]]:
    """Select published rows older than the retention horizon for cleanup.

    Cleanup (plan P5) deletes only ``published`` rows older than the
    retention window in bounded batches; pending, publishing and dead rows
    are never selected.
    """
    return (
        select(OutboxEvent)
        .where(
            OutboxEvent.status == OutboxEventStatus.PUBLISHED,
            OutboxEvent.created_at < created_before,
        )
        .order_by(OutboxEvent.created_at.asc())
        .limit(limit)
    )


def queued_jobs_for_reconciliation_statement(
    *,
    published_before: datetime,
    limit: int,
) -> Select[tuple[Job]]:
    """Select queued jobs whose latest published dispatch is stale.

    Only a job with a previously *published* dispatch qualifies.  The
    correlated predicates exclude jobs with a newer published dispatch and
    jobs that already have pending/publishing replacement work, leaving the
    coordinator service to lock each selected job and create its replacement
    event atomically.
    """
    latest = aliased(OutboxEvent)
    newer_published = aliased(OutboxEvent)
    active = aliased(OutboxEvent)
    return (
        select(Job)
        .join(
            latest,
            (latest.aggregate_type == "job")
            & (latest.aggregate_id == Job.id)
            & (latest.status == OutboxEventStatus.PUBLISHED),
        )
        .where(
            Job.status == JobStatus.QUEUED,
            latest.processed_at.is_not(None),
            latest.processed_at <= published_before,
            ~exists(
                select(1).where(
                    newer_published.aggregate_type == "job",
                    newer_published.aggregate_id == Job.id,
                    newer_published.status == OutboxEventStatus.PUBLISHED,
                    newer_published.processed_at > latest.processed_at,
                )
            ),
            ~exists(
                select(1).where(
                    active.aggregate_type == "job",
                    active.aggregate_id == Job.id,
                    active.status.in_((OutboxEventStatus.PENDING, OutboxEventStatus.PUBLISHING)),
                )
            ),
        )
        .order_by(latest.processed_at.asc(), Job.id.asc())
        .limit(limit)
    )
