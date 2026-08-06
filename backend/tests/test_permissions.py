"""Tests for the roles and permissions model (v0.2 Scope §6.4, BP §9, §10).

Metadata checks are pure Python and run everywhere; permission enforcement and
the role-assignment service run against the in-memory fakes from
``context_helpers.py`` so the suite needs neither PostgreSQL nor a network
connection. The real-database seed and mutation checks live in
``test_permissions_db.py``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, cast

import pytest
from fastapi import Depends
from httpx import AsyncClient, Response
from sqlalchemy import Table, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    FakeSession,
    build_context_app_fixture,
    context_client,
    make_membership,
    make_user,
)

from app.api.dependencies import require_permission
from app.core.exceptions import NotFoundError
from app.db.base import Base
from app.modules.organisations.models import OrganisationMembership
from app.modules.permissions.constants import ALL_PERMISSION_CODES, PERMISSIONS, ROLE_PERMISSION_MAP
from app.modules.permissions.models import Permission, Role, RolePermission
from app.modules.permissions.service import assign_role


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


def _build_permission_app(context_app: ContextApp) -> ContextApp:
    """Return the context app with a probe route gated by ``records.create``."""
    app, state, private_key = context_app

    async def _probe_permission(
        membership: Annotated[
            OrganisationMembership, Depends(require_permission("records.create"))
        ],
    ) -> dict[str, str]:
        return {"organisation_id": str(membership.organisation_id)}

    app.add_api_route("/_test/permission", _probe_permission, methods=["GET"])
    return app, state, private_key


# --- Model metadata (BP §9, §10) ---


def test_permission_tables_registered_on_base_metadata() -> None:
    for table_name in ("permissions", "role_permissions"):
        assert table_name in Base.metadata.tables


def test_permission_code_is_unique() -> None:
    table = cast(Table, Permission.__table__)
    unique = next(
        c
        for c in table.constraints
        if isinstance(c, UniqueConstraint) and c.name == "uq_permissions_code"
    )
    assert list(unique.columns) == [table.c.code]


def test_role_permission_unique_pair_and_foreign_keys() -> None:
    table = cast(Table, RolePermission.__table__)
    unique = next(
        c
        for c in table.constraints
        if isinstance(c, UniqueConstraint) and c.name == "uq_role_permissions_role_id_permission_id"
    )
    assert {column.name for column in unique.columns} == {"role_id", "permission_id"}
    fk_names = {constraint.name for constraint in table.foreign_key_constraints}
    assert fk_names == {
        "fk_role_permissions_role_id_roles",
        "fk_role_permissions_permission_id_permissions",
    }


# --- Seed catalogue (v0.2 Scope §6.4 checklist: data migration seed) ---


def test_permission_codes_are_unique_in_catalogue() -> None:
    codes = [code for code, _ in PERMISSIONS]
    assert len(codes) == len(set(codes))


def test_role_permission_map_covers_only_seeded_roles() -> None:
    seeded_roles = {"owner", "administrator", "manager", "member", "viewer"}
    assert set(ROLE_PERMISSION_MAP) == seeded_roles


def test_role_permission_map_grants_only_catalogue_permissions() -> None:
    catalogue = set(ALL_PERMISSION_CODES)
    for role, granted in ROLE_PERMISSION_MAP.items():
        assert set(granted) <= catalogue, f"role {role} grants an unknown permission"


def test_owner_holds_every_permission() -> None:
    assert set(ROLE_PERMISSION_MAP["owner"]) == set(ALL_PERMISSION_CODES)


def test_manage_roles_reserved_for_owner_and_administrator() -> None:
    """v0.2 Scope §6.4: role assignment is gated on ``users.manage_roles``."""
    holders = {
        role for role, granted in ROLE_PERMISSION_MAP.items() if "users.manage_roles" in granted
    }
    assert holders == {"owner", "administrator"}


def test_viewer_can_read_records_but_not_write() -> None:
    """Acceptance §5.5 precondition: viewer reads records, every write is denied."""
    granted = set(ROLE_PERMISSION_MAP["viewer"])
    assert "records.read" in granted
    assert granted & {"records.create", "records.update", "records.delete"} == set()


# --- Default-deny enforcement (v0.2 Scope §6.4 checklist: require_permission) ---


async def _get_permission_probe(client: AsyncClient, token: str, org_id: uuid.UUID) -> Response:
    return await client.get(
        "/_test/permission",
        headers={"Authorization": f"Bearer {token}", "X-Org-Id": str(org_id)},
    )


async def test_require_permission_denies_unlisted_permission(context_app: ContextApp) -> None:
    """A permission not granted to any of the caller's roles is denied."""
    app, state, private_key = _build_permission_app(context_app)
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, uuid.uuid4())
    state.lookup_queue = [user, membership]  # user, then membership context
    state.granted_permissions = {"records.read"}  # granted, but not records.create

    async with context_client(app) as client:
        response = await _get_permission_probe(
            client, make_token(private_key), membership.organisation_id
        )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


async def test_require_permission_allows_granted_permission(context_app: ContextApp) -> None:
    app, state, private_key = _build_permission_app(context_app)
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, uuid.uuid4())
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"records.read", "records.create"}

    async with context_client(app) as client:
        response = await _get_permission_probe(
            client, make_token(private_key), membership.organisation_id
        )

    assert response.status_code == 200
    assert response.json()["organisation_id"] == str(membership.organisation_id)


# --- Role-assignment service (v0.2 Scope §6.4 checklist) ---


async def test_assign_role_links_membership_and_role(context_app: ContextApp) -> None:
    _app, state, _private_key = context_app
    membership = make_membership(make_user(), uuid.uuid4())
    role = Role(code="manager", name="Manager")
    role.id = uuid.uuid4()
    state.lookup_queue = [role, membership]

    await assign_role(
        cast(AsyncSession, FakeSession(state)), membership_id=membership.id, role_code="manager"
    )

    (link,) = state.membership_roles
    assert link.membership_id == membership.id
    assert link.role_id == role.id


async def test_assign_role_rejects_unknown_membership(context_app: ContextApp) -> None:
    _app, state, _private_key = context_app
    role = Role(code="manager", name="Manager")
    role.id = uuid.uuid4()
    state.lookup_queue = [role, None]  # role found, but no membership row matches

    with pytest.raises(NotFoundError):
        await assign_role(
            cast(AsyncSession, FakeSession(state)), membership_id=uuid.uuid4(), role_code="manager"
        )
    assert state.membership_roles == []


async def test_assign_role_rejects_unknown_role(context_app: ContextApp) -> None:
    _app, state, _private_key = context_app
    membership = make_membership(make_user(), uuid.uuid4())
    state.lookup_queue = [None]  # no role row matches

    with pytest.raises(NotFoundError):
        await assign_role(
            cast(AsyncSession, FakeSession(state)), membership_id=membership.id, role_code="nobody"
        )
    assert state.membership_roles == []
