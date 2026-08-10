"""Tests for the platform invitation lifecycle (Scope §6.5).

Three layers, mirroring the audit and bootstrap modules' split:

- metadata and query-construction checks are pure Python and run everywhere;
- request-flow tests drive the full ASGI stack with the in-memory fakes from
  ``context_helpers.py``, proving the invite/revoke/list endpoints, the
  login-time linking hook inside ``get_current_user``, the never-grant cases
  (revoked, expired, mismatched, unverified) and the idempotent/race-safe
  acceptance — all without PostgreSQL or a network;
- the real-database proofs (the migration applies, the constraints hold, the
  invite→accept journey round-trips through the real services) live in
  ``test_invitations_db.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from httpx import AsyncClient, Response
from sqlalchemy import Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from tests.auth_helpers import generate_key_pair, make_token
from tests.context_helpers import (
    ContextState,
    FakeProfileClient,
    FakeSession,
    build_context_app,
    context_client,
    make_invitation,
    make_membership,
    make_organisation,
    make_role,
    make_user,
)

from app.core.security import UserProfile
from app.db.base import Base
from app.modules.audit.service import (
    ACTION_INVITATION_ACCEPTED,
    ACTION_INVITATION_REVOKED,
    ACTION_INVITATION_SENT,
    ACTION_MEMBERSHIP_ROLE_CHANGED,
)
from app.modules.invitations import service
from app.modules.invitations.models import Invitation, InvitationStatus
from app.modules.invitations.queries import (
    invitations_count_statement,
    invitations_statement,
    pending_invitations_statement,
)
from app.modules.organisations.models import MembershipStatus
from app.modules.platform_admin.service import ACTION_ORGANISATION_WORKOS_MAPPED
from app.modules.users.models import User

INVITEE_EMAIL = "ada@example.com"  # the default fake profile email


def _table_of(model: type[Base]) -> Table:
    """Return the mapped :class:`Table` with precise typing for introspection."""
    return cast(Table, model.__table__)


def _make_platform_admin_user(state: ContextState) -> User:
    user = make_user(workos_user_id="user_platform_admin")
    state.users[user.workos_user_id] = user
    return user


async def _invite(
    client: AsyncClient,
    token: str,
    organisation_id: uuid.UUID,
    *,
    email: str = "ada@example.com",
    role_code: str = "member",
) -> Response:
    return await client.post(
        f"/api/v1/platform/organisations/{organisation_id}/invitations",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": email, "role_code": role_code},
    )


# --- Model metadata (Scope §6.5, design plan §2.3) ---


def test_invitations_table_registered_on_base_metadata() -> None:
    assert "invitations" in Base.metadata.tables


def test_invitation_columns_follow_scope_shape() -> None:
    table = _table_of(Invitation)
    assert not table.c.organisation_id.nullable
    assert not table.c.email.nullable
    assert not table.c.role_code.nullable
    assert not table.c.invited_by_user_id.nullable
    assert not table.c.expires_at.nullable
    # The WorkOS invitation id is assigned once sent; nullable and unique so a
    # row never collides on NULL while sent rows stay unique.
    assert table.c.workos_invitation_id.nullable is True
    unique_names = {constraint.name for constraint in table.constraints}
    assert "uq_invitations_workos_invitation_id" in unique_names
    assert "ck_invitations_invitation_status" in unique_names


def test_invitation_indexes_cover_the_hot_queries() -> None:
    table = _table_of(Invitation)
    indexed_columns = {column for index in table.indexes for column in index.expressions}
    assert table.c.organisation_id in indexed_columns
    assert table.c.email in indexed_columns
    assert table.c.invited_by_user_id in indexed_columns


# --- Query construction (the WHERE clauses the fake cannot apply) ---


def test_pending_invitations_statement_selects_only_grantable_invitations() -> None:
    statement = pending_invitations_statement("ADA@Example.COM")
    compiled = str(statement.compile(dialect=postgresql.dialect())).lower()
    # Case-insensitive email match against the authenticated user's email
    # (literals are bound as parameters; the columns and lower() are inline).
    assert "lower(invitations.email)" in compiled
    # Only pending (sent) invitations that have not expired can grant.
    assert "invitations.status" in compiled
    assert "invitations.expires_at" in compiled


def test_invitations_statement_filters_by_organisation() -> None:
    org_id = uuid.uuid4()
    statement = invitations_statement(organisation_id=org_id)
    compiled = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "invitations.organisation_id" in compiled


def test_invitations_count_statement_counts_the_filtered_set() -> None:
    statement = invitations_count_statement(organisation_id=uuid.uuid4())
    compiled = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "count(" in compiled
    assert "invitations" in compiled


def test_invitation_actions_are_stable() -> None:
    """Acceptance §5.6: the audited action codes are exactly these strings."""
    assert ACTION_INVITATION_SENT == "invitation.sent"
    assert ACTION_INVITATION_REVOKED == "invitation.revoked"
    assert ACTION_INVITATION_ACCEPTED == "invitation.accepted"
    assert ACTION_MEMBERSHIP_ROLE_CHANGED == "membership.role_changed"


# --- Platform invite endpoint ---


async def test_invite_user_endpoint_sends_workos_and_records_locally() -> None:
    """Acceptance §5.6: inviting writes a row and calls the adapter (stubbed)."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    member_role = make_role("member", "Member")
    state.granted_permissions = {"platform.admin"}
    # get_current_user -> user; invite flow -> org, role, no pending duplicate
    state.lookup_queue = [actor, organisation, member_role, None]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await _invite(client, make_token(private_key), organisation.id)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == INVITEE_EMAIL
    assert body["role_code"] == "member"
    assert body["status"] == "sent"
    assert body["workos_invitation_id"]  # the fake adapter recorded the send

    # The local row exists with the WorkOS delivery id and expiry mirror.
    assert len(state.invitations) == 1
    invitation = state.invitations[0]
    assert invitation.email == INVITEE_EMAIL
    assert invitation.workos_invitation_id == body["workos_invitation_id"]
    assert invitation.expires_at is not None
    # No membership is created at invite time (acceptance §5.6).
    assert state.memberships == []
    # The adapter really was called and the send was audited.
    assert len(state.workos_invitations) == 1
    assert state.workos_invitations[0].email == INVITEE_EMAIL
    assert len(state.audit_events) == 1
    assert state.audit_events[0].action == ACTION_INVITATION_SENT
    assert state.audit_events[0].organisation_id == organisation.id
    assert state.audit_events[0].actor_user_id == actor.id


