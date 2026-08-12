"""Tests for the one-time platform bootstrap (Scope §6.4).

Two layers, mirroring the audit module's split:

- request-flow tests here drive the full ASGI stack through ``/me`` with the
  in-memory fakes from ``context_helpers.py``, proving the grant fires inside
  the ``get_current_user`` provisioning chain and surfaces in ``platform_roles``;
- the real-database proofs (the migration applies, the sentinel constraint
  rejects a second grant, concurrent first logins grant once) live in
  ``test_bootstrap_db.py``.

The configured bootstrap email comes from ``BOOTSTRAP_PLATFORM_ADMIN_EMAIL``
via ``get_settings``; tests monkeypatch the settings accessor on the service
module rather than the process-wide cached settings singleton.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession
from tests.auth_helpers import generate_key_pair, make_token
from tests.context_helpers import (
    ContextState,
    FakeProfileClient,
    FakeSession,
    build_context_app,
    context_client,
    make_membership,
    make_organisation,
    make_owner_role,
    make_platform_admin_role,
    make_user,
)

from app.core.security import UserProfile
from app.db.base import Base
from app.modules.audit.service import ACTION_ORGANISATION_CREATED, ACTION_PLATFORM_BOOTSTRAP_GRANTED
from app.modules.organisations.models import MembershipStatus
from app.modules.permissions.constants import PLATFORM_ADMIN_ROLE_CODE
from app.modules.platform_admin import service
from app.modules.platform_admin.models import (
    BOOTSTRAP_SINGLETON_ID,
    BootstrapState,
    PlatformMembership,
)

BOOTSTRAP_EMAIL = "admin@example.com"


def _configured_email(
    monkeypatch: pytest.MonkeyPatch,
    email: str,
    org: str = "",
) -> None:
    """Point the service's settings accessor at a bootstrap email (and org)."""
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(
            bootstrap_platform_admin_email=email,
            bootstrap_platform_admin_org=org,
        ),
    )


def _bootstrap_state() -> BootstrapState:
    state = BootstrapState(email=BOOTSTRAP_EMAIL, consumed_by_user_id=uuid.uuid4())
    state.id = BOOTSTRAP_SINGLETON_ID
    return state


def _membership(user_id: uuid.UUID, role_id: uuid.UUID) -> PlatformMembership:
    membership = PlatformMembership(user_id=user_id, platform_role_id=role_id)
    membership.id = uuid.uuid4()
    return membership


# --- Model metadata (Scope §6.4) ---


def test_bootstrap_state_table_registered_on_base_metadata() -> None:
    assert "bootstrap_states" in Base.metadata.tables


def test_bootstrap_state_is_a_single_row_table_by_construction() -> None:
    """The fixed sentinel id with a check constraint makes double-grants impossible."""
    table = cast(Table, BootstrapState.__table__)
    assert table.c.id.primary_key
    check_names = {constraint.name for constraint in table.constraints}
    assert "ck_bootstrap_states_single_row" in check_names
    assert "updated_at" not in table.c  # consumed once, never modified


def test_bootstrap_state_columns_follow_scope_shape() -> None:
    table = cast(Table, BootstrapState.__table__)
    assert not table.c.email.nullable
    assert not table.c.consumed_by_user_id.nullable
    assert not table.c.consumed_at.nullable
    assert table.c.consumed_by_user_id.index is True


# --- Grant hook, exercised through the full /me request flow ---


@pytest.fixture
def bootstrap_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the bootstrap for every test in this module."""
    _configured_email(monkeypatch, BOOTSTRAP_EMAIL)


async def _get_me(client: AsyncClient, token: str) -> Response:
    return await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})


async def test_first_login_of_configured_verified_email_grants_platform_admin(
    bootstrap_settings: None,
) -> None:
    """Acceptance §5.5: the exact configured email's first login grants once."""
    state = ContextState()
    state.profile = UserProfile(email=BOOTSTRAP_EMAIL, name="Ada Lovelace", email_verified=True)
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    # user lookup miss -> provision; bootstrap lookup miss; platform role found
    state.lookup_queue = [None, None, make_platform_admin_role()]
    state.scalars_queue = [[], [], ["platform_admin"]]  # memberships, roles, platform roles

    async with context_client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    assert response.json()["user"]["email"] == BOOTSTRAP_EMAIL
    assert response.json()["platform_roles"] == ["platform_admin"]

    assert len(state.platform_memberships) == 1
    assert len(state.bootstrap_states) == 1
    assert state.bootstrap_states[0].email == BOOTSTRAP_EMAIL
    assert len(state.audit_events) == 1
    assert state.audit_events[0].action == ACTION_PLATFORM_BOOTSTRAP_GRANTED
    assert state.audit_events[0].resource_type == "user"
    assert state.audit_events[0].actor_user_id == state.platform_memberships[0].user_id


