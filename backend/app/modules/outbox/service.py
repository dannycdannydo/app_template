"""Outbox event services (durable delivery plan P1, blueprint §19).

This module owns the durable outbox data contract and its invariants:

- :func:`create_dispatch_event` builds the ``job.dispatch_requested`` row that
  requests broker publication for a durable job. The scheduling service (plan
  P3) writes the job row and this event in one transaction and uses the event
  id as the job's dispatch identity; this function owns the event shape, the
  closed payload contract and the tenant-copy rule (the event always copies
  the job's validated organisation id).
- :func:`create_schedule_event` builds global maintenance rows: the
  organisation id is always NULL and the payload must never carry tenant
  data, object references, URLs, provider ids, prompts, document content or
  credentials. The unique schedule key (one row per UTC bucket) is the
  deduplication key that makes concurrent coordinator ticks converge.

Claiming, publication settlement, reconciliation and retention are
coordinator/worker concerns implemented in later checkpoints (P3-P5); this
module only defines the durable contract and its boundary rules.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.conventions import uuid7
from app.modules.outbox.contracts import (
    AGGREGATE_TYPE_JOB,
    EVENT_TYPE_JOB_DISPATCH,
    EVENT_VERSION_JOB_DISPATCH,
    MAX_PAYLOAD_CHARS,
    OutboxContractError,
    dispatch_payload,
    validate_payload,
)
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus


def _deduplication_key_for_dispatch(job_id: uuid.UUID) -> str:
    """One dispatch request per job: the unique idempotency key.

    Reconciliation (plan P4) creates later dispatch events under their own
    cooldown-scoped keys; this is the key of the initial atomic event.
    """
    return f"{EVENT_TYPE_JOB_DISPATCH}:{job_id}"


def _check_payload_size(payload: dict[str, Any], event_type: str) -> None:
    """Fail fast when a payload would exceed the database bound.

    The database check constraint ``ck_outbox_events_bounded_payload`` is the
    authoritative backstop; this mirrors it so producers surface the mistake
    before SQL. ``json.dumps`` is a close proxy for the JSONB text form.
    """
    if len(json.dumps(payload, sort_keys=True)) > MAX_PAYLOAD_CHARS:
        raise OutboxContractError(
            f"payload for {event_type} exceeds the {MAX_PAYLOAD_CHARS}-character bound"
        )


async def create_dispatch_event(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    job_id: uuid.UUID,
    event_id: uuid.UUID | None = None,
) -> OutboxEvent:
    """Create a pending ``job.dispatch_requested`` outbox row for a durable job.

    The event is added to ``session`` but not committed: the caller (the
    durable scheduling service, plan P3) writes the job row and this event in
    the same transaction and commits once. ``event_id`` optionally supplies a
    pre-generated id so the scheduling service can set it as the job's
    dispatch identity atomically. The event copies the job's validated
    organisation id (tenant association) and carries only the job id in its
    payload; the unique deduplication key makes a duplicate scheduling request
    for the same job fail instead of double-publishing.
    """
    payload = dispatch_payload(job_id)
    _check_payload_size(payload, EVENT_TYPE_JOB_DISPATCH)
    event = OutboxEvent(
        id=event_id or uuid7(),
        organisation_id=organisation_id,
        event_type=EVENT_TYPE_JOB_DISPATCH,
        event_version=EVENT_VERSION_JOB_DISPATCH,
        aggregate_type=AGGREGATE_TYPE_JOB,
        aggregate_id=job_id,
        payload=payload,
        deduplication_key=_deduplication_key_for_dispatch(job_id),
        status=OutboxEventStatus.PENDING,
    )
    session.add(event)
    return event


async def create_schedule_event(
    session: AsyncSession,
    *,
    event_type: str,
    schedule_key: str,
    payload: dict[str, Any] | None = None,
    event_version: int = 1,
    available_at: datetime | None = None,
) -> OutboxEvent:
    """Create a pending global maintenance outbox row with no tenant context.

    ``organisation_id`` is always NULL. The payload is canonicalised through
    the event's closed contract before anything is added to the session: an
    unknown event type/version pair or any field beyond the contract's
    (empty) shape raises :class:`OutboxContractError`, so a maintenance row
    can never carry tenant data, object references, URLs, provider ids,
    prompts, document content or credentials. ``schedule_key`` is the unique
    deduplication key (one row per UTC schedule bucket, plan P4), so
    concurrent coordinators converge on a single event. ``available_at``
    defaults to the database clock.
    """
    payload = payload or {}
    _check_payload_size(payload, event_type)
    payload = validate_payload(event_type, event_version, payload)
    # Only set ``available_at`` when given: the model column is NOT NULL with
    # a server default, so omitting it lets the database clock apply, while
    # passing None would violate the constraint.
    event = OutboxEvent(
        organisation_id=None,
        event_type=event_type,
        event_version=event_version,
        aggregate_type=None,
        aggregate_id=None,
        payload=payload,
        deduplication_key=schedule_key,
        status=OutboxEventStatus.PENDING,
        **({"available_at": available_at} if available_at is not None else {}),
    )
    session.add(event)
    return event