async def test_invite_user_endpoint_backfills_workos_organisation_lazily() -> None:
    """Scope §6.3/§6.5: a pre-existing org gains its mapping at first invite."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    legacy_org = make_organisation(workos_organisation_id=None)
    member_role = make_role("member", "Member")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, legacy_org, member_role, None]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await _invite(client, make_token(private_key), legacy_org.id)

    assert response.status_code == 201
    assert legacy_org.workos_organisation_id is not None  # backfilled in place
    assert str(legacy_org.id) in state.workos_organisations  # WorkOS org created
    actions = [event.action for event in state.audit_events]
    assert ACTION_INVITATION_SENT in actions
    assert ACTION_ORGANISATION_WORKOS_MAPPED in actions


async def test_invite_unknown_role_is_rejected_before_workos() -> None:
    """The intended role must exist in the catalogue; nothing is sent."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, organisation, None]  # role not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await _invite(
            client, make_token(private_key), organisation.id, role_code="no_such_role"
        )

    assert response.status_code == 400
    assert response.json()["code"] == "unknown_role"
    assert state.invitations == []
    assert state.workos_invitations == []
    assert state.audit_events == []


async def test_invite_unknown_organisation_is_404() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, None]  # organisation not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await _invite(client, make_token(private_key), uuid.uuid4())

    assert response.status_code == 404
    assert response.json()["code"] == "organisation_not_found"
    assert state.invitations == []
    assert state.audit_events == []


