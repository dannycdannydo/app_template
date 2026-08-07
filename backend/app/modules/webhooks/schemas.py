"""Webhook payload schemas (Scope §6.8, blueprint §13, §30).

The application parses WorkOS deliveries itself instead of depending on the
SDK's event deserialiser, keeping the SDK fully behind the adapter boundary
(AGENTS.md: provider SDKs stay behind adapters) and giving the suite a stable,
versioned contract to test against. All data fields are lenient
(``extra="ignore"``, optional identifiers): the consumer is best-effort by
design, so a delivery that does not carry the identifiers the local refresh
needs is tolerated as a no-op rather than rejected.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.core.exceptions import BadRequestError

# WorkOS invitation/user identifiers in the event payloads the consumer acts on.
_KNOWN_EVENT_TYPES = frozenset(
    {
        "invitation.created",
        "invitation.accepted",
        "invitation.revoked",
        "user.created",
        "user.updated",
        "user.deleted",
    }
)

# BP §30 "input limits": WorkOS deliveries are small JSON objects; anything
# beyond this is rejected before parsing (defence against oversized payloads).
MAX_WEBHOOK_PAYLOAD_BYTES = 1_000_000


class WebhookResponse(BaseModel):
    """Acknowledgement returned for a successfully verified delivery.

    ``processed`` is always true once the signature verified and the payload
    parsed — the consumer's best-effort refresh may or may not have applied a
    local state change, and the response deliberately does not leak which, so
    nothing about local state can be inferred from the reply.
    """

    processed: bool = True


class InvitationEventData(BaseModel):
    """The ``data`` object of an ``invitation.*`` delivery (lenient)."""

    model_config = {"extra": "ignore"}

    id: str | None = None
    """The WorkOS invitation id; local rows are matched on this."""

    state: str | None = None
    email: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    accepted_at: str | None = None
    organization_id: str | None = None


class UserEventData(BaseModel):
    """The ``data`` object of a ``user.*`` delivery (lenient)."""

    model_config = {"extra": "ignore"}

    id: str | None = None
    """The WorkOS user id; local users are matched on this."""

    email: str | None = None


class WorkOSWebhookEvent(BaseModel):
    """One signed WorkOS webhook delivery, envelope and raw data."""

    model_config = {"extra": "ignore"}

    id: str | None = None
    """WorkOS event id; recorded in audit metadata for traceability."""

    event: str = Field(min_length=1)
    """The event type, e.g. ``invitation.revoked``; unknown types are tolerated."""

    data: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None

    @property
    def is_known_type(self) -> bool:
        """True for event types the consumer explicitly understands."""
        return self.event in _KNOWN_EVENT_TYPES


def parse_webhook_event(payload: bytes) -> WorkOSWebhookEvent:
    """Parse a verified raw delivery into a typed event (raises on malformed).

    Malformed JSON or a payload without an event type is a 400 — a signed
    delivery that is not a usable WorkOS event should stop WorkOS's retries
    rather than be silently absorbed. Unknown but well-formed event types are
    the consumer's no-op case, not an error here.
    """
    try:
        raw: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequestError(
            code="invalid_webhook_payload",
            message="The webhook payload is not valid JSON.",
        ) from exc
    if not isinstance(raw, dict):
        raise BadRequestError(
            code="invalid_webhook_payload",
            message="The webhook payload must be a JSON object.",
        )
    try:
        return WorkOSWebhookEvent.model_validate(raw)
    except ValidationError as exc:
        # A payload without a usable event type cannot be dispatched; reject it
        # so WorkOS stops retrying instead of absorbing a broken delivery.
        raise BadRequestError(
            code="invalid_webhook_payload",
            message="The webhook payload is not a valid WorkOS event.",
        ) from exc
