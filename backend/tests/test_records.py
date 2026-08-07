"""Integration tests for the tenant-scoped records module (v0.2 Scope §6.5).

The full ASGI stack runs with the fakes from ``context_helpers.py`` so the
suite needs neither PostgreSQL nor a network connection; the real-database
scoping proof lives in ``test_records_db.py``. These tests exercise the
request flow: permission gating per route, organisation context derivation,
the pagination envelope, and the 404 contract for missing records.
"""

from __future__ import annotations

import uuid
from typing import cast as typing_cast

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    FakeSession,
    build_context_app_fixture,
    context_client,
    make_membership,
    make_organisation_feature,
    make_record,
    make_user,
)

from app.core.exceptions import NotFoundError
from app.db.base import Base
from app.modules.records import service
from app.modules.records.models import Record


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


# --- Model metadata (BP §7, §10) ---


def test_records_table_registered_on_base_metadata() -> None:
    assert "records" in Base.metadata.tables


def test_record_has_org_foreign_key_and_composite_index() -> None:
    table = typing_cast(Table, Record.__table__)
    fk_names = {constraint.name for constraint in table.foreign_key_constraints}
    assert fk_names == {"fk_records_organisation_id_organisations"}
    index_names = {index.name for index in table.indexes}
    assert index_names == {
        "ix_records_organisation_id",
        "ix_records_organisation_id_created_at",
    }
    composite = next(
        index for index in table.indexes if index.name == "ix_records_organisation_id_created_at"
    )
    assert [column.name for column in composite.columns] == [
        "organisation_id",
        "created_at",
    ]


# --- Request flow (acceptance §5.4, §5.5, §5.7) ---


async def _list_records(
    client: AsyncClient,
    token: str,
    org_id: uuid.UUID,
    params: dict[str, int] | None = None,
) -> Response:
    return await client.get(
        "/api/v1/records",
        params=params,
        headers={"Authorization": f"Bearer {token}", "X-Org-Id": str(org_id)},
    )


async def _create_record(
    client: AsyncClient,
    token: str,
    org_id: uuid.UUID,
    payload: dict[str, object],
) -> Response:
    return await client.post(
        "/api/v1/records",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "X-Org-Id": str(org_id)},
    )


async def test_list_requires_token(context_app: ContextApp) -> None:
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.get("/api/v1/records")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_list_requires_org_context(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/records",
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "org_context_required"


async def test_list_returns_pagination_envelope(context_app: ContextApp) -> None:
    """Acceptance §5.7: the list returns the documented pagination envelope."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.records = [
        make_record(org_id, title="Alpha"),
        make_record(org_id, title="Beta"),
    ]
    state.lookup_queue = [user, membership, 2]  # user, membership, then total count
    state.granted_permissions = {"records.read"}

    async with context_client(app) as client:
        response = await _list_records(client, make_token(private_key), org_id)

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [str(record.id) for record in state.records]
    assert [item["title"] for item in body["items"]] == ["Alpha", "Beta"]
    assert "body" not in body["items"][0]  # list items are summaries, not details


async def test_list_respects_page_size_parameter(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership, 3]
    state.granted_permissions = {"records.read"}

    async with context_client(app) as client:
        response = await _list_records(
            client, make_token(private_key), org_id, params={"page": 1, "page_size": 2}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert body["total"] == 3


async def test_viewer_write_is_denied(context_app: ContextApp) -> None:
    """Acceptance §5.5: a viewer can read but every write returns 403."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"records.read"}  # viewer bundle

    async with context_client(app) as client:
        token = make_token(private_key)
        response = await _create_record(client, token, org_id, {"title": "Sneaky", "body": ""})

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert state.records == []  # nothing was created


async def test_create_record_uses_context_org_id(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"records.read", "records.create"}

    async with context_client(app) as client:
        response = await _create_record(
            client, make_token(private_key), org_id, {"title": "Hello", "body": "World"}
        )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Hello"
    assert body["body"] == "World"
    (record,) = state.records
    assert record.organisation_id == org_id
    assert record.id == uuid.UUID(body["id"])


async def test_create_record_never_trusts_body_org_id(context_app: ContextApp) -> None:
    """Acceptance §5.4: an organisation id in the body is rejected, not honoured."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"records.read", "records.create"}

    payload: dict[str, object] = {
        "title": "Smuggled",
        "body": "x",
        "organisation_id": str(uuid.uuid4()),
    }
    async with context_client(app) as client:
        response = await _create_record(client, make_token(private_key), org_id, payload)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert state.records == []


async def test_get_record_returns_detail(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    record = make_record(org_id, title="Detail", body="The body")
    state.records = [record]
    state.lookup_queue = [user, membership, record]
    state.granted_permissions = {"records.read"}

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/records/{record.id}",
            headers={
                "Authorization": f"Bearer {make_token(private_key)}",
                "X-Org-Id": str(org_id),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(record.id)
    assert body["title"] == "Detail"
    assert body["body"] == "The body"


async def test_get_missing_record_is_404(context_app: ContextApp) -> None:
    """A record that does not exist (or is outside the org) is a 404."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership, None]  # org-scoped lookup finds nothing
    state.granted_permissions = {"records.read"}

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/records/{uuid.uuid4()}",
            headers={
                "Authorization": f"Bearer {make_token(private_key)}",
                "X-Org-Id": str(org_id),
            },
        )
    assert response.status_code == 404
    assert response.json()["code"] == "record_not_found"


async def test_update_record_changes_fields(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    record = make_record(org_id, title="Old", body="Old body")
    state.records = [record]
    state.lookup_queue = [user, membership, record]
    state.granted_permissions = {"records.read", "records.update"}

    async with context_client(app) as client:
        response = await client.patch(
            f"/api/v1/records/{record.id}",
            json={"title": "New"},
            headers={
                "Authorization": f"Bearer {make_token(private_key)}",
                "X-Org-Id": str(org_id),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "New"
    assert body["body"] == "Old body"  # untouched fields keep their values
    assert state.records[0].title == "New"


async def test_delete_record_removes_it(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    record = make_record(org_id)
    state.records = [record]
    # Deletion is gated by the platform-controlled records.deletion flag
    # (Scope §6.7); the override row makes the test organisation able to
    # delete records.
    state.feature_flags = [make_organisation_feature(org_id)]
    state.lookup_queue = [user, membership, record]
    state.granted_permissions = {"records.read", "records.delete"}

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/records/{record.id}",
            headers={
                "Authorization": f"Bearer {make_token(private_key)}",
                "X-Org-Id": str(org_id),
            },
        )

    assert response.status_code == 204
    assert state.records == []


# --- Service layer (BP §11) ---


async def test_get_record_outside_org_raises_not_found(context_app: ContextApp) -> None:
    """The service contract behind the 404: an unmatched org-scoped lookup."""
    _app, state, _private_key = context_app
    state.lookup_queue = [None]

    with pytest.raises(NotFoundError):
        await service.get_record(
            typing_cast(AsyncSession, FakeSession(state)),
            organisation_id=uuid.uuid4(),
            record_id=uuid.uuid4(),
        )


async def test_delete_record_outside_org_raises_not_found(context_app: ContextApp) -> None:
    _app, state, _private_key = context_app
    state.lookup_queue = [None]

    with pytest.raises(NotFoundError):
        await service.delete_record(
            typing_cast(AsyncSession, FakeSession(state)),
            organisation_id=uuid.uuid4(),
            record_id=uuid.uuid4(),
        )