async def test_invite_duplicate_pending_invitation_is_409() -> None:
    """One grantable invitation per email per org, so linking stays unambiguous."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    member_role = make_role("member", "Member")
    existing = make_invitation(organisation.id, actor.id, email=INVITEE_EMAIL)
    state.invitations = [existing]
    state.granted_permissions = {"platform.admin"}
    # The platform admin's verified email differs from the invite email, so
    # the login-time link hook never consumes the queue for this invitation.
    state.profile = UserProfile(email="admin@example.com", name="Admin", email_verified=True)
    # user; invite flow -> org, role, the already-pending invitation
    state.lookup_queue = [actor, organisation, member_role, existing]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await _invite(client, make_token(private_key), organisation.id)

    assert response.status_code == 409
    assert response.json()["code"] == "invitation_pending_exists"
    assert len(state.invitations) == 1  # nothing new was inserted
    assert state.workos_invitations == []
    assert state.audit_events == []


async def test_invite_endpoint_requires_platform_admin() -> None:
    """Scope §6.2: an org owner without platform membership is denied."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    # The organisation-owner bundle only: no platform.admin code.
    state.granted_permissions = {"organisation.manage", "users.manage_roles", "records.read"}
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await _invite(client, make_token(private_key), uuid.uuid4())

    assert response.status_code == 403
    assert response.json()["code"] == "platform_admin_required"
    assert state.invitations == []
    assert state.audit_events == []


# --- Platform invite listing ---


async def test_list_invitations_endpoint_returns_paginated_rows() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    first = make_invitation(organisation.id, actor.id, email="one@example.com")
    second = make_invitation(organisation.id, actor.id, email="two@example.com")
    state.invitations = [first, second]
    state.granted_permissions = {"platform.admin"}
    # user; listing -> org, total (the fake answers the rows by entity)
    state.lookup_queue = [actor, organisation, 2]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/platform/organisations/{organisation.id}/invitations",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert {item["email"] for item in body["items"]} == {"one@example.com", "two@example.com"}


async def test_list_invitations_unknown_organisation_is_404() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, None]  # organisation not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/platform/organisations/{uuid.uuid4()}/invitations",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "organisation_not_found"


# --- Platform revoke endpoint ---


async def test_revoke_invitation_endpoint_revokes_workos_and_locally() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(
        organisation.id,
        actor.id,
        email="invitee@example.com",
        workos_invitation_id="inv_workos_sent",
    )
    state.invitations = [invitation]
    state.granted_permissions = {"platform.admin"}
    # user; revoke -> the invitation row (different email, so no linking fires)
    state.lookup_queue = [actor, invitation]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/platform/organisations/{organisation.id}/invitations/{invitation.id}",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    assert invitation.status == InvitationStatus.REVOKED
    # The WorkOS invitation was revoked through the adapter.
    assert state.revoked_workos_invitations == [invitation.workos_invitation_id]
    assert len(state.audit_events) == 1
    assert state.audit_events[0].action == ACTION_INVITATION_REVOKED


async def test_revoke_terminal_invitation_is_conflict() -> None:
    """Only a pending invitation can be revoked; terminal states stay put."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(
        organisation.id,
        actor.id,
        email="invitee@example.com",
        status=InvitationStatus.ACCEPTED,
        workos_invitation_id="inv_workos_sent",
    )
    state.invitations = [invitation]
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, invitation]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/platform/organisations/{organisation.id}/invitations/{invitation.id}",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "invitation_not_revocable"
    assert invitation.status == InvitationStatus.ACCEPTED
    assert state.audit_events == []


async def test_revoke_unknown_invitation_is_404() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, None]  # invitation not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/platform/organisations/{organisation.id}/invitations/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "invitation_not_found"
    assert state.audit_events == []


# --- Login-time linking: the authoritative acceptance point ---


async def _me(client: AsyncClient, token: str) -> Response:
    return await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})


async def test_full_invite_accept_journey_creates_membership_and_audits() -> None:
    """Acceptance §5.6: invite → no membership → login links and audits both."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    member_role = make_role("member", "Member")
    invitee = make_user(workos_user_id="user_invitee")
    state.users[invitee.workos_user_id] = invitee
    state.granted_permissions = {"platform.admin"}
    private_key, _ = generate_key_pair()

    # Request 1: the platform admin invites the invitee.
    state.lookup_queue = [actor, organisation, member_role, None]
    app = build_context_app(private_key=private_key, state=state)
    async with context_client(app) as client:
        response = await _invite(client, make_token(private_key), organisation.id)
    assert response.status_code == 201
    # No membership row exists before acceptance (acceptance §5.6).
    assert state.memberships == []

    # Request 2: the invitee signs in; the provisioning chain links the invite.
    membership = make_membership(invitee, organisation.id)
    state.lookup_queue = [invitee, member_role, None]  # user, role, no membership
    # /me payload: memberships, org role codes, platform role codes
    state.scalars_queue = [[(membership, organisation.name)], ["member"], []]
    async with context_client(app) as client:
        response = await _me(client, make_token(private_key, sub=invitee.workos_user_id))

    assert response.status_code == 200
    body = response.json()
    assert [item["organisation_id"] for item in body["memberships"]] == [str(organisation.id)]
    assert body["roles"] == ["member"]

    # The membership, the intended role and the invitation all settled.
    assert len(state.memberships) == 1
    assert state.memberships[0].user_id == invitee.id
    assert state.memberships[0].status == MembershipStatus.ACTIVE
    assert len(state.membership_roles) == 1
    assert state.invitations[0].status == InvitationStatus.ACCEPTED

    # Both events were audited: the acceptance and the role grant.
    actions = [event.action for event in state.audit_events]
    assert ACTION_INVITATION_SENT in actions
    assert ACTION_INVITATION_ACCEPTED in actions
    assert ACTION_MEMBERSHIP_ROLE_CHANGED in actions
    accepted = [e for e in state.audit_events if e.action == ACTION_INVITATION_ACCEPTED]
    assert accepted[0].organisation_id == organisation.id
    assert accepted[0].actor_user_id == invitee.id