async def test_repeat_login_is_a_no_op(
    bootstrap_settings: None,
) -> None:
    """Acceptance §5.5: a second login grants nothing new."""
    state = ContextState()
    user = make_user()
    state.users[user.workos_user_id] = user
    already_consumed = _bootstrap_state()
    already_consumed.consumed_by_user_id = user.id
    state.bootstrap_states = [already_consumed]
    existing_membership = _membership(user.id, uuid.uuid4())
    state.platform_memberships = [existing_membership]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    # user found; bootstrap lookup returns the consumed record
    state.lookup_queue = [user, already_consumed]
    state.scalars_queue = [[], [], ["platform_admin"]]

    async with context_client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    assert response.json()["platform_roles"] == ["platform_admin"]
    assert state.platform_memberships == [existing_membership]
    assert state.bootstrap_states == [already_consumed]
    assert state.audit_events == []


async def test_wrong_email_never_grants(bootstrap_settings: None) -> None:
    """Acceptance §5.5: a different WorkOS email never triggers the grant."""
    state = ContextState()
    state.profile = UserProfile(email="other@example.com", name="Other", email_verified=True)
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    # user miss -> provision; bootstrap miss; the email mismatch short-circuits
    state.lookup_queue = [None, None]
    state.scalars_queue = [[], [], []]

    async with context_client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    assert response.json()["platform_roles"] == []
    assert state.platform_memberships == []
    assert state.bootstrap_states == []
    assert state.audit_events == []


async def test_unverified_email_never_grants(bootstrap_settings: None) -> None:
    """Acceptance §5.5: email_verified is required for the privileged grant."""
    state = ContextState()
    state.profile = UserProfile(email=BOOTSTRAP_EMAIL, name="Ada", email_verified=False)
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    state.lookup_queue = [None, None]
    state.scalars_queue = [[], [], []]

    async with context_client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    assert response.json()["platform_roles"] == []
    assert state.platform_memberships == []
    assert state.bootstrap_states == []
    assert state.audit_events == []


async def test_configured_email_matches_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook compares case-insensitively against the (validator-normalised) value."""
    _configured_email(monkeypatch, "admin@example.com")  # the settings validator lower-cases
    state = ContextState()
    # The WorkOS profile reports the email in a different case; the match holds.
    state.profile = UserProfile(email="ADMIN@Example.COM", name="Ada", email_verified=True)
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    state.lookup_queue = [None, None, make_platform_admin_role()]
    state.scalars_queue = [[], [], ["platform_admin"]]

    async with context_client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    assert response.json()["platform_roles"] == ["platform_admin"]
    assert len(state.bootstrap_states) == 1


async def test_unconfigured_bootstrap_is_a_no_op() -> None:
    """Without BOOTSTRAP_PLATFORM_ADMIN_EMAIL nothing is ever granted."""
    state = ContextState()
    state.profile = UserProfile(email=BOOTSTRAP_EMAIL, name="Ada", email_verified=True)
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    # user miss -> provision; the hook returns before any further queries
    state.lookup_queue = [None]
    state.scalars_queue = [[], [], []]

    async with context_client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    assert response.json()["platform_roles"] == []
    assert state.platform_memberships == []
    assert state.bootstrap_states == []
    assert state.audit_events == []


async def test_disabled_user_is_rejected_before_any_grant(bootstrap_settings: None) -> None:
    """A disabled user is blocked 403 and the hook never runs."""
    state = ContextState()
    disabled = make_user(is_active=False)
    state.users[disabled.workos_user_id] = disabled
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    state.lookup_queue = [disabled]

    async with context_client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 403
    assert response.json()["code"] == "user_disabled"
    assert state.platform_memberships == []
    assert state.bootstrap_states == []
    assert state.audit_events == []


# --- Grant hook edge cases, exercised at the service level ---


async def test_lost_race_recovers_without_double_grant(bootstrap_settings: None) -> None:
    """A concurrent first login that lost the race never double-grants."""
    state = ContextState()
    state.profile = UserProfile(email=BOOTSTRAP_EMAIL, name="Ada", email_verified=True)
    winner_state = _bootstrap_state()
    state.bootstrap_states = [winner_state]  # what the concurrent winner persisted
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    user = make_user(workos_user_id="user_bootstrap_race")
    # bootstrap miss; platform role found; post-rollback re-read sees the winner
    state.lookup_queue = [None, make_platform_admin_role(), winner_state]
    state.fail_commits = 1  # the losing insert violates the sentinel constraint

    membership = await service.maybe_grant_bootstrap_platform_admin(
        session, user, FakeProfileClient(state)
    )

    assert membership is None
    assert state.bootstrap_states == [winner_state]  # no second record
    assert state.platform_memberships == []  # the losing membership was rolled back
    assert state.audit_events == []  # the losing audit row was rolled back


async def test_missing_platform_role_surfaces_as_503(
    bootstrap_settings: None,
) -> None:
    """A broken deployment (no seeded role) fails loudly instead of no-opping."""
    state = ContextState()
    state.profile = UserProfile(email=BOOTSTRAP_EMAIL, name="Ada", email_verified=True)
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    user = make_user(workos_user_id="user_bootstrap_role_missing")
    state.lookup_queue = [None, None]  # bootstrap miss; role lookup returns None

    with pytest.raises(service.ServiceUnavailableError) as excinfo:
        await service.maybe_grant_bootstrap_platform_admin(session, user, FakeProfileClient(state))

    assert excinfo.value.code == "platform_bootstrap_failed"
    assert state.platform_memberships == []
    assert state.bootstrap_states == []


# --- Bootstrap organisation provisioning (BOOTSTRAP_PLATFORM_ADMIN_ORG) ---


async def test_bootstrap_grant_also_creates_configured_organisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grant provisions the admin's tenant: org + owner membership atomically."""
    _configured_email(monkeypatch, BOOTSTRAP_EMAIL, org="Trakr")
    state = ContextState()
    state.profile = UserProfile(email=BOOTSTRAP_EMAIL, name="Ada Lovelace", email_verified=True)
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    # user miss -> provision; bootstrap miss; platform role; org miss; owner role
    state.lookup_queue = [
        None,
        None,
        make_platform_admin_role(),
        None,
        make_owner_role(),
    ]
    state.scalars_queue = [[], [], ["platform_admin"]]

    async with context_client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    assert response.json()["platform_roles"] == ["platform_admin"]
    assert len(state.organisations) == 1
    assert state.organisations[0].name == "Trakr"
    assert len(state.memberships) == 1
    assert state.memberships[0].organisation_id == state.organisations[0].id
    assert state.memberships[0].status == MembershipStatus.ACTIVE
    provisioned_user = next(iter(state.users.values()))
    assert state.memberships[0].user_id == provisioned_user.id
    assert len(state.membership_roles) == 1
    assert state.membership_roles[0].membership_id == state.memberships[0].id
    actions = [event.action for event in state.audit_events]
    assert ACTION_ORGANISATION_CREATED in actions
    assert ACTION_PLATFORM_BOOTSTRAP_GRANTED in actions


