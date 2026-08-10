"""Tests for the WorkOS webhook consumer (Scope §6.8).

Three layers, mirroring the invitations and bootstrap modules' split:

- payload-parsing checks exercise ``parse_webhook_event`` directly: known and
  unknown event types parse, malformed deliveries raise the standard 400;
- request-flow tests drive the full ASGI stack with the in-memory fakes from
  ``context_helpers.py``, proving the signature gate (valid -> processed,
  missing/wrong/stale signature -> 401), the best-effort refreshes
  (``invitation.revoked`` mirrors locally and audits, ``user.deleted``
  deactivates and audits), the deliberate no-ops (unknown events,
  ``invitation.accepted`` never grants, terminal invitations untouched) and
  the acceptance §5.9 invariant: a login without any webhook delivery still
  links the invitation, and a webhook-revoked invitation never grants;
- the real-database proofs live in ``test_webhooks_db.py``.

The configured webhook secret comes from ``WORKOS_WEBHOOK_SECRET`` via
``get_settings``; tests monkeypatch the settings accessor on the dependencies
module, exactly like the bootstrap tests patch the service module's accessor.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient, Response
from tests.auth_helpers import generate_key_pair, make_token, webhook_signature_header
from tests.context_helpers import (
    ContextState,
    build_context_app,
    context_client,
    make_invitation,
    make_membership,
    make_organisation,
    make_role,
    make_user,
)

import app.api.dependencies as dependencies_module
from app.core.exceptions import BadRequestError
from app.modules.audit.service import (
    ACTION_INVITATION_ACCEPTED,
    ACTION_INVITATION_REVOKED,
    ACTION_MEMBERSHIP_ROLE_CHANGED,
    ACTION_USER_DEACTIVATED,
)
from app.modules.invitations.models import InvitationStatus
from app.modules.organisations.models import MembershipStatus
from app.modules.webhooks.schemas import MAX_WEBHOOK_PAYLOAD_BYTES, parse_webhook_event

SECRET = "whsec_test"


def _configure_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the dependencies module's settings accessor at the test secret."""
    monkeypatch.setattr(
        dependencies_module,
        "get_settings",
        lambda: SimpleNamespace(workos_webhook_secret=SECRET),
    )


def _signature_header(
    payload: bytes, *, secret: str = SECRET, timestamp_ms: int | None = None
) -> str:
    return webhook_signature_header(payload, secret, timestamp_ms or int(time.time() * 1000))


async def _deliver(
    client: AsyncClient,
    payload: dict[str, Any],
    *,
    secret: str = SECRET,
    timestamp_ms: int | None = None,
    header: str | None = None,
) -> Response:
    """POST a signed WorkOS-style delivery; the header defaults to a valid one."""
    body = json.dumps(payload).encode()
    return await client.post(
        "/api/v1/webhooks/workos",
        content=body,
        headers={
            "workos-signature": header
            or _signature_header(body, secret=secret, timestamp_ms=timestamp_ms)
        },
    )


# --- Payload parsing (pure Python, no request involved) ---


def test_parse_webhook_event_accepts_a_known_delivery() -> None:
    event = parse_webhook_event(
        b'{"id":"evt_1","event":"invitation.revoked","data":{"id":"inv_1"}}'
    )
    assert event.event == "invitation.revoked"
    assert event.id == "evt_1"
    assert event.data["id"] == "inv_1"
    assert event.is_known_type is True


def test_parse_webhook_event_tolerates_unknown_types() -> None:
    event = parse_webhook_event(b'{"event":"some.future.event","data":{}}')
    assert event.event == "some.future.event"
    assert event.is_known_type is False


def test_parse_webhook_event_rejects_malformed_json() -> None:
    with pytest.raises(BadRequestError):
        parse_webhook_event(b"not-json")


def test_parse_webhook_event_rejects_non_object_payload() -> None:
    with pytest.raises(BadRequestError):
        parse_webhook_event(b'["an", "array"]')


def test_parse_webhook_event_rejects_missing_event_type() -> None:
    with pytest.raises(BadRequestError):
        parse_webhook_event(b'{"id":"evt_1","data":{}}')


# --- Signature gate (acceptance §5.9) ---


async def test_verified_delivery_is_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(client, {"event": "unknown.event", "data": {}})

    assert response.status_code == 200
    assert response.json() == {"processed": True}
    assert state.audit_events == []  # unknown events change nothing


async def test_second_precision_timestamp_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """WorkOS documents milliseconds but publishes examples in seconds."""
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(
            client,
            {"event": "unknown.event", "data": {}},
            timestamp_ms=int(time.time()),  # seconds precision
        )

    assert response.status_code == 200


