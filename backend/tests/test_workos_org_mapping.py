"""Tests for the WorkOS organisation mapping (Scope §6.3, acceptance §5.4).

The request-flow tests run against the full ASGI stack with the in-memory
fakes from ``context_helpers.py``: the real WorkOS session validator backed by
a local RSA key, a fake database session, and a fake WorkOS organisations
provider that records created organisations by external id. The service-level
tests prove the lazy-backfill helper directly. The database-level invariants
(nullable + unique column, mapping round-trip) live in
``test_workos_org_mapping_db.py``.
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    ContextState,
    FakeSession,
    FakeWorkOSOrganizationsProvider,
    build_context_app_fixture,
    context_client,
    make_organisation,
    make_user,
)

from app.modules.audit.models import AuditEvent
from app.modules.platform_admin import service
from app.modules.platform_admin.service import ACTION_ORGANISATION_WORKOS_MAPPED


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


async def _create_platform_organisation(
    client: AsyncClient,
    *,
    token: str,
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
    return await client.post(
        "/api/v1/platform/organisations",
        json=payload or {"name": "Acme"},
        headers=request_headers,
    )


def _platform_admin(state: ContextState):
    """Stage an authenticated user holding the platform bundle."""
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    state.granted_permissions = {"platform.admin"}
    return user


async def test_platform_create_organisation_requires_token(context_app: ContextApp) -> None:
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.post("/api/v1/platform/organisations", json={"name": "Acme"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_platform_create_organisation_denied_without_platform_permission(
    context_app: ContextApp,
) -> None:
    """Acceptance §5.2: org authorisation never grants platform access."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    state.granted_permissions = {"organisation.manage", "users.manage_roles"}

    async with context_client(app) as client:
        response = await _create_platform_organisation(client, token=make_token(private_key))

    assert response.status_code == 403
    assert response.json()["code"] == "platform_admin_required"
    assert state.organisations == []  # nothing was created


async def test_platform_create_organisation_creates_org_with_mapping_and_audit(
    context_app: ContextApp,
) -> None:
    """Acceptance §5.4: internal org + WorkOS org + mapping in one transaction."""
    app, state, private_key = context_app
    actor = _platform_admin(state)

    async with context_client(app) as client:
        response = await _create_platform_organisation(client, token=make_token(private_key))

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Acme"
    assert body["workos_organisation_id"].startswith("org_workos_")

    (organisation,) = state.organisations
    assert organisation.id == uuid.UUID(body["id"])
    assert organisation.name == "Acme"
    assert organisation.workos_organisation_id == body["workos_organisation_id"]

    # The fake provider recorded the WorkOS organisation under the internal
    # id as its external id, so an orphan after a failed transaction can be
    # found again (documented reconciliation key).
    workos_organisation = state.workos_organisations[str(organisation.id)]
    assert workos_organisation.name == "Acme"
    assert workos_organisation.external_id == str(organisation.id)

    (event,) = state.audit_events
    assert event.action == "organisation.created"
    assert event.organisation_id == organisation.id
    assert event.actor_user_id == actor.id
    assert event.event_metadata["workos_organisation_id"] == body["workos_organisation_id"]


async def test_platform_create_organisation_mapping_never_client_writable(
    context_app: ContextApp,
) -> None:
    """Acceptance §5.4: the mapping field is rejected from request bodies."""
    app, state, private_key = context_app
    _platform_admin(state)

    payload: dict[str, object] = {
        "name": "Acme",
        "workos_organisation_id": "org_workos_smuggled",
    }
    async with context_client(app) as client:
        response = await _create_platform_organisation(
            client, token=make_token(private_key), payload=payload
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert state.organisations == []
    assert state.workos_organisations == {}  # no WorkOS call was made


async def test_platform_create_organisation_validates_name(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    _platform_admin(state)

    async with context_client(app) as client:
        response = await _create_platform_organisation(
            client, token=make_token(private_key), payload={"name": ""}
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert state.organisations == []


# --- Lazy backfill (design plan §3.1, acceptance §5.4) ---


async def test_ensure_workos_organisation_backfills_existing_organisation() -> None:
    """A pre-mapping organisation gains its WorkOS mapping at first need."""
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    provider = FakeWorkOSOrganizationsProvider(state)
    organisation = make_organisation(name="Legacy Ltd")
    state.organisations.append(organisation)
    actor = make_user()

    result = await service.ensure_workos_organisation(session, organisation, provider, actor=actor)
    await session.commit()

    assert result is organisation
    assert organisation.workos_organisation_id is not None
    assert state.workos_organisations[str(organisation.id)].name == "Legacy Ltd"
    (event,) = state.audit_events
    assert event.action == ACTION_ORGANISATION_WORKOS_MAPPED
    assert event.organisation_id == organisation.id
    assert event.actor_user_id == actor.id
    assert event.event_metadata["workos_organisation_id"] == organisation.workos_organisation_id


async def test_ensure_workos_organisation_is_a_noop_when_already_mapped() -> None:
    """An already-mapped organisation is never re-created in WorkOS."""
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    provider = FakeWorkOSOrganizationsProvider(state)
    organisation = make_organisation(
        name="Mapped Ltd", workos_organisation_id="org_workos_existing"
    )

    result = await service.ensure_workos_organisation(session, organisation, provider)

    assert result is organisation
    assert organisation.workos_organisation_id == "org_workos_existing"
    assert state.workos_organisations == {}  # no WorkOS create call
    assert state.audit_events == []  # no audit row either


async def test_ensure_workos_organisation_is_idempotent_across_calls() -> None:
    """Calling backfill twice produces exactly one WorkOS org and one audit row."""
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    provider = FakeWorkOSOrganizationsProvider(state)
    organisation = make_organisation(name="Twice Ltd")

    await service.ensure_workos_organisation(session, organisation, provider)
    await session.commit()
    mapping = organisation.workos_organisation_id
    await service.ensure_workos_organisation(session, organisation, provider)
    await session.commit()

    assert organisation.workos_organisation_id == mapping
    assert len(state.workos_organisations) == 1
    assert len(state.audit_events) == 1
    assert isinstance(state.audit_events[0], AuditEvent)
