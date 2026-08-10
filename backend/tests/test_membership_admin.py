"""Tests for platform membership administration (Scope §6.6).

Mirrors the invitations module's split:

- metadata, query-construction and action-code checks are pure Python and run
  everywhere;
- request-flow tests drive the full ASGI stack with the in-memory fakes from
  ``context_helpers.py``, proving the list / assign-role / remove-role /
  suspend / reactivate / remove endpoints, the idempotent no-ops, the
  suspension enforcement against org routes (403 ``not_a_member`` via the
  existing active-membership check) and the invitation revocation on
  suspension and removal — all without PostgreSQL or a network;
- the real-database proofs (persistence, the membership_roles cascade, the
  audit rows) live in ``test_membership_admin_db.py``.

Queue notes for the fakes: the platform permission check consumes the
``scalars_queue`` if it is non-empty, so platform-route tests keep it empty
and stage memberships/roles in ``state``, letting the entity-based answers in
``FakeSession.scalars`` serve the service queries (which re-filter in Python,
exactly like the invitation-linking service).
"""

from __future__ import annotations

import uuid
from typing import cast

from httpx import AsyncClient, Response
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from tests.auth_helpers import generate_key_pair, make_token
from tests.context_helpers import (
    ContextState,
    FakeSession,
    FakeWorkOSInvitationsProvider,
    build_context_app,
    context_client,
    make_invitation,
    make_membership,
    make_membership_role,
    make_organisation,
    make_role,
    make_user,
)

from app.core.security import UserProfile
from app.modules.audit.service import (
    ACTION_INVITATION_REVOKED,
    ACTION_MEMBERSHIP_REACTIVATED,
    ACTION_MEMBERSHIP_REMOVED,
    ACTION_MEMBERSHIP_ROLE_CHANGED,
    ACTION_MEMBERSHIP_SUSPENDED,
)
from app.modules.invitations.models import InvitationStatus
from app.modules.organisations.models import MembershipStatus
from app.modules.platform_admin.queries import (
    memberships_count_statement,
    memberships_statement,
)
from app.modules.platform_admin.service import (
    assign_role,
    list_memberships,
    remove_role,
    set_membership_status,
)
from app.modules.users.models import User


def _make_platform_admin_user(state: ContextState) -> User:
    user = make_user(workos_user_id="user_platform_admin")
    state.users[user.workos_user_id] = user
    return user


def _make_member(state: ContextState, *, workos_user_id: str, email: str) -> User:
    member = make_user(workos_user_id=workos_user_id)
    member.email = email
    state.users[member.workos_user_id] = member
    return member


async def _get_memberships(client: AsyncClient, token: str, organisation_id: uuid.UUID) -> Response:
    return await client.get(
        f"/api/v1/platform/organisations/{organisation_id}/memberships",
        headers={"Authorization": f"Bearer {token}"},
    )


# --- Model and query metadata (Scope §6.6) ---


def test_memberships_statement_filters_by_organisation() -> None:
    org_id = uuid.uuid4()
    statement = memberships_statement(organisation_id=org_id)
    compiled = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "organisation_memberships.organisation_id" in compiled


def test_memberships_count_statement_counts_the_filtered_set() -> None:
    statement = memberships_count_statement(organisation_id=uuid.uuid4())
    compiled = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "count(" in compiled
    assert "organisation_memberships" in compiled


def test_membership_actions_are_stable() -> None:
    """Blueprint §29 / design plan §5: the audited action codes are exact."""
    assert ACTION_MEMBERSHIP_ROLE_CHANGED == "membership.role_changed"
    assert ACTION_MEMBERSHIP_SUSPENDED == "membership.suspended"
    assert ACTION_MEMBERSHIP_REACTIVATED == "membership.reactivated"
    assert ACTION_MEMBERSHIP_REMOVED == "membership.removed"


# --- Platform listing endpoint ---