async def test_missing_signature_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/webhooks/workos",
            content=b'{"event":"invitation.revoked"}',
        )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_webhook_signature"


async def test_wrong_secret_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(client, {"event": "unknown.event"}, secret="whsec_wrong")

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_webhook_signature"


async def test_stale_signature_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid signature older than the 300s tolerance is replayed and rejected."""
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(
            client,
            {"event": "unknown.event"},
            timestamp_ms=int(time.time() * 1000) - 10 * 60 * 1000,
        )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_webhook_signature"


async def test_unset_secret_rejects_fail_closed() -> None:
    """Without WORKOS_WEBHOOK_SECRET the endpoint rejects every delivery."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(client, {"event": "unknown.event"}, secret="any-secret")

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_webhook_signature"


# --- Malformed payloads ---


async def test_malformed_payload_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/webhooks/workos",
            content=b"not-json",
            headers={"workos-signature": _signature_header(b"not-json")},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_webhook_payload"


async def test_payload_without_event_type_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    app = build_context_app(private_key=generate_key_pair()[0], state=state)
    body = b'{"id":"evt_1","data":{}}'

    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/webhooks/workos",
            content=body,
            headers={"workos-signature": _signature_header(body)},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_webhook_payload"


async def test_oversized_content_length_rejected_before_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared Content-Length above the cap is rejected before buffering."""
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    app = build_context_app(private_key=generate_key_pair()[0], state=state)
    body = b'{"event":"unknown.event","data":{}}'

    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/webhooks/workos",
            content=body,
            headers={
                "workos-signature": _signature_header(body),
                "content-length": str(MAX_WEBHOOK_PAYLOAD_BYTES + 1),
            },
        )

    assert response.status_code == 400
    assert response.json()["code"] == "webhook_payload_invalid"


# --- Best-effort invitation refresh ---


def _staged_invitation(state: ContextState, *, status: InvitationStatus = InvitationStatus.SENT):
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    inviter = make_user(workos_user_id="user_inviter")
    invitation = make_invitation(
        organisation.id,
        inviter.id,
        workos_invitation_id="inv_workos_1",
        email="ada@example.com",
        status=status,
    )
    state.organisations = [organisation]
    state.invitations = [invitation]
    return invitation


async def test_invitation_revoked_webhook_mirrors_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WorkOS-side revocation flips the local row and writes the audit trail."""
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitation = _staged_invitation(state)
    state.lookup_queue = [invitation]  # scalar() -> the local invitation row
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(
            client,
            {
                "id": "evt_revoke_1",
                "event": "invitation.revoked",
                "data": {"id": "inv_workos_1", "state": "revoked"},
            },
        )

    assert response.status_code == 200
    assert invitation.status == InvitationStatus.REVOKED
    assert len(state.audit_events) == 1
    event = state.audit_events[0]
    assert event.action == ACTION_INVITATION_REVOKED
    assert event.organisation_id == invitation.organisation_id
    assert event.actor_user_id is None  # system-driven, not an admin action
    assert event.resource_id == str(invitation.id)
    assert event.event_metadata["source"] == "webhook"
    assert event.event_metadata["workos_event_id"] == "evt_revoke_1"


async def test_invitation_revoked_unknown_workos_id_is_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    _staged_invitation(state)
    state.lookup_queue = [None]  # no local row carries this WorkOS invitation id
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(
            client,
            {"event": "invitation.revoked", "data": {"id": "inv_workos_unknown"}},
        )

    assert response.status_code == 200
    assert state.invitations[0].status == InvitationStatus.SENT  # untouched
    assert state.audit_events == []


