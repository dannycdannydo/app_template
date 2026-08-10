"""Unit tests for WorkOS-backed user provisioning and profile refresh."""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession
from tests.context_helpers import ContextState, FakeProfileClient, FakeSession, make_user

from app.core.security import UserProfile, ValidatedSession
from app.modules.users.service import get_or_provision_user


async def test_existing_user_profile_is_refreshed_before_invitation_linking() -> None:
    state = ContextState()
    user = make_user(workos_user_id="user_changed")
    user.email = "old@example.com"
    user.name = "Old Name"
    state.profile = UserProfile(email="new@example.com", name="New Name", email_verified=True)
    state.lookup_queue = [user]

    result = await get_or_provision_user(
        cast(AsyncSession, FakeSession(state)),
        ValidatedSession(
            workos_user_id=user.workos_user_id,
            session_id=None,
            organisation_id=None,
            claims={},
        ),
        FakeProfileClient(state),
    )

    assert result is user
    assert (user.email, user.name) == ("new@example.com", "New Name")


async def test_unverified_profile_change_does_not_replace_existing_identity() -> None:
    state = ContextState()
    user = make_user(workos_user_id="user_unverified")
    user.email = "verified@example.com"
    state.profile = UserProfile(
        email="unverified@example.com", name="Unverified Name", email_verified=False
    )
    state.lookup_queue = [user]

    result = await get_or_provision_user(
        cast(AsyncSession, FakeSession(state)),
        ValidatedSession(
            workos_user_id=user.workos_user_id,
            session_id=None,
            organisation_id=None,
            claims={},
        ),
        FakeProfileClient(state),
    )

    assert result is user
    assert (user.email, user.name) == ("verified@example.com", "Ada Lovelace")
