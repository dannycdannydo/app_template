"""Tests for the append-only audit module and platform plane (Scope §6.1, §6.2).

Metadata checks are pure Python and run everywhere; the request-flow tests use
the in-memory fakes from ``context_helpers.py`` so the suite needs neither
PostgreSQL nor a network connection. The real-database proofs — the migration
applies cleanly, the filters actually filter and rows round-trip — live in
``test_audit_db.py`` and the migration smoke test in ``test_db.py``.
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import Table, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    ContextState,
    FakeSession,
    build_context_app_fixture,
    context_client,
    make_audit_event,
    make_organisation_feature,
    make_user,
)

from app.db.base import Base
from app.main import create_app
from app.modules.audit import service
from app.modules.audit.models import AuditEvent
from app.modules.audit.queries import (
    audit_events_count_statement,
    audit_events_statement,
)
from app.modules.permissions.constants import (
    ALL_PERMISSION_CODES,
    PLATFORM_ADMIN_ROLE_CODE,
    PLATFORM_ALL_PERMISSION_CODES,
    PLATFORM_PERMISSIONS,
)
from app.modules.platform_admin.models import (
    PlatformMembership,
    PlatformRole,
    PlatformRolePermission,
)
from app.modules.records import service as records_service


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


def _table_of(model: type[Base]) -> Table:
    """Return the mapped :class:`Table` with precise typing for introspection."""
    return cast(Table, model.__table__)


# --- Model metadata (Scope §6.1, BP §29, §10) ---


def test_audit_events_table_registered_on_base_metadata() -> None:
    assert "audit_events" in Base.metadata.tables


def test_audit_event_is_append_only_by_construction() -> None:
    """Scope §6.1: no update column and no timestamps mixin on the audit row."""
    table = _table_of(AuditEvent)
    assert "updated_at" not in table.c
    assert "deleted_at" not in table.c


def test_audit_event_columns_follow_blueprint_shape() -> None:
    table = _table_of(AuditEvent)
    assert table.c.organisation_id.nullable is True
    assert table.c.actor_user_id.nullable is True
    assert not table.c.action.nullable
    assert not table.c.resource_type.nullable
    assert not table.c.resource_id.nullable
    assert not table.c.metadata.nullable
    assert table.c.action.index is True
    assert isinstance(table.c.metadata.type, postgresql.JSONB)


def test_audit_event_foreign_keys_and_indexes() -> None:
    table = _table_of(AuditEvent)
    fk_names = {constraint.name for constraint in table.foreign_key_constraints}
    assert fk_names == {
        "fk_audit_events_organisation_id_organisations",
        "fk_audit_events_actor_user_id_users",
    }
    index_names = {index.name for index in table.indexes}
    assert index_names == {
        "ix_audit_events_organisation_id",
        "ix_audit_events_actor_user_id",
        "ix_audit_events_action",
        "ix_audit_events_organisation_id_created_at",
    }
    composite = next(
        index
        for index in table.indexes
        if index.name == "ix_audit_events_organisation_id_created_at"
    )
    assert [column.name for column in composite.columns] == [
        "organisation_id",
        "created_at",
    ]


# --- Platform plane model metadata (Scope §6.2) ---


def test_platform_tables_registered_on_base_metadata() -> None:
    for table_name in ("platform_roles", "platform_role_permissions", "platform_memberships"):
        assert table_name in Base.metadata.tables


def test_platform_role_code_is_unique() -> None:
    table = _table_of(PlatformRole)
    unique = next(
        c
        for c in table.constraints
        if isinstance(c, UniqueConstraint) and c.name == "uq_platform_roles_code"
    )
    assert list(unique.columns) == [table.c.code]


def test_platform_membership_unique_user_and_role() -> None:
    table = _table_of(PlatformMembership)
    unique = next(
        c
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
        and c.name == "uq_platform_memberships_user_id_platform_role_id"
    )
    assert {column.name for column in unique.columns} == {"user_id", "platform_role_id"}


def test_platform_role_permission_unique_pair() -> None:
    table = _table_of(PlatformRolePermission)
    unique = next(
        c
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
        and c.name == "uq_platform_role_permissions_platform_role_id_permission_id"
    )
    assert {column.name for column in unique.columns} == {
        "platform_role_id",
        "permission_id",
    }


def test_platform_permissions_are_kept_out_of_org_role_bundles() -> None:
    """Scope §6.2: an org owner's bundle must never include platform codes."""
    assert PLATFORM_PERMISSIONS
    assert PLATFORM_ALL_PERMISSION_CODES == ("platform.admin",)
    assert not set(PLATFORM_ALL_PERMISSION_CODES) & set(ALL_PERMISSION_CODES)
    assert PLATFORM_ADMIN_ROLE_CODE == "platform_admin"