async def test_invitation_revoked_never_touches_terminal_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-accepted invitation is terminal; the webhook cannot rewrite it."""
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitation = _staged_invitation(state, status=InvitationStatus.ACCEPTED)
    state.lookup_queue = [invitation]
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(
            client,
            {"event": "invitation.revoked", "data": {"id": "inv_workos_1"}},
        )

    assert response.status_code == 200
    assert invitation.status == InvitationStatus.ACCEPTED
    assert state.audit_events == []


async def test_invitation_accepted_webhook_never_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    """A grant is never produced by a webhook: no membership, no local flip.

    ``invitation.accepted`` is a deliberate no-op: only the login-time
    reconciliation (Scope §6.5) writes the local ``accepted`` status — and only
    together with the membership grant it represents. Flipping the status here
    would silently prevent that grant.
    """
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitation = _staged_invitation(state)
    state.lookup_queue = [None]  # the consumer reads nothing for this event type
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(
            client,
            {
                "event": "invitation.accepted",
                "data": {"id": "inv_workos_1", "state": "accepted"},
            },
        )

    assert response.status_code == 200
    assert invitation.status == InvitationStatus.SENT  # still grantable locally
    assert state.memberships == []  # no membership was created
    assert state.audit_events == []


# --- Best-effort user-lifecycle refresh ---


async def test_user_deleted_webhook_deactivates_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    user = make_user(workos_user_id="user_gone", is_active=True)
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(
            client,
            {
                "id": "evt_user_1",
                "event": "user.deleted",
                "data": {"id": "user_gone", "email": "ada@example.com"},
            },
        )

    assert response.status_code == 200
    assert user.is_active is False
    assert len(state.audit_events) == 1
    event = state.audit_events[0]
    assert event.action == ACTION_USER_DEACTIVATED
    assert event.actor_user_id is None
    assert event.resource_type == "user"
    assert event.resource_id == str(user.id)
    assert event.event_metadata["source"] == "webhook"


async def test_user_deleted_unknown_user_is_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    state.lookup_queue = [None]
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(client, {"event": "user.deleted", "data": {"id": "user_unknown"}})

    assert response.status_code == 200
    assert state.audit_events == []


async def test_user_deleted_redelivery_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeated user.deleted delivery changes nothing and audits nothing twice."""
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    user = make_user(workos_user_id="user_gone", is_active=False)
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    app = build_context_app(private_key=generate_key_pair()[0], state=state)

    async with context_client(app) as client:
        response = await _deliver(client, {"event": "user.deleted", "data": {"id": "user_gone"}})

    assert response.status_code == 200
    assert user.is_active is False  # already inactive
    assert state.audit_events == []  # no new audit row


# --- Webhooks never break the authoritative login-time reconciliation ---


async def test_login_without_any_webhook_delivery_still_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance §5.9: a login links the invitation even if no webhook arrived."""
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitee = make_user(workos_user_id="user_invitee")
    state.users[invitee.workos_user_id] = invitee
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    member_role = make_role("member", "Member")
    invitation = make_invitation(organisation.id, invitee.id, email="ada@example.com")
    invitation.workos_invitation_id = "inv_workos_1"
    state.invitations = [invitation]

    membership = make_membership(invitee, organisation.id)
    # user; the linking pass resolves the role, finds no membership, grants it
    state.lookup_queue = [invitee, member_role, None]
    # /me payload: memberships, org role codes, platform role codes
    state.scalars_queue = [[(membership, organisation.name)], ["member"], []]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    # No webhook delivery happens anywhere in this test.
    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/me",
            headers={
                "Authorization": f"Bearer {make_token(private_key, sub=invitee.workos_user_id)}"
            },
        )

    assert response.status_code == 200
    assert len(state.memberships) == 1
    assert state.memberships[0].user_id == invitee.id
    assert state.memberships[0].status == MembershipStatus.ACTIVE
    assert invitation.status == InvitationStatus.ACCEPTED
    actions = {event.action for event in state.audit_events}
    assert ACTION_INVITATION_ACCEPTED in actions
    assert ACTION_MEMBERSHIP_ROLE_CHANGED in actions


async def test_webhook_revoked_invitation_never_grants_at_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A webhook-revoked invitation is excluded from the grant path (acceptance §5.6/§5.9)."""
    _configure_secret(monkeypatch)
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitee = make_user(workos_user_id="user_invitee")
    state.users[invitee.workos_user_id] = invitee
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(organisation.id, invitee.id, email="ada@example.com")
    invitation.workos_invitation_id = "inv_workos_1"
    state.invitations = [invitation]
    private_key, _ = generate_key_pair()

    # Delivery 1: WorkOS revokes the invitation; the consumer mirrors it.
    state.lookup_queue = [invitation]
    app = build_context_app(private_key=private_key, state=state)
    async with context_client(app) as client:
        response = await _deliver(
            client, {"event": "invitation.revoked", "data": {"id": "inv_workos_1"}}
        )
    assert response.status_code == 200
    assert invitation.status == InvitationStatus.REVOKED

    # Delivery 2: the invitee logs in. The revoked invitation must not grant.
    state.lookup_queue = [invitee]  # user only; the linking pass sees REVOKED
    state.scalars_queue = [[], [], []]  # /me payload with no memberships
    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/me",
            headers={
                "Authorization": f"Bearer {make_token(private_key, sub=invitee.workos_user_id)}"
            },
        )

    assert response.status_code == 200
    assert state.memberships == []  # never granted
    assert invitation.status == InvitationStatus.REVOKED
    revoked_audits = [e for e in state.audit_events if e.action == ACTION_INVITATION_REVOKED]
    assert len(revoked_audits) == 1
    assert not [e for e in state.audit_events if e.action == ACTION_INVITATION_ACCEPTED]