async def test_repeat_login_after_acceptance_links_nothing() -> None:
    """Idempotence: a second login marks nothing new and creates no duplicate."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitee = make_user(workos_user_id="user_invitee")
    state.users[invitee.workos_user_id] = invitee
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(organisation.id, invitee.id, email=INVITEE_EMAIL)
    invitation.status = InvitationStatus.ACCEPTED  # already linked by a prior login
    state.invitations = [invitation]
    membership = make_membership(invitee, organisation.id)
    state.memberships = [membership]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    # user; the linking pass finds the invitation already accepted and skips it
    state.lookup_queue = [invitee]
    state.scalars_queue = [[(membership, organisation.name)], ["member"], []]

    async with context_client(app) as client:
        response = await _me(client, make_token(private_key, sub=invitee.workos_user_id))

    assert response.status_code == 200
    assert state.memberships == [membership]  # no duplicate, no reactivation
    assert state.membership_roles == []
    assert state.audit_events == []  # nothing was linked again


async def test_revoked_invitation_never_grants() -> None:
    """Acceptance §5.6: a revoked invitation stays revoked and grants nothing."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitee = make_user(workos_user_id="user_invitee")
    state.users[invitee.workos_user_id] = invitee
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(
        organisation.id,
        invitee.id,
        email=INVITEE_EMAIL,
        status=InvitationStatus.REVOKED,
        workos_invitation_id="inv_workos_revoked",
    )
    state.invitations = [invitation]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    state.lookup_queue = [invitee]
    state.scalars_queue = [[], [], []]

    async with context_client(app) as client:
        response = await _me(client, make_token(private_key, sub=invitee.workos_user_id))

    assert response.status_code == 200
    assert state.memberships == []
    assert state.membership_roles == []
    assert invitation.status == InvitationStatus.REVOKED
    assert state.audit_events == []


async def test_expired_invitation_never_grants() -> None:
    """Acceptance §5.6: an expired invitation is ignored at login."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitee = make_user(workos_user_id="user_invitee")
    state.users[invitee.workos_user_id] = invitee
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(
        organisation.id,
        invitee.id,
        email=INVITEE_EMAIL,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    state.invitations = [invitation]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    state.lookup_queue = [invitee]
    state.scalars_queue = [[], [], []]

    async with context_client(app) as client:
        response = await _me(client, make_token(private_key, sub=invitee.workos_user_id))

    assert response.status_code == 200
    assert state.memberships == []
    assert invitation.status == InvitationStatus.SENT  # left untouched
    assert state.audit_events == []


async def test_email_mismatch_never_grants() -> None:
    """Acceptance §5.6: the authenticated WorkOS email must equal the invite."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitee = make_user(workos_user_id="user_invitee")
    state.users[invitee.workos_user_id] = invitee
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(organisation.id, invitee.id, email="other@example.com")
    state.invitations = [invitation]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    state.lookup_queue = [invitee]
    state.scalars_queue = [[], [], []]

    async with context_client(app) as client:
        response = await _me(client, make_token(private_key, sub=invitee.workos_user_id))

    assert response.status_code == 200
    assert state.memberships == []
    assert invitation.status == InvitationStatus.SENT
    assert state.audit_events == []