async def test_list_memberships_endpoint_returns_paginated_rows() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    linus = _make_member(state, workos_user_id="user_linus", email="linus@example.com")
    member_role = make_role("member", "Member")
    viewer_role = make_role("viewer", "Viewer")
    state.roles = [member_role, viewer_role]
    grace_membership = make_membership(grace, organisation.id)
    linus_membership = make_membership(linus, organisation.id)
    state.memberships = [grace_membership, linus_membership]
    state.membership_roles = [
        make_membership_role(grace_membership.id, member_role.id),
        make_membership_role(linus_membership.id, viewer_role.id),
    ]
    state.granted_permissions = {"platform.admin"}
    # user; listing -> org, total; detail per membership -> user row
    state.lookup_queue = [actor, organisation, 2, grace, linus]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await _get_memberships(client, make_token(private_key), organisation.id)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 50
    items = {item["user_email"]: item for item in body["items"]}
    assert items["grace@example.com"]["roles"] == ["member"]
    assert items["grace@example.com"]["status"] == "active"
    assert items["linus@example.com"]["roles"] == ["viewer"]


async def test_list_memberships_unknown_organisation_is_404() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, None]  # organisation not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await _get_memberships(client, make_token(private_key), uuid.uuid4())

    assert response.status_code == 404
    assert response.json()["code"] == "organisation_not_found"


