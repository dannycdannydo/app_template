"""Outbox event ORM model (durable delivery plan P1, blueprint §19).

``outbox_events`` is the transactional outbox that stands between durable job
scheduling and the Redis broker. The business change and the event that
requests publication are written in the same PostgreSQL transaction, so an
accepted job survives a broker outage: Redis executes, PostgreSQL provides
durability (blueprint §19).

The table is internal infrastructure, not an API resource. Every row carries
the stable past-tense ``event_type`` and a closed ``event_version``; the JSON
``payload`` is validated against a strict internal contract before it is
persisted and again before publication, so a persisted payload can never
smuggle an actor/function name, a URL, a credential or tenant content into
the broker path. ``organisation_id`` is nullable because maintenance events
are global (no tenant context at all); every durable job event copies the
job's validated organisation id so the tenant boundary survives the outbox
hop. ``deduplication_key`` is the unique idempotency key that makes a
duplicate scheduling request (or a duplicate schedule tick) fail instead of
double-publishing.

Statuses are ``pending`` (due for publication), ``publishing`` (claimed by a
coordinator), ``published`` (settled) and ``dead`` (permanently invalid,
operator attention required). There is deliberately no ``updated_at`` column:
the publication lifecycle timing lives in the explicit ``claimed_at`` /
``processed_at`` columns, exactly like the jobs and audit tables.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import UuidV7, uuid7


class OutboxEventStatus(enum.StrEnum):
    """Lifecycle state of one outbox row (plan decisions, blueprint §19)."""

    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DEAD = "dead"


def _status_values(enum_class: type[OutboxEventStatus]) -> list[str]:
    """Return the values a status column stores, not the enum names."""
    return [member.value for member in enum_class]


class OutboxEvent(Base):
    """One durable intent-to-publish row (blueprint §19 outbox shape).

    The indexes mirror the coordinator's four access patterns: due claims
    (``status`` + ``available_at``), aggregate history (``aggregate_type`` +
    ``aggregate_id`` for reconciliation cooldowns), stale-claim recovery
    (``status`` + ``claimed_at`` for expiring publication leases) and
    published retention (``status`` + ``created_at`` for the 30-day cleanup).
    The payload is bounded by a database check constraint so a producer bug
    cannot persist an unbounded JSON blob.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_due_claims", "status", "available_at"),
        Index("ix_outbox_events_aggregate_history", "aggregate_type", "aggregate_id", "id"),
        Index("ix_outbox_events_stale_claims", "status", "claimed_at"),
        Index("ix_outbox_events_published_retention", "status", "created_at"),
        CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'dead')",
            name="outbox_event_status",
        ),
        CheckConstraint("event_version >= 1", name="positive_event_version"),
        CheckConstraint("attempt_count >= 0", name="non_negative_attempt_count"),
        CheckConstraint(
            "char_length(payload::text) <= 16384",
            name="bounded_payload",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    # NULL for global maintenance events; otherwise the validated
    # organisation id of the durable job the event dispatches (plan P1:
    # tenant association survives the outbox hop).
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    event_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # The aggregate the event belongs to (e.g. ``job`` for dispatch events)
    # and its id, kept provider-neutral and reference-only.
    aggregate_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Unique idempotency key: one dispatch request per job, one schedule
    # bucket per maintenance event. NULL for rows without a natural key.
    deduplication_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    status: Mapped[OutboxEventStatus] = mapped_column(
        Enum(
            OutboxEventStatus,
            name="outbox_event_status",
            native_enum=False,
            length=16,
            # Persist the enum values ("pending", ...) so rows match the
            # check constraint and server default, exactly like jobs.
            values_callable=_status_values,
        ),
        nullable=False,
        default=OutboxEventStatus.PENDING,
        server_default=OutboxEventStatus.PENDING.value,
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Opaque token written by the claiming coordinator; settlement verifies
    # it so a stale publisher cannot settle a row it no longer owns.
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Bounded, sanitised error surface for dead events and failed
    # publications (plan: logs and errors never carry payload content).
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