# --- record_event service (Scope §6.1) ---


async def test_record_event_writes_row_with_request_context() -> None:
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()

    event = await service.record_event(
        session,
        organisation_id=org_id,
        actor_user_id=actor_id,
        action="organisation.created",
        resource_type="organisation",
        resource_id=str(org_id),
        metadata={"extra": "value"},
    )

    assert event.action == "organisation.created"
    assert event.organisation_id == org_id
    assert event.actor_user_id == actor_id
    assert event.resource_type == "organisation"
    assert event.resource_id == str(org_id)
    assert event.event_metadata["extra"] == "value"
    # The request id is always stamped into the metadata; the middleware has
    # not run in a bare service call, so it is the empty default.
    assert event.event_metadata["request_id"] == ""
    # Committing the calling service's transaction persists the row, which is
    # then never modified or removed (append-only).
    await session.commit()
    assert len(state.audit_events) == 1
    assert state.audit_events[0].id == event.id


async def test_record_event_flush_does_not_commit() -> None:
    """BP §11: the calling service owns the transaction; record_event only flushes."""
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))

    await service.record_event(
        session,
        action="record.created",
        resource_type="record",
        resource_id="record-1",
    )

    # Flushed but not committed: the row has an id but is not yet in state.
    assert state.audit_events == []


# --- list_audit_events service (Scope §6.1) ---


async def test_list_audit_events_returns_pagination_envelope() -> None:
    state = ContextState()
    event_a = make_audit_event(action="record.created")
    event_b = make_audit_event(action="record.deleted")
    state.audit_events = [event_a, event_b]
    state.lookup_queue = [2]  # the total count
    session: AsyncSession = cast(AsyncSession, FakeSession(state))

    events, total = await service.list_audit_events(session, page=1, page_size=50)

    assert total == 2
    assert [event.id for event in events] == [event_a.id, event_b.id]


async def test_audit_events_statement_filters_on_approved_columns() -> None:
    """Scope §6.1: the listing filters only on the approved columns."""
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    statement = audit_events_statement(
        organisation_id=org_id,
        actor_user_id=actor_id,
        action="organisation.created",
    )
    compiled = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "audit_events.organisation_id" in compiled
    assert "audit_events.actor_user_id" in compiled
    assert "audit_events.action" in compiled
    assert "updated_at" not in compiled  # nothing in the audit query touches update state


async def test_audit_events_count_statement_counts_filtered_set() -> None:
    statement = audit_events_count_statement(action="organisation.created")
    compiled = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "count(" in compiled
    assert "audit_events" in compiled


# --- Platform-gated listing endpoint (Scope §6.1, §6.2) ---