async def test_list_memberships_requires_platform_admin() -> None:
    """Scope §6.2: an org owner without platform membership is denied."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    state.granted_permissions = {"organisation.manage", "users.manage_roles", "records.read"}
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await _get_memberships(client, make_token(private_key), uuid.uuid4())

    assert response.status_code == 403
    assert response.json()["code"] == "platform_admin_required"


# --- Role assignment ---


async def test_assign_role_endpoint_grants_and_audits() -> None:
    """Scope §6.6: a role grant round-trips and writes membership.role_changed."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    member_role = make_role("member", "Member")
    manager_role = make_role("manager", "Manager")
    state.roles = [member_role, manager_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    state.membership_roles = [make_membership_role(membership.id, member_role.id)]
    state.granted_permissions = {"platform.admin"}
    # user; org, membership, role, no held grant; detail -> user
    state.lookup_queue = [actor, organisation, membership, manager_role, None, grace]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.post(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{membership.id}/roles",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
            json={"role_code": "manager"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["user_email"] == "grace@example.com"
    assert body["roles"] == ["manager", "member"]
    assert len(state.membership_roles) == 2
    assert len(state.audit_events) == 1
    event = state.audit_events[0]
    assert event.action == ACTION_MEMBERSHIP_ROLE_CHANGED
    assert event.event_metadata["action"] == "assigned"
    assert event.event_metadata["role_code"] == "manager"
    assert event.organisation_id == organisation.id
    assert event.actor_user_id == actor.id
    assert event.resource_type == "membership"
    assert event.resource_id == str(membership.id)


async def test_assign_role_unknown_role_is_400() -> None:
    """The role must exist in the catalogue; nothing is granted or audited."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, organisation, membership, None]  # role not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.post(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{membership.id}/roles",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
            json={"role_code": "no_such_role"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "unknown_role"
    assert state.membership_roles == []
    assert state.audit_events == []


async def test_assign_role_duplicate_is_an_idempotent_no_op() -> None:
    """A role the membership already holds is not granted twice or audited."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    manager_role = make_role("manager", "Manager")
    state.roles = [manager_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    held = make_membership_role(membership.id, manager_role.id)
    state.membership_roles = [held]
    state.granted_permissions = {"platform.admin"}
    # user; org, membership, role, the held grant; detail -> user
    state.lookup_queue = [actor, organisation, membership, manager_role, held, grace]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.post(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{membership.id}/roles",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
            json={"role_code": "manager"},
        )

    assert response.status_code == 200
    assert response.json()["roles"] == ["manager"]
    assert state.membership_roles == [held]
    assert state.audit_events == []


async def test_assign_role_unknown_membership_is_404() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, organisation, None]  # membership not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.post(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{uuid.uuid4()}/roles",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
            json={"role_code": "manager"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "membership_not_found"
    assert state.audit_events == []


# --- Role removal ---


async def test_remove_role_endpoint_revokes_and_audits() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    member_role = make_role("member", "Member")
    manager_role = make_role("manager", "Manager")
    state.roles = [member_role, manager_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    held = make_membership_role(membership.id, manager_role.id)
    state.membership_roles = [make_membership_role(membership.id, member_role.id), held]
    state.granted_permissions = {"platform.admin"}
    # user; org, membership, role, the held grant; detail -> user
    state.lookup_queue = [actor, organisation, membership, manager_role, held, grace]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/platform/organisations/{organisation.id}"
            f"/memberships/{membership.id}/roles/manager",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 200
    assert response.json()["roles"] == ["member"]
    # The grant row is gone and the removal was audited.
    assert len(state.membership_roles) == 1
    assert len(state.audit_events) == 1
    event = state.audit_events[0]
    assert event.action == ACTION_MEMBERSHIP_ROLE_CHANGED
    # record_event also stamps the request id into metadata; assert the
    # domain fields rather than the whole dict.
    assert event.event_metadata["role_code"] == "manager"
    assert event.event_metadata["action"] == "removed"
    assert event.resource_id == str(membership.id)


async def test_remove_role_not_held_is_an_idempotent_no_op() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    member_role = make_role("member", "Member")
    manager_role = make_role("manager", "Manager")
    state.roles = [member_role, manager_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    state.membership_roles = [make_membership_role(membership.id, member_role.id)]
    state.granted_permissions = {"platform.admin"}
    # user; org, membership, role, no held grant; detail -> user
    state.lookup_queue = [actor, organisation, membership, manager_role, None, grace]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/platform/organisations/{organisation.id}"
            f"/memberships/{membership.id}/roles/manager",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 200
    assert response.json()["roles"] == ["member"]
    assert len(state.membership_roles) == 1
    assert state.audit_events == []


# --- Suspend / reactivate ---


async def test_suspend_membership_endpoint_suspends_and_revokes_invitations() -> None:
    """Scope §6.6 / design plan §9 item 5: suspension revokes pending invites."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    member_role = make_role("member", "Member")
    state.roles = [member_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    state.membership_roles = [make_membership_role(membership.id, member_role.id)]
    pending = make_invitation(
        organisation.id,
        actor.id,
        email="grace@example.com",
        workos_invitation_id="inv_workos_pending",
    )
    state.invitations = [pending]
    # The platform admin's verified email differs from the member's, so the
    # login-time linking hook never consumes the staged invitation.
    state.profile = UserProfile(
        email="admin@example.com", name="Platform Admin", email_verified=True
    )
    state.granted_permissions = {"platform.admin"}
    # user; org, membership, member user; detail -> member user again
    state.lookup_queue = [actor, organisation, membership, grace, grace]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.patch(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{membership.id}/status",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
            json={"status": "suspended"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "suspended"
    assert membership.status == MembershipStatus.SUSPENDED
    # The pending invitation was revoked locally and at WorkOS.
    assert pending.status == InvitationStatus.REVOKED
    assert state.revoked_workos_invitations == ["inv_workos_pending"]
    # Both the status change and the revocation were audited.
    actions = [event.action for event in state.audit_events]
    assert ACTION_INVITATION_REVOKED in actions
    assert ACTION_MEMBERSHIP_SUSPENDED in actions
    suspended = [e for e in state.audit_events if e.action == ACTION_MEMBERSHIP_SUSPENDED]
    assert suspended[0].event_metadata["previous_status"] == "active"
    assert suspended[0].event_metadata["revoked_invitations"] == 1
    assert suspended[0].actor_user_id == actor.id
    assert suspended[0].resource_id == str(membership.id)


async def test_suspended_membership_is_rejected_by_org_routes() -> None:
    """Acceptance §5.7: a suspended membership fails the active-membership check.

    The platform admin suspends the member; the member's own session is then
    rejected by an org-scoped route with 403 ``not_a_member`` — the existing
    ``get_current_membership`` dependency enforces suspension with no new code.
    """
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    member_role = make_role("member", "Member")
    state.roles = [member_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    state.membership_roles = [make_membership_role(membership.id, member_role.id)]
    state.profile = UserProfile(
        email="admin@example.com", name="Platform Admin", email_verified=True
    )
    state.granted_permissions = {"platform.admin"}
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        token = make_token(private_key)
        # user; org, membership, member user; detail -> member user again
        state.lookup_queue = [actor, organisation, membership, grace, grace]
        suspend = await client.patch(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{membership.id}/status",
            headers={"Authorization": f"Bearer {token}"},
            json={"status": "suspended"},
        )
        assert suspend.status_code == 200

        # Request 2: the suspended member tries an org route.
        state.lookup_queue = [grace, membership]
        context = await client.get(
            "/_test/context",
            headers={
                "Authorization": f"Bearer {make_token(private_key, sub=grace.workos_user_id)}",
                "X-Org-Id": str(organisation.id),
            },
        )
        assert context.status_code == 403
        assert context.json()["code"] == "not_a_member"


async def test_reactivate_restores_org_access() -> None:
    """Acceptance §5.7: reactivation restores the active-membership check."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    member_role = make_role("member", "Member")
    state.roles = [member_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    state.membership_roles = [make_membership_role(membership.id, member_role.id)]
    state.profile = UserProfile(
        email="admin@example.com", name="Platform Admin", email_verified=True
    )
    state.granted_permissions = {"platform.admin"}
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        token = make_token(private_key)
        # user; org, membership, member user; detail -> member user again
        state.lookup_queue = [actor, organisation, membership, grace, grace]
        suspend = await client.patch(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{membership.id}/status",
            headers={"Authorization": f"Bearer {token}"},
            json={"status": "suspended"},
        )
        assert suspend.status_code == 200

        # Request 2: the platform admin reactivates the membership.
        state.lookup_queue = [actor, organisation, membership, grace]
        reactivate = await client.patch(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{membership.id}/status",
            headers={"Authorization": f"Bearer {token}"},
            json={"status": "active"},
        )
        assert reactivate.status_code == 200
        assert reactivate.json()["status"] == "active"
        assert membership.status == MembershipStatus.ACTIVE
        actions = [event.action for event in state.audit_events]
        assert ACTION_MEMBERSHIP_REACTIVATED in actions

        # Request 3: the member's org context works again.
        state.lookup_queue = [grace, membership]
        context = await client.get(
            "/_test/context",
            headers={
                "Authorization": f"Bearer {make_token(private_key, sub=grace.workos_user_id)}",
                "X-Org-Id": str(organisation.id),
            },
        )
        assert context.status_code == 200
        assert context.json()["organisation_id"] == str(organisation.id)


async def test_status_update_rejects_lifecycle_only_statuses() -> None:
    """Only active/suspended are platform-settable; invited/left are rejected."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.patch(
            f"/api/v1/platform/organisations/{uuid.uuid4()}/memberships/{uuid.uuid4()}/status",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
            json={"status": "left"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert state.audit_events == []


# --- Removal ---


async def test_remove_membership_endpoint_deletes_and_revokes_invitations() -> None:
    """Scope §6.6: removal deletes the row and revokes pending invitations."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    member_role = make_role("member", "Member")
    state.roles = [member_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    state.membership_roles = [make_membership_role(membership.id, member_role.id)]
    pending = make_invitation(
        organisation.id,
        actor.id,
        email="grace@example.com",
        workos_invitation_id="inv_workos_pending",
    )
    state.invitations = [pending]
    state.profile = UserProfile(
        email="admin@example.com", name="Platform Admin", email_verified=True
    )
    state.granted_permissions = {"platform.admin"}
    # user; org, membership; detail -> user; invitations answered by entity
    state.lookup_queue = [actor, organisation, membership, grace]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{membership.id}",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["user_email"] == "grace@example.com"
    assert body["roles"] == ["member"]
    # The membership row and its role grants are gone.
    assert membership not in state.memberships
    assert state.membership_roles == []
    # The pending invitation was revoked locally and at WorkOS.
    assert pending.status == InvitationStatus.REVOKED
    assert state.revoked_workos_invitations == ["inv_workos_pending"]
    actions = [event.action for event in state.audit_events]
    assert ACTION_INVITATION_REVOKED in actions
    assert ACTION_MEMBERSHIP_REMOVED in actions
    removed = [e for e in state.audit_events if e.action == ACTION_MEMBERSHIP_REMOVED]
    assert removed[0].event_metadata["user_id"] == str(grace.id)
    assert removed[0].event_metadata["revoked_invitations"] == 1
    assert removed[0].actor_user_id == actor.id


async def test_remove_membership_unknown_membership_is_404() -> None:
    state = ContextState(owner_role=make_role("owner", "Owner"))
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, organisation, None]  # membership not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/platform/organisations/{organisation.id}/memberships/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "membership_not_found"
    assert state.audit_events == []


# --- Service level: the steady state is cheap and safe ---


async def test_list_memberships_service_returns_details_and_total() -> None:
    """Service-level proof of the detail assembly without the HTTP layer."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    member_role = make_role("member", "Member")
    state.roles = [member_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    state.membership_roles = [make_membership_role(membership.id, member_role.id)]
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    state.lookup_queue = [organisation, 1, grace]

    details, total = await list_memberships(
        session, organisation_id=organisation.id, page=1, page_size=50
    )

    assert total == 1
    assert len(details) == 1
    assert details[0].user_email == "grace@example.com"
    assert details[0].roles == ["member"]


async def test_list_memberships_uses_a_bounded_number_of_queries() -> None:
    """A 100-row page must not turn user and role rendering into N+1 reads."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    role = make_role("member", "Member")
    state.roles = [role]
    for index in range(100):
        user = _make_member(
            state,
            workos_user_id=f"user_{index}",
            email=f"member_{index}@example.com",
        )
        membership = make_membership(user, organisation.id)
        state.memberships.append(membership)
        state.membership_roles.append(make_membership_role(membership.id, role.id))
    session = cast(AsyncSession, FakeSession(state))
    state.lookup_queue = [organisation, 100]

    details, total = await list_memberships(
        session, organisation_id=organisation.id, page=1, page_size=100
    )

    assert total == 100
    assert len(details) == 100
    assert state.scalar_calls == 2
    assert state.scalars_calls == 4


async def test_membership_mutations_are_idempotent_and_race_safe() -> None:
    """No-op paths never audit: steady-state administration is silent."""
    state = ContextState(owner_role=make_role("owner", "Owner"))
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    grace = _make_member(state, workos_user_id="user_grace", email="grace@example.com")
    member_role = make_role("member", "Member")
    state.roles = [member_role]
    membership = make_membership(grace, organisation.id)
    state.memberships = [membership]
    state.membership_roles = [make_membership_role(membership.id, member_role.id)]
    state.profile = UserProfile(
        email="admin@example.com", name="Platform Admin", email_verified=True
    )
    actor = _make_platform_admin_user(state)
    session: AsyncSession = cast(AsyncSession, FakeSession(state))

    # Assigning a held role, removing a non-held role and re-setting the same
    # status are all no-ops: no rows change and nothing is audited.
    state.lookup_queue = [
        organisation,
        membership,
        member_role,
        make_membership_role(membership.id, member_role.id),
        grace,
    ]
    detail = await assign_role(
        session,
        actor,
        organisation_id=organisation.id,
        membership_id=membership.id,
        role_code="member",
    )
    assert detail.roles == ["member"]

    state.lookup_queue = [organisation, membership, member_role, None, grace]
    detail = await remove_role(
        session,
        actor,
        organisation_id=organisation.id,
        membership_id=membership.id,
        role_code="member",
    )
    assert detail.roles == ["member"]

    state.lookup_queue = [organisation, membership, grace]
    detail = await set_membership_status(
        session,
        actor,
        organisation_id=organisation.id,
        membership_id=membership.id,
        status=MembershipStatus.ACTIVE,
        workos_invitations=FakeWorkOSInvitationsProvider(state),
    )
    assert detail.membership.status == MembershipStatus.ACTIVE

    # The no-op paths never touched the WorkOS provider or the audit log.
    assert state.workos_invitations == []
    assert state.revoked_workos_invitations == []
    assert state.audit_events == []
    assert len(state.membership_roles) == 1