async def test_unverified_email_never_grants() -> None:
    """Acceptance §5.6: email_verified gates the privileged membership grant."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitee = make_user(workos_user_id="user_invitee")
    state.users[invitee.workos_user_id] = invitee
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(organisation.id, invitee.id, email=INVITEE_EMAIL)
    state.invitations = [invitation]
    state.profile = UserProfile(email=INVITEE_EMAIL, name="Ada Lovelace", email_verified=False)
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    state.lookup_queue = [invitee]
    state.scalars_queue = [[], [], []]

    async with context_client(app) as client:
        response = await _me(client, make_token(private_key, sub=invitee.workos_user_id))

    assert response.status_code == 200
    assert state.memberships == []
    assert invitation.status == InvitationStatus.SENT
    assert state.audit_events == []


async def test_existing_membership_accepts_without_mutating_it() -> None:
    """An invitation never silently changes an existing membership (Scope §6.6 owns that)."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    invitee = make_user(workos_user_id="user_invitee")
    state.users[invitee.workos_user_id] = invitee
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(organisation.id, invitee.id, email=INVITEE_EMAIL)
    state.invitations = [invitation]
    existing = make_membership(invitee, organisation.id)
    state.memberships = [existing]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    # user, role (resolved), the already-existing membership
    state.lookup_queue = [invitee, make_role("member", "Member"), existing]
    state.scalars_queue = [[(existing, organisation.name)], ["owner"], []]

    async with context_client(app) as client:
        response = await _me(client, make_token(private_key, sub=invitee.workos_user_id))

    assert response.status_code == 200
    assert state.memberships == [existing]
    assert state.membership_roles == []  # no role was force-assigned
    assert invitation.status == InvitationStatus.ACCEPTED
    actions = [event.action for event in state.audit_events]
    assert ACTION_INVITATION_ACCEPTED in actions
    assert ACTION_MEMBERSHIP_ROLE_CHANGED not in actions


# --- Race safety (service level, mirroring the bootstrap lost-race test) ---


async def test_lost_race_recovers_without_double_grant() -> None:
    """A concurrent first login that lost the race never double-grants.

    The winning login's membership is already committed when the loser's
    insert fires the unique (user_id, organisation_id) constraint; the loser
    rolls back, re-runs, sees the membership and only marks the invitation
    accepted.
    """
    state = ContextState(owner_role=make_role("owner", "Owner"))
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    invitee = make_user(workos_user_id="user_invitee")
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    invitation = make_invitation(organisation.id, invitee.id, email=INVITEE_EMAIL)
    state.invitations = [invitation]
    winner_membership = make_membership(invitee, organisation.id)
    state.memberships = [winner_membership]
    # pass 1: role found, no membership -> insert loses the race; pass 2: role
    # found, the winner's membership is visible -> nothing new is created.
    state.lookup_queue = [
        make_role("member", "Member"),
        None,
        make_role("member", "Member"),
        winner_membership,
    ]
    state.fail_commits = 1

    accepted = await service.link_invitation_on_login(session, invitee, FakeProfileClient(state))

    assert accepted == [invitation]
    assert state.memberships == [winner_membership]  # no duplicate
    assert state.membership_roles == []  # the losing grant was rolled back
    assert invitation.status == InvitationStatus.ACCEPTED
    actions = [event.action for event in state.audit_events]
    assert ACTION_INVITATION_ACCEPTED in actions
    assert ACTION_MEMBERSHIP_ROLE_CHANGED not in actions  # only the winner granted


async def test_no_pending_invitation_is_a_no_op() -> None:
    """The steady state: no grantable invitation means no WorkOS profile call."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    invitee = make_user(workos_user_id="user_invitee")
    profile_client = FakeProfileClient(state)

    accepted = await service.link_invitation_on_login(session, invitee, profile_client)

    assert accepted == []
    assert state.memberships == []
    assert state.audit_events == []
