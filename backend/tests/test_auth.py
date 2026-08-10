"""Integration tests for the auth dependency and /me endpoint (v0.2 Scope §6.2).

Exercises the full ASGI stack with the real WorkOS session validator backed
by a local signing key. The database session and the WorkOS profile client
are replaced with in-memory fakes so the suite runs without PostgreSQL or a
network connection (the same philosophy as ``test_health.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.exc import IntegrityError
from tests.auth_helpers import build_validator, generate_key_pair, make_token

from app.api.dependencies import get_db
from app.core.exceptions import ExternalServiceError
from app.core.security import (
    UserProfile,
    UserProfileClient,
    get_session_validator,
    get_user_profile_client,
)
from app.main import create_app
from app.modules.invitations.models import Invitation
from app.modules.organisations.models import MembershipStatus, OrganisationMembership
from app.modules.users.models import User

WORKOS_USER_ID = "user_test123"


@dataclass
class AuthState:
    """In-memory stand-ins shared across requests of one test."""

    users: dict[str, User] = field(default_factory=dict[str, User])  # by workos_user_id
    lookup_queue: list[User | None] = field(  # consumed by scalar()
        default_factory=list[User | None]
    )
    memberships: list[OrganisationMembership] = field(default_factory=list[OrganisationMembership])
    scalars_queue: list[list[Any]] = field(  # consumed by scalars() in call order
        default_factory=list[list[Any]]
    )
    fail_commits: int = 0
    profile_calls: list[str] = field(default_factory=list[str])


class _ScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Minimal session stand-in implementing the surface the auth flow uses."""

    def __init__(self, state: AuthState) -> None:
        self._state = state
        self._added: list[User] = []

    async def scalar(self, statement: object) -> User | None:
        if self._state.lookup_queue:
            return self._state.lookup_queue.pop(0)
        return None

    async def scalars(self, statement: object) -> _ScalarsResult:
        # The login-time invitation query (Scope §6.5) runs inside
        # get_current_user, before the /me payload queries that consume the
        # queue; with no staged invitations it must answer empty rather than
        # consume a queued payload row.
        descriptions = getattr(statement, "column_descriptions", None)
        entity = descriptions[0].get("entity") if descriptions else None
        if entity is Invitation:
            return _ScalarsResult([])
        if self._state.scalars_queue:
            return _ScalarsResult(self._state.scalars_queue.pop(0))
        return _ScalarsResult(self._state.memberships)

    def add(self, instance: User) -> None:
        self._added.append(instance)

    async def commit(self) -> None:
        if self._state.fail_commits:
            self._state.fail_commits -= 1
            raise IntegrityError("insert", {}, Exception("duplicate key value"))
        now = datetime.now(UTC)
        for user in self._added:
            user.id = user.id or uuid.uuid4()
            user.created_at = user.created_at or now
            # Mirrors the model's ``is_active`` default applied at flush time;
            # provisioned users are always created active.
            user.is_active = True
            self._state.users[user.workos_user_id] = user

    async def rollback(self) -> None:
        self._added.clear()


class FakeProfileClient(UserProfileClient):
    def __init__(self, state: AuthState) -> None:
        self._state = state

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        self._state.profile_calls.append(workos_user_id)
        return UserProfile(email="ada@example.com", name="Ada Lovelace", email_verified=True)


class FailingProfileClient(UserProfileClient):
    async def get_profile(self, workos_user_id: str) -> UserProfile:
        raise ExternalServiceError(
            code="workos_profile_unavailable",
            message="Authentication could not be completed. Please try again.",
        )


def _make_user(*, is_active: bool = True, workos_user_id: str = WORKOS_USER_ID) -> User:
    user = User(workos_user_id=workos_user_id, email="ada@example.com", name="Ada Lovelace")
    user.id = uuid.uuid4()
    user.is_active = is_active
    user.created_at = datetime.now(UTC)
    return user


def _make_membership(user: User) -> OrganisationMembership:
    membership = OrganisationMembership(
        user_id=user.id,
        organisation_id=uuid.uuid4(),
        status=MembershipStatus.ACTIVE,
    )
    membership.id = uuid.uuid4()
    membership.created_at = datetime.now(UTC)
    return membership


def _build_app(
    *, private_key: rsa.RSAPrivateKey, state: AuthState, profiles: UserProfileClient | None = None
) -> FastAPI:
    """Build the app with fakes for the session, validator and profile client."""
    app = create_app()

    async def override_db() -> AsyncIterator[FakeSession]:
        yield FakeSession(state)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_validator] = lambda: build_validator(private_key)
    app.dependency_overrides[get_user_profile_client] = lambda: profiles or FakeProfileClient(state)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def _get_me(client: AsyncClient, token: str | None) -> Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.get("/api/v1/me", headers=headers)


AuthApp = tuple[FastAPI, AuthState, rsa.RSAPrivateKey]


@pytest.fixture
def auth_app() -> AuthApp:
    """A configured app with an in-memory auth state, ready for a request."""
    private_key, _ = generate_key_pair()
    state = AuthState()
    app = _build_app(private_key=private_key, state=state)
    return app, state, private_key


