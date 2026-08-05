"""Shared in-memory fakes for request-context and organisation tests (Scope §6.3).

Mirrors the philosophy of ``tests/auth_helpers.py`` and the fakes in
``tests/test_auth.py``: the full ASGI stack runs with the real WorkOS session
validator backed by a local RSA key, while the database session and the
WorkOS profile client are replaced so the suite needs neither PostgreSQL nor
a network connection. ``lookup_queue`` drives ``scalar()`` in call order, so a
test queues exactly the rows the request flow reads.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from tests.auth_helpers import build_validator, generate_key_pair

from app.api.dependencies import get_current_membership, get_db
from app.core.security import (
    UserProfile,
    UserProfileClient,
    get_session_validator,
    get_user_profile_client,
)
from app.main import create_app
from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.permissions.models import MembershipRole, Role
from app.modules.records.models import Record
from app.modules.users.models import User


@dataclass
class ContextState:
    """In-memory stand-ins shared across requests of one test."""

    users: dict[str, User] = field(default_factory=dict[str, User])  # by workos_user_id
    lookup_queue: list[Any] = field(  # consumed by scalar() in call order
        default_factory=list[Any]
    )
    organisations: list[Organisation] = field(default_factory=list[Organisation])
    memberships: list[OrganisationMembership] = field(default_factory=list[OrganisationMembership])
    membership_roles: list[MembershipRole] = field(default_factory=list[MembershipRole])
    records: list[Record] = field(default_factory=list[Record])
    owner_role: Role | None = None
    granted_permissions: set[str] = field(  # consumed by scalars() (permission checks)
        default_factory=set[str]
    )


def make_owner_role() -> Role:
    """Build the seeded owner role row the organisation service looks up."""
    role = Role(code="owner", name="Owner")
    role.id = uuid.uuid4()
    return role


def make_user(*, workos_user_id: str = "user_test123", is_active: bool = True) -> User:
    user = User(workos_user_id=workos_user_id, email="ada@example.com", name="Ada Lovelace")
    user.id = uuid.uuid4()
    user.is_active = is_active
    user.created_at = datetime.now(UTC)
    return user


def make_membership(
    user: User,
    organisation_id: uuid.UUID,
    *,
    status: MembershipStatus = MembershipStatus.ACTIVE,
) -> OrganisationMembership:
    membership = OrganisationMembership(
        user_id=user.id,
        organisation_id=organisation_id,
        status=status,
    )
    membership.id = uuid.uuid4()
    membership.created_at = datetime.now(UTC)
    return membership


def make_record(
    organisation_id: uuid.UUID,
    *,
    title: str = "First record",
    body: str = "Record body",
) -> Record:
    record = Record(organisation_id=organisation_id, title=title, body=body)
    record.id = uuid.uuid4()
    record.created_at = datetime.now(UTC)
    record.updated_at = datetime.now(UTC)
    return record


class _ScalarsResult:
    """Stand-in for an async ``ScalarResult``: carries rows and exposes .all()."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Minimal session stand-in for the request-context and creation flows."""

    def __init__(self, state: ContextState) -> None:
        self._state = state
        self._added: list[Any] = []

    async def scalar(self, statement: object) -> Any:
        if self._state.lookup_queue:
            return self._state.lookup_queue.pop(0)
        return None

    async def scalars(self, statement: object) -> _ScalarsResult:
        # The permission check queries permission codes for a membership; the
        # fake answers from the granted set the test configures. The records
        # list query selects the Record entity; the fake answers from the
        # records the test staged. Anything else falls back to the granted set.
        descriptions = getattr(statement, "column_descriptions", None)
        if descriptions and descriptions[0].get("entity") is Record:
            return _ScalarsResult(list(self._state.records))
        return _ScalarsResult(sorted(self._state.granted_permissions))

    def add(self, instance: Any) -> None:
        self._added.append(instance)

    async def flush(self) -> None:
        now = datetime.now(UTC)
        for obj in self._added:
            if obj.id is None:
                obj.id = uuid.uuid4()
            if obj.created_at is None:
                obj.created_at = now
            if obj.updated_at is None:
                obj.updated_at = now

    async def commit(self) -> None:
        await self.flush()
        for obj in self._added:
            if isinstance(obj, Organisation):
                self._state.organisations.append(obj)
            elif isinstance(obj, OrganisationMembership):
                self._state.memberships.append(obj)
            elif isinstance(obj, MembershipRole):
                self._state.membership_roles.append(obj)
            elif isinstance(obj, Record):
                # Updates reuse the staged instance, so never append twice.
                if all(existing is not obj for existing in self._state.records):
                    self._state.records.append(obj)
            elif isinstance(obj, User):
                # Mirrors the model's ``is_active`` default applied at flush time;
                # provisioned users are always created active.
                obj.is_active = True
                self._state.users[obj.workos_user_id] = obj
        self._added.clear()

    async def delete(self, instance: Any) -> None:
        self._state.records = [record for record in self._state.records if record.id != instance.id]

    async def rollback(self) -> None:
        self._added.clear()

    async def refresh(self, instance: Any) -> None:
        # Attributes are already populated by flush(); nothing to re-read.
        return None


class FakeProfileClient(UserProfileClient):
    def __init__(self, state: ContextState) -> None:
        self._state = state

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        return UserProfile(email="ada@example.com", name="Ada Lovelace")


def build_context_app(
    *,
    private_key: rsa.RSAPrivateKey,
    state: ContextState,
) -> FastAPI:
    """Build the app with fakes, adding a probe route for the membership dependency."""
    app = create_app()

    def _probe(
        membership: Annotated[OrganisationMembership, Depends(get_current_membership)],
    ) -> dict[str, str]:
        return {"organisation_id": str(membership.organisation_id)}

    app.add_api_route("/_test/context", _probe, methods=["GET"])

    async def override_db() -> AsyncIterator[FakeSession]:
        yield FakeSession(state)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_validator] = lambda: build_validator(private_key)
    app.dependency_overrides[get_user_profile_client] = lambda: FakeProfileClient(state)
    return app


def context_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


ContextApp = tuple[FastAPI, ContextState, rsa.RSAPrivateKey]


def build_context_app_fixture() -> ContextApp:
    """Configure an app with an in-memory context state, ready for a request."""
    private_key, _ = generate_key_pair()
    state = ContextState(owner_role=make_owner_role())
    app = build_context_app(private_key=private_key, state=state)
    return app, state, private_key
