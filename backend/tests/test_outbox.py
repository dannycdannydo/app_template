"""Pure unit tests for the outbox payload contracts (durable delivery plan P1).

These tests never touch SQL: they prove the closed payload contracts and the
fail-fast size bound that guard the broker path, so a persisted outbox payload
can never smuggle an actor/function name, a URL, a credential or tenant
content into the coordinator. The real-PostgreSQL data contract (atomic
commit/rollback, tenant association, deduplication, constraints) is proven in
``test_outbox_db.py``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.outbox.contracts import (
    AGGREGATE_TYPE_JOB,
    EVENT_TYPE_JOB_DISPATCH,
    EVENT_VERSION_JOB_DISPATCH,
    MAX_PAYLOAD_CHARS,
    JobDispatchPayload,
    OutboxContractError,
    dispatch_payload,
    validate_payload,
)
from app.modules.outbox.models import OutboxEventStatus
from app.modules.outbox.service import create_dispatch_event, create_schedule_event


class _RecordingSession:
    """Minimal stand-in for an ``AsyncSession`` that records added objects.

    The service functions only add to the session (commit/rollback ownership
    belongs to the caller), so a stub proves the built event shape without
    SQL. Type ``Any`` keeps static checkers happy.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def test_dispatch_payload_is_reference_only() -> None:
    """The persisted payload carries exactly the job id, nothing else."""
    job_id = uuid.uuid4()
    assert dispatch_payload(job_id) == {"job_id": str(job_id)}


def test_dispatch_payload_contract_forbids_extra_fields() -> None:
    """A payload with any extra key fails validation (extra='forbid')."""
    with pytest.raises(ValidationError):
        # Built from a dict so the invalid key is a runtime value, not a
        # statically-known attribute.
        JobDispatchPayload.model_validate(
            {"job_id": str(uuid.uuid4()), "actor_name": "app.tasks.run"}
        )


def test_validate_payload_accepts_canonical_dispatch_payload() -> None:
    job_id = uuid.uuid4()
    canonical = validate_payload(
        EVENT_TYPE_JOB_DISPATCH,
        EVENT_VERSION_JOB_DISPATCH,
        dispatch_payload(job_id),
    )
    assert canonical == {"job_id": str(job_id)}


def test_validate_payload_rejects_unknown_contract() -> None:
    """An event type/version with no contract is permanent and explicit."""
    with pytest.raises(OutboxContractError, match="unknown outbox event contract"):
        validate_payload("registry.smuggle", 1, {"job_id": str(uuid.uuid4())})


def test_validate_payload_rejects_invalid_shape() -> None:
    """Wrong field types and extra fields both violate the closed contract."""
    with pytest.raises(OutboxContractError, match="invalid"):
        validate_payload(
            EVENT_TYPE_JOB_DISPATCH, EVENT_VERSION_JOB_DISPATCH, {"job_id": "not-a-uuid"}
        )
    with pytest.raises(OutboxContractError, match="invalid"):
        validate_payload(
            EVENT_TYPE_JOB_DISPATCH,
            EVENT_VERSION_JOB_DISPATCH,
            {"job_id": str(uuid.uuid4()), "url": "https://example.com/secret"},
        )


async def test_create_dispatch_event_builds_tenant_scoped_event() -> None:
    """The event copies the organisation id and carries the job as aggregate."""
    session = _RecordingSession()
    organisation_id = uuid.uuid4()
    job_id = uuid.uuid4()

    event = await create_dispatch_event(
        cast(AsyncSession, session), organisation_id=organisation_id, job_id=job_id
    )

    assert session.added == [event]
    assert event.organisation_id == organisation_id
    assert event.aggregate_id == job_id
    assert event.event_type == EVENT_TYPE_JOB_DISPATCH
    assert event.event_version == EVENT_VERSION_JOB_DISPATCH
    assert event.aggregate_type == AGGREGATE_TYPE_JOB
    assert event.payload == {"job_id": str(job_id)}
    assert event.deduplication_key == f"{EVENT_TYPE_JOB_DISPATCH}:{job_id}"
    assert event.status == OutboxEventStatus.PENDING
    assert event.claim_token is None
    assert event.last_error is None
    # ``attempt_count`` (0) and ``available_at``/``created_at`` are flush-time
    # defaults (SQLAlchemy applies Python/server defaults on INSERT), so their
    # persisted values are asserted in the real-database tests.


async def test_create_schedule_event_has_no_tenant_context() -> None:
    """Maintenance events are global: no organisation, no aggregate, no data."""
    session = _RecordingSession()

    event = await create_schedule_event(
        cast(AsyncSession, session),
        event_type="ai.retention",
        schedule_key="ai.retention:2026-08-14T00",
    )

    assert event.organisation_id is None
    assert event.aggregate_type is None
    assert event.aggregate_id is None
    assert event.payload == {}
    assert event.deduplication_key == "ai.retention:2026-08-14T00"
    assert event.status == OutboxEventStatus.PENDING


async def test_create_schedule_event_rejects_unknown_event_type() -> None:
    """An event type/version with no closed contract is rejected outright.

    This closes the boundary the review probe exploited: arbitrary event
    types must never reach ``session.add``.
    """
    session = _RecordingSession()

    with pytest.raises(OutboxContractError, match="unknown outbox event contract"):
        await create_schedule_event(
            cast(AsyncSession, session),
            event_type="arbitrary.actor",
            schedule_key="arbitrary.actor:2026-08-14T00",
        )

    assert session.added == []


async def test_create_schedule_event_rejects_reference_bearing_payload() -> None:
    """Maintenance payloads are closed and empty: no refs, URLs or actor names."""
    session = _RecordingSession()

    for bad_payload in (
        {"actor_name": "app.tasks.run"},
        {"signed_url": "https://example.test/private"},
        {"schedule_key": "ai.retention:2026-08-14T00"},
    ):
        with pytest.raises(OutboxContractError, match="invalid"):
            await create_schedule_event(
                cast(AsyncSession, session),
                event_type="ai.retention",
                schedule_key="ai.retention:2026-08-14T00",
                payload=bad_payload,
            )

    assert session.added == []


async def test_create_schedule_event_canonicalises_valid_payload() -> None:
    """A conforming payload is canonicalised through the closed contract."""
    session = _RecordingSession()

    event = await create_schedule_event(
        cast(AsyncSession, session),
        event_type="ai.transfer_reconcile",
        schedule_key="ai.transfer_reconcile:2026-08-14T00",
        payload={},
    )

    assert event.event_type == "ai.transfer_reconcile"
    assert event.event_version == 1
    assert event.payload == {}
    assert event.deduplication_key == "ai.transfer_reconcile:2026-08-14T00"


async def test_create_schedule_event_rejects_oversized_payload() -> None:
    """A payload over the database bound fails fast before any SQL."""
    session = _RecordingSession()

    with pytest.raises(OutboxContractError, match="exceeds"):
        await create_schedule_event(
            cast(AsyncSession, session),
            event_type="ai.retention",
            schedule_key="ai.retention:2026-08-14T00",
            payload={"blob": "x" * (MAX_PAYLOAD_CHARS + 1)},
        )

    assert session.added == []
