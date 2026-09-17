"""Behavioural tests for explicit platform-administrator lifecycle changes."""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.context_helpers import ContextState, FakeSession, make_platform_admin_role, make_user

from app.core.exceptions import BadRequestError
from app.modules.audit.service import (
    ACTION_PLATFORM_ADMIN_GRANTED,
    ACTION_PLATFORM_ADMIN_RECOVERY_GRANTED,
    ACTION_PLATFORM_ADMIN_REVOKED,
)
from app.modules.platform_admin.models import PlatformMembership
from app.modules.platform_admin.service import (
    grant_platform_admin,
    recover_platform_admin,
    revoke_platform_admin,
)


def _session(state: ContextState) -> AsyncSession:
    return cast(AsyncSession, FakeSession(state))


async def test_grant_platform_admin_is_idempotent_and_audited() -> None:
    state = ContextState()
    actor = make_user(workos_user_id="actor")
    target = make_user(workos_user_id="target")
    role = make_platform_admin_role()
    # role; target user; no existing membership; detail (user, role)
    state.lookup_queue = [role, target, None, target, role]

    detail = await grant_platform_admin(_session(state), actor=actor, user_id=target.id)

    assert detail.user_email == target.email
    assert len(state.platform_memberships) == 1
    assert state.audit_events[0].action == ACTION_PLATFORM_ADMIN_GRANTED
    assert state.audit_events[0].actor_user_id == actor.id
    assert state.audit_events[0].event_metadata["user_id"] == str(target.id)
    assert state.audit_events[0].event_metadata["role"] == role.code

    membership = state.platform_memberships[0]
    state.lookup_queue = [role, target, membership, target, role]
    await grant_platform_admin(_session(state), actor=actor, user_id=target.id)

    assert len(state.platform_memberships) == 1
    assert [event.action for event in state.audit_events] == [ACTION_PLATFORM_ADMIN_GRANTED]


async def test_grant_platform_admin_rejects_disabled_user() -> None:
    state = ContextState()
    actor = make_user(workos_user_id="actor")
    target = make_user(workos_user_id="disabled", is_active=False)
    role = make_platform_admin_role()
    state.lookup_queue = [role, target]

    with pytest.raises(BadRequestError, match="disabled") as exc_info:
        await grant_platform_admin(_session(state), actor=actor, user_id=target.id)

    assert exc_info.value.code == "user_disabled"
    assert state.platform_memberships == []
    assert state.audit_events == []


async def test_grant_platform_admin_recovers_from_unique_constraint_race() -> None:
    state = ContextState(fail_commits=1)
    actor = make_user(workos_user_id="actor")
    target = make_user(workos_user_id="target")
    role = make_platform_admin_role()
    winning_membership = PlatformMembership(user_id=target.id, platform_role_id=role.id)
    state.lookup_queue = [role, target, None, winning_membership, target, role]

    detail = await grant_platform_admin(_session(state), actor=actor, user_id=target.id)

    assert detail.membership is winning_membership
    assert state.platform_memberships == []
    assert state.audit_events == []


async def test_revoke_platform_admin_preserves_final_active_admin_and_audits() -> None:
    state = ContextState()
    actor = make_user(workos_user_id="actor")
    target = make_user(workos_user_id="target")
    role = make_platform_admin_role()
    membership = PlatformMembership(user_id=target.id, platform_role_id=role.id)
    membership.id = uuid.uuid4()
    state.platform_memberships = [membership]
    # role; membership; target user; active-admin count of 1 -> refusal.
    state.lookup_queue = [role, membership, target, 1]

    with pytest.raises(BadRequestError, match="At least one") as exc_info:
        await revoke_platform_admin(
            _session(state), actor=actor, platform_membership_id=membership.id
        )

    assert exc_info.value.code == "last_platform_admin"
    assert state.platform_memberships == [membership]
    assert state.audit_events == []

    retained = PlatformMembership(user_id=actor.id, platform_role_id=role.id)
    retained.id = uuid.uuid4()
    state.platform_memberships.append(retained)
    # role; membership; target; count 2; detail (user, role).
    state.lookup_queue = [role, membership, target, 2, target, role]
    detail = await revoke_platform_admin(
        _session(state), actor=actor, platform_membership_id=membership.id
    )

    assert detail.membership is membership
    assert state.platform_memberships == [retained]
    assert state.audit_events[0].action == ACTION_PLATFORM_ADMIN_REVOKED
    assert state.audit_events[0].event_metadata["user_id"] == str(target.id)


async def test_revoke_platform_admin_of_inactive_user_is_allowed() -> None:
    """An inactive member is not a recovery principal, so removing them is safe."""
    state = ContextState()
    actor = make_user(workos_user_id="actor")
    disabled = make_user(workos_user_id="disabled", is_active=False)
    role = make_platform_admin_role()
    membership = PlatformMembership(user_id=disabled.id, platform_role_id=role.id)
    membership.id = uuid.uuid4()
    state.platform_memberships = [membership]
    # role; membership; inactive target; count 1 (but target is not enabled).
    state.lookup_queue = [role, membership, disabled, 1, disabled, role]

    detail = await revoke_platform_admin(
        _session(state), actor=actor, platform_membership_id=membership.id
    )

    assert detail.membership is membership
    assert state.platform_memberships == []
    assert state.audit_events[0].action == ACTION_PLATFORM_ADMIN_REVOKED


async def test_recover_platform_admin_grants_only_when_locked_out() -> None:
    state = ContextState()
    target = make_user(workos_user_id="target")
    role = make_platform_admin_role()
    state.users[target.workos_user_id] = target
    # role; target user; active-admin count of 0 -> recovery grant proceeds.
    state.lookup_queue = [role, target, 0, None]

    recovered = await recover_platform_admin(
        _session(state), email=target.email, reason="operator recovered locked-out plane"
    )

    assert recovered is target
    assert len(state.platform_memberships) == 1
    event = state.audit_events[0]
    assert event.action == ACTION_PLATFORM_ADMIN_RECOVERY_GRANTED
    assert event.actor_user_id is None
    assert event.event_metadata["source"] == "break_glass"
    assert event.event_metadata["reason"] == "operator recovered locked-out plane"


async def test_recover_platform_admin_rejects_blank_reason() -> None:
    """A whitespace-only break-glass reason is refused before any DB access."""
    state = ContextState()

    with pytest.raises(BadRequestError) as exc_info:
        await recover_platform_admin(_session(state), email="ada@example.com", reason="   ")

    assert exc_info.value.code == "recovery_reason_required"
    assert state.platform_memberships == []
    assert state.audit_events == []
