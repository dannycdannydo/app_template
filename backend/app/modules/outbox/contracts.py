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
# turns these rows into enqueues of the AI retention and provider-file
# reconciliation actors. Version 2 carries the durable ``maintenance_run_id``
# and nothing else, so the broker message references a PostgreSQL row that
# owns the sweep's claim, lease, retry and terminal outcome.
#
# Version 1 (an empty payload) is retained as a *legacy* contract, not as a
# producer: rows written by the previous release may still be pending when the
# new coordinator starts, and they must publish rather than turn ``dead``
# mid-deployment. Producers always write
# :data:`EVENT_VERSION_AI_RETENTION` / :data:`EVENT_VERSION_TRANSFER_RECONCILE`.
EVENT_TYPE_AI_RETENTION = "ai.retention"
EVENT_VERSION_AI_RETENTION = 2
EVENT_TYPE_TRANSFER_RECONCILE = "ai.transfer_reconcile"
EVENT_VERSION_TRANSFER_RECONCILE = 2
# The superseded, argument-free maintenance contract version.
EVENT_VERSION_MAINTENANCE_LEGACY = 1

# Internal retention-ledger event (durable delivery plan P5).  This is never
# dispatched; it records completion of one UTC cleanup bucket.
EVENT_TYPE_OUTBOX_CLEANUP_COMPLETED = "outbox.cleanup_completed"
EVENT_VERSION_OUTBOX_CLEANUP_COMPLETED = 1

# The current dispatch version each maintenance event type is produced at.
# Producers read this map instead of hard-coding a version, so adding a sweep
# (or versioning one) cannot leave a producer writing a contract the
# coordinator no longer publishes.
MAINTENANCE_EVENT_VERSIONS: dict[str, int] = {
    EVENT_TYPE_AI_RETENTION: EVENT_VERSION_AI_RETENTION,
    EVENT_TYPE_TRANSFER_RECONCILE: EVENT_VERSION_TRANSFER_RECONCILE,
}

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
    """Closed, empty payload for an internal or legacy maintenance event.

    Deliberately empty: maintenance payloads carry no tenant data, object
    references, URLs, provider ids, prompts, document content or credentials
    (plan "message/data minimisation"). ``extra='forbid'`` turns any extra
    field into a contract violation, so such a row can never smuggle
    unapproved content into the broker path.

    It remains the contract for the never-dispatched outbox cleanup ledger row
    and for legacy version-1 maintenance rows written before the durable
    maintenance-run ledger existed (plan P4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class MaintenanceRunPayload(BaseModel):
    """Reference-only payload for a scheduled maintenance dispatch (plan P4).

    Contains exactly one field: the durable maintenance-run id. The sweep's
    task type, UTC bucket, attempt count, lease and outcome all live on that
    PostgreSQL row, so the broker message stays a pure reference — no tenant
    data, object keys, prompts, provider ids, URLs or credentials.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    maintenance_run_id: uuid.UUID


# Event type/version -> closed payload contract. Unknown pairs are rejected
# by :func:`validate_payload`; the registry (plan P3) extends this table with
# the actor message shapes, never with free-form payloads.
_PAYLOAD_CONTRACTS: dict[tuple[str, int], type[BaseModel]] = {
    (EVENT_TYPE_JOB_DISPATCH, EVENT_VERSION_JOB_DISPATCH): JobDispatchPayload,
    (EVENT_TYPE_AI_RETENTION, EVENT_VERSION_AI_RETENTION): MaintenanceRunPayload,
    (EVENT_TYPE_TRANSFER_RECONCILE, EVENT_VERSION_TRANSFER_RECONCILE): MaintenanceRunPayload,
    # Legacy in-flight rows from the previous release (empty payload).
    (EVENT_TYPE_AI_RETENTION, EVENT_VERSION_MAINTENANCE_LEGACY): MaintenancePayload,
    (EVENT_TYPE_TRANSFER_RECONCILE, EVENT_VERSION_MAINTENANCE_LEGACY): MaintenancePayload,
    (
        EVENT_TYPE_OUTBOX_CLEANUP_COMPLETED,
        EVENT_VERSION_OUTBOX_CLEANUP_COMPLETED,
    ): MaintenancePayload,
}


def dispatch_payload(job_id: uuid.UUID) -> dict[str, Any]:
    """Build the persisted JSON payload for a job dispatch event.

    ``job_id`` is serialised as a string so the payload round-trips through
    JSONB exactly as the worker-facing contract expects.
    """
    return JobDispatchPayload(job_id=job_id).model_dump(mode="json")


def maintenance_payload(maintenance_run_id: uuid.UUID) -> dict[str, Any]:
    """Build the persisted JSON payload for a maintenance dispatch event.

    ``maintenance_run_id`` is serialised as a string so the payload
    round-trips through JSONB exactly as the worker-facing contract expects.
    """
    return MaintenanceRunPayload(maintenance_run_id=maintenance_run_id).model_dump(mode="json")


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