async def test_me_requires_bearer_token(auth_app: AuthApp) -> None:
    app, _state, _private_key = auth_app
    async with _client(app) as client:
        response = await _get_me(client, None)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_me_rejects_garbage_token(auth_app: AuthApp) -> None:
    app, _state, _private_key = auth_app
    async with _client(app) as client:
        response = await _get_me(client, "not-a-jwt")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_session"


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"issuer": "https://evil.example.com/"},
        {"client_id": "client_other"},
        {"aud": "client_other"},
        {"seconds_valid": -3600},
    ],
)
async def test_me_rejects_invalid_sessions(auth_app: AuthApp, token_kwargs: dict[str, Any]) -> None:
    """Acceptance §5.1: wrong issuer, audience or expiry are rejected on a protected endpoint."""
    app, _state, private_key = auth_app
    async with _client(app) as client:
        response = await _get_me(client, make_token(private_key, **token_kwargs))
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_session"


async def test_me_rejects_tampered_signature(auth_app: AuthApp) -> None:
    app, _state, _private_key = auth_app
    other_key, _ = generate_key_pair()
    async with _client(app) as client:
        response = await _get_me(client, make_token(other_key))
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_session"


async def test_me_provisions_user_on_first_login(auth_app: AuthApp) -> None:
    app, state, private_key = auth_app
    async with _client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "ada@example.com"
    assert body["user"]["name"] == "Ada Lovelace"
    assert body["user"]["is_active"] is True
    assert state.users[WORKOS_USER_ID].workos_user_id == WORKOS_USER_ID
    assert state.profile_calls == [WORKOS_USER_ID, WORKOS_USER_ID]


async def test_me_reuses_existing_user_on_second_login(auth_app: AuthApp) -> None:
    app, state, private_key = auth_app
    existing = _make_user()
    state.users[WORKOS_USER_ID] = existing
    state.lookup_queue = [existing]

    async with _client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(existing.id)
    # The existing local identity is refreshed from WorkOS before invitation
    # reconciliation uses the verified provider email, so an email change can
    # link a pending invitation without waiting for webhook delivery.
    assert state.profile_calls == [WORKOS_USER_ID, WORKOS_USER_ID]


async def test_me_never_trusts_identity_fields_from_token(auth_app: AuthApp) -> None:
    """Acceptance §5.4: identity comes from the validated profile, not the token."""
    app, _state, private_key = auth_app
    token = make_token(private_key, extra={"email": "evil@example.com", "name": "Evil"})

    async with _client(app) as client:
        response = await _get_me(client, token)

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "ada@example.com"
    assert response.json()["user"]["name"] == "Ada Lovelace"


async def test_me_rejects_disabled_user(auth_app: AuthApp) -> None:
    """Acceptance §5.6: a disabled user is blocked even with a valid session."""
    app, state, private_key = auth_app
    disabled = _make_user(is_active=False)
    state.users[WORKOS_USER_ID] = disabled
    state.lookup_queue = [disabled]

    async with _client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 403
    assert response.json()["code"] == "user_disabled"


async def test_me_returns_memberships_and_roles(auth_app: AuthApp) -> None:
    app, state, private_key = auth_app
    user = _make_user()
    state.users[WORKOS_USER_ID] = user
    state.lookup_queue = [user]
    membership = _make_membership(user)
    state.memberships = [membership]
    # memberships, then org role codes, then platform role codes
    state.scalars_queue = [[membership], ["owner"], []]

    async with _client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    body = response.json()
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["status"] == "active"
    assert body["roles"] == ["owner"]
    assert body["platform_roles"] == []


async def test_me_returns_platform_roles_for_platform_admin(auth_app: AuthApp) -> None:
    """Scope §6.2: /me surfaces platform roles so the UI can gate the admin centre."""
    app, state, private_key = auth_app
    user = _make_user()
    state.users[WORKOS_USER_ID] = user
    state.lookup_queue = [user]
    # memberships, then org role codes, then platform role codes
    state.scalars_queue = [[], [], ["platform_admin"]]

    async with _client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    body = response.json()
    assert body["memberships"] == []
    assert body["roles"] == []
    assert body["platform_roles"] == ["platform_admin"]


async def test_me_handles_concurrent_provisioning_race(auth_app: AuthApp) -> None:
    """A unique-constraint loss on first login re-reads the winning row."""
    app, state, private_key = auth_app
    winner = _make_user()
    state.users[WORKOS_USER_ID] = winner
    state.lookup_queue = [None, winner]  # first lookup misses, second finds the winner
    state.fail_commits = 1

    async with _client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(winner.id)
    assert state.users[WORKOS_USER_ID].id == winner.id
    assert state.profile_calls == [WORKOS_USER_ID, WORKOS_USER_ID]


async def test_me_surfaces_profile_provider_failure_as_safe_upstream_error() -> None:
    private_key, _ = generate_key_pair()
    app = _build_app(
        private_key=private_key,
        state=AuthState(),
        profiles=FailingProfileClient(),
    )

    async with _client(app) as client:
        response = await _get_me(client, make_token(private_key))

    assert response.status_code == 502
    assert response.json()["code"] == "workos_profile_unavailable"
    assert response.json()["message"] == "Authentication could not be completed. Please try again."