async def test_bootstrap_grant_reuses_an_existing_organisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An organisation with the configured name is reused, never duplicated."""
    _configured_email(monkeypatch, BOOTSTRAP_EMAIL, org="Trakr")
    state = ContextState()
    state.profile = UserProfile(email=BOOTSTRAP_EMAIL, name="Ada", email_verified=True)
    org = make_organisation(name="Trakr")
    state.organisations = [org]
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    user = make_user(workos_user_id="user_bootstrap_existing_org")
    # bootstrap miss; platform role; org found; membership miss; owner role
    state.lookup_queue = [
        None,
        make_platform_admin_role(),
        org,
        None,
        make_owner_role(),
    ]

    membership = await service.maybe_grant_bootstrap_platform_admin(
        session, user, FakeProfileClient(state)
    )

    assert membership is not None
    assert state.organisations == [org]  # reused, not duplicated
    assert len(state.memberships) == 1
    assert state.memberships[0].organisation_id == org.id
    assert len(state.membership_roles) == 1
    actions = [event.action for event in state.audit_events]
    assert ACTION_ORGANISATION_CREATED not in actions
    assert ACTION_PLATFORM_BOOTSTRAP_GRANTED in actions


async def test_bootstrap_grant_skips_an_existing_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user already in the organisation is left untouched (idempotent)."""
    _configured_email(monkeypatch, BOOTSTRAP_EMAIL, org="Trakr")
    state = ContextState()
    state.profile = UserProfile(email=BOOTSTRAP_EMAIL, name="Ada", email_verified=True)
    user = make_user(workos_user_id="user_bootstrap_member")
    org = make_organisation(name="Trakr")
    state.organisations = [org]
    existing = make_membership(user, org.id)
    state.memberships = [existing]
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    # bootstrap miss; platform role; org found; membership found -> no-op
    state.lookup_queue = [None, make_platform_admin_role(), org, existing]

    membership = await service.maybe_grant_bootstrap_platform_admin(
        session, user, FakeProfileClient(state)
    )

    assert membership is not None
    assert state.memberships == [existing]
    assert state.membership_roles == []
    actions = [event.action for event in state.audit_events]
    assert ACTION_ORGANISATION_CREATED not in actions
    assert ACTION_PLATFORM_BOOTSTRAP_GRANTED in actions


async def test_bootstrap_grant_without_owner_role_surfaces_as_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing owner-role seed fails loudly, before any organisation row."""
    _configured_email(monkeypatch, BOOTSTRAP_EMAIL, org="Trakr")
    state = ContextState()
    state.profile = UserProfile(email=BOOTSTRAP_EMAIL, name="Ada", email_verified=True)
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    user = make_user(workos_user_id="user_bootstrap_owner_missing")
    # bootstrap miss; platform role; org miss; owner role miss
    state.lookup_queue = [None, make_platform_admin_role(), None, None]

    with pytest.raises(service.ServiceUnavailableError) as excinfo:
        await service.maybe_grant_bootstrap_platform_admin(session, user, FakeProfileClient(state))

    assert excinfo.value.code == "platform_bootstrap_failed"
    assert state.organisations == []
    assert state.memberships == []


def test_bootstrap_granted_action_is_stable() -> None:
    """Acceptance §5.5: the audited action is exactly platform.bootstrap_granted."""
    assert ACTION_PLATFORM_BOOTSTRAP_GRANTED == "platform.bootstrap_granted"
    assert PLATFORM_ADMIN_ROLE_CODE == "platform_admin"
