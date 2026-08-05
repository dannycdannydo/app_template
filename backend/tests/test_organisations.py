"""Integration tests for organisation creation (Scope §6.3, acceptance §5.3).

Proves the creator's membership is created active and assigned the ``owner``
role in one transaction, and that identity fields smuggled into the request
body are never accepted (acceptance §5.4).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient, Response
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    build_context_app_fixture,
    context_client,
    make_user,
)

from app.modules.organisations.models import MembershipStatus


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


async def _create_organisation(
    client: AsyncClient,
    *,
    token: str,
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
    return await client.post(
        "/api/v1/organisations", json=payload or {"name": "Acme"}, headers=request_headers
    )


async def test_create_organisation_requires_token(context_app: ContextApp) -> None:
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.post("/api/v1/organisations", json={"name": "Acme"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_create_organisation_makes_creator_owner(context_app: ContextApp) -> None:
    """Acceptance §5.3: 201 with the creator's membership assigned the owner role."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user, state.owner_role]  # user lookup, then owner role

    async with context_client(app) as client:
        response = await _create_organisation(client, token=make_token(private_key))

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Acme"
    assert uuid.UUID(body["id"])

    (organisation,) = state.organisations
    (membership,) = state.memberships
    (membership_role,) = state.membership_roles
    owner_role = state.owner_role
    assert owner_role is not None
    assert organisation.id == uuid.UUID(body["id"])
    assert membership.user_id == user.id
    assert membership.organisation_id == organisation.id
    assert membership.status == MembershipStatus.ACTIVE
    assert membership_role.membership_id == membership.id
    assert membership_role.role_id == owner_role.id


async def test_create_organisation_requires_token_with_org_header(
    context_app: ContextApp,
) -> None:
    """X-Org-Id is not required for the bootstrap endpoint; the token still is."""
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/organisations",
            json={"name": "Acme"},
            headers={"X-Org-Id": str(uuid.uuid4())},
        )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_create_organisation_never_trusts_identity_fields(
    context_app: ContextApp,
) -> None:
    """Acceptance §5.4: identity fields in the body are rejected, not honoured."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user, state.owner_role]

    payload: dict[str, object] = {
        "name": "Acme",
        "user_id": "someone-else",
        "organisation_id": str(uuid.uuid4()),
    }
    async with context_client(app) as client:
        response = await _create_organisation(
            client, token=make_token(private_key), payload=payload
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert state.organisations == []  # nothing was created


async def test_create_organisation_validates_name(context_app: ContextApp) -> None:
    app, _state, private_key = context_app
    async with context_client(app) as client:
        response = await _create_organisation(
            client, token=make_token(private_key), payload={"name": ""}
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