async def _list_audit_events(
    client: AsyncClient,
    token: str,
    params: dict[str, str | int] | None = None,
) -> Response:
    return await client.get(
        "/api/v1/platform/audit-events",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_audit_events_requires_authentication(context_app: ContextApp) -> None:
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.get("/api/v1/platform/audit-events")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_audit_events_denied_without_platform_permission(context_app: ContextApp) -> None:
    """Scope §6.2: org authorisation never grants platform access."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    state.granted_permissions = {"organisation.manage"}  # the org-owner bundle

    async with context_client(app) as client:
        response = await _list_audit_events(client, make_token(private_key))

    assert response.status_code == 403
    assert response.json()["code"] == "platform_admin_required"


async def test_audit_events_platform_admin_lists_events(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    event_a = make_audit_event(organisation_id=org_id, action="organisation.created")
    event_b = make_audit_event(organisation_id=org_id, action="record.deleted")
    state.audit_events = [event_a, event_b]
    state.lookup_queue = [user, 2]  # user lookup, then the total count
    state.granted_permissions = {"platform.admin"}

    async with context_client(app) as client:
        response = await _list_audit_events(client, make_token(private_key))

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [str(event_a.id), str(event_b.id)]
    assert body["items"][0]["action"] == "organisation.created"
    assert body["items"][0]["organisation_id"] == str(org_id)


async def test_audit_events_accepts_filter_parameters(context_app: ContextApp) -> None:
    """Scope §6.1: the endpoint wires org/actor/action filters into the service."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    state.audit_events = [make_audit_event(organisation_id=org_id, action="record.created")]
    state.lookup_queue = [user, 1]
    state.granted_permissions = {"platform.admin"}

    async with context_client(app) as client:
        response = await _list_audit_events(
            client,
            make_token(private_key),
            params={
                "organisation_id": str(org_id),
                "actor_user_id": str(actor_id),
                "action": "record.created",
                "page": 1,
                "page_size": 10,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["page_size"] == 10
    assert body["total"] == 1


async def test_audit_events_exposes_only_approved_filter_parameters() -> None:
    """BP §12: the endpoint declares only the approved query filter fields."""
    operation = create_app().openapi()["paths"]["/api/v1/platform/audit-events"]["get"]
    query_parameters = {
        parameter["name"]
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "query"
    }
    assert query_parameters == {
        "page",
        "page_size",
        "organisation_id",
        "actor_user_id",
        "action",
    }


# --- Representative mutations write audit rows (Scope §6.1) ---


async def test_create_organisation_writes_audit_event(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user, state.owner_role]

    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/organisations",
            json={"name": "Acme"},
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )

    assert response.status_code == 201
    (organisation,) = state.organisations
    (event,) = state.audit_events
    assert event.action == "organisation.created"
    assert event.resource_type == "organisation"
    assert event.resource_id == str(organisation.id)
    assert event.organisation_id == organisation.id
    assert event.actor_user_id == user.id
    # The full request stack stamps the bound request id into the audit row.
    assert event.event_metadata["request_id"]


async def test_record_mutations_write_audit_events() -> None:
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    # Deletion is gated by the records.deletion feature flag (Scope §6.7);
    # the override row lets this flow exercise the delete path.
    state.feature_flags = [make_organisation_feature(org_id)]

    record = await records_service.create_record(
        session,
        organisation_id=org_id,
        title="First",
        body="Body",
        actor_user_id=actor_id,
    )
    assert state.audit_events[-1].action == "record.created"
    assert state.audit_events[-1].actor_user_id == actor_id

    state.lookup_queue = [record]
    await records_service.update_record(
        session,
        organisation_id=org_id,
        record_id=record.id,
        title="Second",
        body=None,
        actor_user_id=actor_id,
    )
    assert state.audit_events[-1].action == "record.updated"

    state.lookup_queue = [record]
    await records_service.delete_record(
        session,
        organisation_id=org_id,
        record_id=record.id,
        actor_user_id=actor_id,
    )
    assert state.audit_events[-1].action == "record.deleted"
    assert len(state.audit_events) == 3


async def test_no_write_endpoints_exist_for_audit_events() -> None:
    """Append-only by construction: no create/update/delete route is registered."""
    paths = create_app().openapi()["paths"]
    assert set(paths["/api/v1/platform/audit-events"]) == {"get"}
