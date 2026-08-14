"""Strict internal outbox event payload contracts (durable delivery plan P1).

The coordinator (plan P3) reads outbox rows and turns them into Dramatiq
messages, so every payload is validated against a closed contract before it is
persisted and again before publication. Payloads are reference-only:
``job.dispatch_requested`` carries nothing but the job id, and maintenance
payloads carry no tenant data, object references, URLs, provider ids, prompts,
document content or credentials (plan "message/data minimisation"). A contract
forbids extra fields, so an outbox row can never smuggle an actor/function
name or any other unapproved content into the broker path.

The full allow-listed dispatch registry (event type -> actor message shape)
is implemented with the coordinator in plan P3; this module is the data
contract that registry validates against.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class OutboxContractError(ValueError):
    """An outbox payload violates its closed contract or has no contract.

    Internal infrastructure error, not an API error: the coordinator maps it
    to a permanent ``dead`` outbox row (plan P3), never to an HTTP response.
    """


# Stable past-tense event names (blueprint §19) and their closed versions.
# The event type/version pair is the contract key the registry uses.
EVENT_TYPE_JOB_DISPATCH = "job.dispatch_requested"
EVENT_VERSION_JOB_DISPATCH = 1

# Scheduled maintenance events (durable delivery plan P4): the coordinator
# turns these rows into enqueues of the existing AI retention and
# provider-file reconciliation actors. Their payloads are deliberately empty
# (see :class:`MaintenancePayload`); the UTC-bucket identity lives in
# ``deduplication_key``.
EVENT_TYPE_AI_RETENTION = "ai.retention"
EVENT_VERSION_AI_RETENTION = 1
EVENT_TYPE_TRANSFER_RECONCILE = "ai.transfer_reconcile"
EVENT_VERSION_TRANSFER_RECONCILE = 1

# Aggregate names recorded on outbox rows for aggregate-history queries.
AGGREGATE_TYPE_JOB = "job"

# Bounded payload size mirrored from the database check constraint
# (ck_outbox_events_bounded_payload) so producers fail fast before SQL.
MAX_PAYLOAD_CHARS = 16384


class JobDispatchPayload(BaseModel):
    """Reference-only payload for a ``job.dispatch_requested`` event.

    Contains exactly one field: the durable job id. No file bytes, document
    text, prompts, signed URLs, object keys, recipients, credentials or
    provider responses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: uuid.UUID


class MaintenancePayload(BaseModel):
    """Closed payload for a scheduled maintenance event.

    Deliberately empty: maintenance payloads carry no tenant data, object
    references, URLs, provider ids, prompts, document content or credentials
    (plan "message/data minimisation"), and the UTC-bucket schedule identity
    already lives in ``deduplication_key``. ``extra='forbid'`` turns any
    extra field into a contract violation, so a maintenance row can never
    smuggle unapproved content into the broker path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# Event type/version -> closed payload contract. Unknown pairs are rejected
# by :func:`validate_payload`; the registry (plan P3) extends this table with
# the actor message shapes, never with free-form payloads.
_PAYLOAD_CONTRACTS: dict[tuple[str, int], type[BaseModel]] = {
    (EVENT_TYPE_JOB_DISPATCH, EVENT_VERSION_JOB_DISPATCH): JobDispatchPayload,
    (EVENT_TYPE_AI_RETENTION, EVENT_VERSION_AI_RETENTION): MaintenancePayload,
    (EVENT_TYPE_TRANSFER_RECONCILE, EVENT_VERSION_TRANSFER_RECONCILE): MaintenancePayload,
}


def dispatch_payload(job_id: uuid.UUID) -> dict[str, Any]:
    """Build the persisted JSON payload for a job dispatch event.

    ``job_id`` is serialised as a string so the payload round-trips through
    JSONB exactly as the worker-facing contract expects.
    """
    return JobDispatchPayload(job_id=job_id).model_dump(mode="json")


def validate_payload(
    event_type: str, event_version: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate a persisted payload against its closed contract.

    Returns the canonical JSON-safe payload. Raises :class:`OutboxContractError`
    for an unknown event type/version or a payload that does not match its
    contract (wrong types, missing or extra fields).
    """
    contract = _PAYLOAD_CONTRACTS.get((event_type, event_version))
    if contract is None:
        raise OutboxContractError(f"unknown outbox event contract {event_type!r} v{event_version}")
    try:
        return contract.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise OutboxContractError(f"invalid {event_type} v{event_version} payload: {exc}") from exc
