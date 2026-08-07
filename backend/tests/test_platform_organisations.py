"""Tests for platform organisation administration (Scope §6.9).

Covers the three endpoints the admin centre needs beyond the existing create:
list every organisation (paginated), view one organisation, and rename one
organisation. These complete the organisation-administration surface that the
design plan (``PLATFORM_ADMIN_WORKFLOW_PLAN.md`` §2.1) specified for Scope
§6.3 but which was only partially wired then — create was shipped, list/view/
edit were not. The request-flow tests follow the membership-administration
pattern: full ASGI stack, in-memory fakes, real session validator backed by a
local RSA key. Persistence and the audit row are proven against a real
PostgreSQL in ``test_platform_organisations_db.py``.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    ContextState,
    build_context_app_fixture,
    context_client,
    make_organisation,
    make_user,
)

from app.modules.audit.service import ACTION_ORGANISATION_UPDATED


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


def _platform_admin(state: ContextState):
    """Stage an authenticated user holding the platform bundle."""
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    state.granted_permissions = {"platform.admin"}
    return user


def _auth_headers(client: AsyncClient, private_key: rsa.RSAPrivateKey) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(private_key)}"}


# --- Action-code metadata (Scope §6.9) ---


def test_organisation_updated_action_is_stable() -> None:
    """Blueprint §29: the audited action code is exact."""
    assert ACTION_ORGANISATION_UPDATED == "organisation.updated"


# --- Listing endpoint ---


async def test_list_platform_organisations_returns_paginated_rows(
    context_app: ContextApp,
) -> None:
    app, state, private_key = context_app
    actor = _platform_admin(state)
    first = make_organisation(name="Acme Ltd")
    second = make_organisation(name="Globex Inc")
    state.organisations = [first, second]
    # get_current_user -> actor; count -> 2; the rows answer from state.
    state.lookup_queue = [actor, 2]

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/platform/organisations", headers=_auth_headers(client, private_key)
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 50
    names = {item["name"] for item in body["items"]}
    assert names == {"Acme Ltd", "Globex Inc"}


async def test_list_platform_organisations_requires_platform_admin(
    context_app: ContextApp,
) -> None:
    """Scope §6.2: an org owner without platform membership is denied."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    state.granted_permissions = {"organisation.manage", "users.manage_roles", "records.read"}

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/platform/organisations", headers=_auth_headers(client, private_key)
        )

    assert response.status_code == 403
    assert response.json()["code"] == "platform_admin_required"


async def test_list_platform_organisations_requires_token(context_app: ContextApp) -> None:
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.get("/api/v1/platform/organisations")
    assert response.status_code == 401


# --- Detail endpoint ---


async def test_get_platform_organisation_returns_mapping(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    actor = _platform_admin(state)
    organisation = make_organisation(name="Acme Ltd", workos_organisation_id="org_workos_acme")
    state.lookup_queue = [actor, organisation]

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/platform/organisations/{organisation.id}",
            headers=_auth_headers(client, private_key),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(organisation.id)
    assert body["name"] == "Acme Ltd"
    assert body["workos_organisation_id"] == "org_workos_acme"


async def test_get_platform_organisation_unknown_is_404(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    actor = _platform_admin(state)
    state.lookup_queue = [actor, None]

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/platform/organisations/{uuid.uuid4()}",
            headers=_auth_headers(client, private_key),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "organisation_not_found"


# --- Update endpoint ---


async def test_update_platform_organisation_renames_and_audits(
    context_app: ContextApp,
) -> None:
    app, state, private_key = context_app
    actor = _platform_admin(state)
    organisation = make_organisation(name="Acme Ltd")
    state.lookup_queue = [actor, organisation]

    async with context_client(app) as client:
        response = await client.patch(
            f"/api/v1/platform/organisations/{organisation.id}",
            json={"name": "Acme International"},
            headers=_auth_headers(client, private_key),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Acme International"
    assert organisation.name == "Acme International"

    # The rename is audited (blueprint §29, acceptance §5.1).
    written = [event for event in state.audit_events if event.action == ACTION_ORGANISATION_UPDATED]
    assert len(written) == 1
    assert written[0].resource_type == "organisation"
    assert written[0].resource_id == str(organisation.id)
    assert written[0].organisation_id == organisation.id
    assert written[0].event_metadata["name"] == "Acme International"


async def test_update_platform_organisation_unknown_is_404(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    actor = _platform_admin(state)
    state.lookup_queue = [actor, None]

    async with context_client(app) as client:
        response = await client.patch(
            f"/api/v1/platform/organisations/{uuid.uuid4()}",
            json={"name": "Renamed"},
            headers=_auth_headers(client, private_key),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "organisation_not_found"


async def test_update_platform_organisation_rejects_smuggled_fields(
    context_app: ContextApp,
) -> None:
    """Server-controlled fields are never client-writable (acceptance §5.4)."""
    app, state, private_key = context_app
    _platform_admin(state)
    organisation = make_organisation(name="Acme Ltd")

    async with context_client(app) as client:
        response = await client.patch(
            f"/api/v1/platform/organisations/{organisation.id}",
            json={
                "name": "Acme Ltd",
                "workos_organisation_id": "org_workos_smuggled",
                "id": str(uuid.uuid4()),
            },
            headers=_auth_headers(client, private_key),
        )

    assert response.status_code == 422


async def test_update_platform_organisation_rejects_empty_name(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    _platform_admin(state)
    organisation = make_organisation(name="Acme Ltd")

    async with context_client(app) as client:
        response = await client.patch(
            f"/api/v1/platform/organisations/{organisation.id}",
            json={"name": ""},
            headers=_auth_headers(client, private_key),
        )

    assert response.status_code == 422
