"""Integration tests for the organisation context dependency (Scope §6.3).

Exercises ``get_current_membership`` through the full ASGI stack against a
probe route, covering the standard context failures and the active-membership
acceptance path (Scope §6.3 checklist, acceptance §5.4).
"""

from __future__ import annotations

import uuid

import pytest
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    build_context_app_fixture,
    context_client,
    make_membership,
    make_user,
)

from app.modules.organisations.models import MembershipStatus


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_context_requires_bearer_token(context_app: ContextApp) -> None:
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.get("/_test/context")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_context_requires_org_header(context_app: ContextApp) -> None:
    """Acceptance §5.4: a missing X-Org-Id on a protected route is a 400."""
    app, _state, private_key = context_app
    async with context_client(app) as client:
        response = await client.get(
            "/_test/context", headers=_auth_headers(make_token(private_key))
        )
    assert response.status_code == 400
    assert response.json()["code"] == "org_context_required"


async def test_context_rejects_malformed_org_id(context_app: ContextApp) -> None:
    app, _state, private_key = context_app
    headers = {**_auth_headers(make_token(private_key)), "X-Org-Id": "not-a-uuid"}
    async with context_client(app) as client:
        response = await client.get("/_test/context", headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_org_id"


async def test_context_rejects_org_user_does_not_belong_to(context_app: ContextApp) -> None:
    """Acceptance §5.4: an org the user does not belong to is a 403."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user, None]  # user lookup, then no matching membership

    headers = {**_auth_headers(make_token(private_key)), "X-Org-Id": str(uuid.uuid4())}
    async with context_client(app) as client:
        response = await client.get("/_test/context", headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "not_a_member"


async def test_context_rejects_non_active_membership(context_app: ContextApp) -> None:
    """A suspended membership grants no organisation context."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    suspended = make_membership(user, org_id, status=MembershipStatus.SUSPENDED)
    state.lookup_queue = [user, suspended]

    headers = {**_auth_headers(make_token(private_key)), "X-Org-Id": str(org_id)}
    async with context_client(app) as client:
        response = await client.get("/_test/context", headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "not_a_member"


async def test_context_accepts_active_membership(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    state.lookup_queue = [user, make_membership(user, org_id)]

    headers = {**_auth_headers(make_token(private_key)), "X-Org-Id": str(org_id)}
    async with context_client(app) as client:
        response = await client.get("/_test/context", headers=headers)

    assert response.status_code == 200
    assert response.json()["organisation_id"] == str(org_id)


async def test_context_rejects_disabled_user(context_app: ContextApp) -> None:
    """A disabled user is blocked before the membership is resolved."""
    app, state, private_key = context_app
    disabled = make_user(is_active=False)
    state.users[disabled.workos_user_id] = disabled
    state.lookup_queue = [disabled]

    headers = {**_auth_headers(make_token(private_key)), "X-Org-Id": str(uuid.uuid4())}
    async with context_client(app) as client:
        response = await client.get("/_test/context", headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "user_disabled"
