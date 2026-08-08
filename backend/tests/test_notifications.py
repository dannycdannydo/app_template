"""Tests for the notifications module (Scope §6.3, blueprint §20).

The request-flow tests run the full ASGI stack with the fakes from
``context_helpers.py``, so the suite needs neither PostgreSQL nor a network
connection; the real-database scoping, filter and delivery-lifecycle proofs
live in ``test_notifications_db.py``. These tests cover permission gating per
route, the list envelope (with the unread count riding on it), the
unread-count endpoint, mark-read semantics (including the not-found path) and
the test-send flow (notification + delivery + durable job + audit row in one
transaction).
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from sqlalchemy import CheckConstraint, Index, Table
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    build_context_app_fixture,
    context_client,
    make_membership,
    make_notification,
    make_user,
)

from app.db.base import Base
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.notifications.service import NOTIFICATION_TYPE_TEST


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


def _auth_headers(token: str, org_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": str(org_id)}


# --- Model metadata (BP §7, §10, §20) ---


def test_notification_tables_registered_on_base_metadata() -> None:
    for table_name in ("notifications", "notification_deliveries"):
        assert table_name in Base.metadata.tables


def test_notification_has_org_and_user_foreign_keys_and_indexes() -> None:
    table = cast(Table, Notification.__table__)
    fk_columns = sorted(constraint.columns.keys() for constraint in table.foreign_key_constraints)
    assert ["organisation_id"] in fk_columns
    assert ["user_id"] in fk_columns

    index_names = {index.name for index in table.indexes}
    assert "ix_notifications_organisation_id_user_id_created_at" in index_names
    assert "ix_notifications_organisation_id_user_id_read_at" in index_names


def test_delivery_has_status_and_attempt_constraints() -> None:
    table = cast(Table, NotificationDelivery.__table__)
    checks = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_notification_deliveries_delivery_status" in checks
    assert "ck_notification_deliveries_non_negative_attempt_count" in checks
    assert any(
        isinstance(constraint, Index)
        and constraint.name == "ix_notification_deliveries_notification_id"
        for constraint in table.indexes
    )


# --- Permission gating (default deny, BP §9) ---


async def test_list_requires_token(context_app: ContextApp) -> None:
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.get("/api/v1/notifications")
    assert response.status_code == 401


async def test_list_denied_without_notifications_read(context_app: ContextApp) -> None:
    """Default deny: a membership without notifications.read gets 403."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"records.read"}  # the viewer bundle: no notifications

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/notifications", headers=_auth_headers(make_token(private_key), org_id)
        )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


async def test_test_send_denied_without_notifications_manage(context_app: ContextApp) -> None:
    """A member with read-only notifications access cannot send a test."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"notifications.read"}

    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/notifications/test", headers=_auth_headers(make_token(private_key), org_id)
        )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert state.notifications == []  # nothing was created


# --- List endpoint (acceptance §5.5) ---


async def test_list_returns_envelope_with_unread_count(context_app: ContextApp) -> None:
    """The list returns the pagination envelope plus the caller's unread count."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.notifications = [
        make_notification(org_id, user.id, read_at=None),
        make_notification(org_id, user.id, read_at=None),
    ]
    # user, membership, then the total count and the unread count
    state.lookup_queue = [user, membership, 2, 1]
    state.granted_permissions = {"notifications.read"}

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/notifications", headers=_auth_headers(make_token(private_key), org_id)
        )

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["total"] == 2
    assert body["unread_count"] == 1
    assert {item["id"] for item in body["items"]} == {str(n.id) for n in state.notifications}
    assert body["items"][0]["type"] == NOTIFICATION_TYPE_TEST


async def test_list_accepts_type_filter_parameter(context_app: ContextApp) -> None:
    """The type query parameter is accepted (approved filter field, BP §12).

    The real WHERE clause is proven by the database tests; here we prove the
    parameter is wired and the envelope still comes back.
    """
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.notifications = [make_notification(org_id, user.id)]
    state.lookup_queue = [user, membership, 1, 0]
    state.granted_permissions = {"notifications.read"}

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/notifications?type=notification.test_sent",
            headers=_auth_headers(make_token(private_key), org_id),
        )
    assert response.status_code == 200
    assert response.json()["total"] == 1


# --- Unread-count endpoint ---


async def test_unread_count_endpoint(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership, 3]
    state.granted_permissions = {"notifications.read"}

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/notifications/unread-count",
            headers=_auth_headers(make_token(private_key), org_id),
        )
    assert response.status_code == 200
    assert response.json() == {"unread_count": 3}


# --- Mark-read endpoint ---


async def test_mark_read_sets_read_at(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    notification = make_notification(org_id, user.id, read_at=None)
    state.lookup_queue = [user, membership, notification]
    state.granted_permissions = {"notifications.read"}

    async with context_client(app) as client:
        response = await client.patch(
            f"/api/v1/notifications/{notification.id}/read",
            headers=_auth_headers(make_token(private_key), org_id),
        )
    assert response.status_code == 200
    assert response.json()["id"] == str(notification.id)
    assert response.json()["read_at"] is not None
    assert notification.read_at is not None


async def test_mark_read_unknown_notification_is_404(context_app: ContextApp) -> None:
    """A notification that does not exist is a 404 (isolation boundary)."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership, None]
    state.granted_permissions = {"notifications.read"}

    async with context_client(app) as client:
        response = await client.patch(
            f"/api/v1/notifications/{uuid.uuid4()}/read",
            headers=_auth_headers(make_token(private_key), org_id),
        )
    assert response.status_code == 404
    assert response.json()["code"] == "notification_not_found"


# --- Test-send endpoint (acceptance §5.5) ---


async def test_test_send_creates_notification_delivery_job_and_audit(
    context_app: ContextApp,
) -> None:
    """One transaction: notification + email delivery + durable job + audit."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"notifications.manage"}

    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/notifications/test",
            headers=_auth_headers(make_token(private_key), org_id),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["type"] == NOTIFICATION_TYPE_TEST
    assert body["read_at"] is None

    # The notification, its email delivery, the durable job and the audit row
    # were all written (the fake session persists them on commit).
    assert len(state.notifications) == 1
    assert len(state.notification_deliveries) == 1
    assert len(state.jobs) == 1
    assert state.notification_deliveries[0].recipient == user.email
    assert state.notification_deliveries[0].status.value == "queued"
    assert state.jobs[0].job_type == "notification.email"
    assert state.jobs[0].input_reference == str(state.notification_deliveries[0].id)
    assert any(event.action == "notification.test_sent" for event in state.audit_events)
