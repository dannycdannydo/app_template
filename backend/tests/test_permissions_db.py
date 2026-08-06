"""Real-database integration tests for roles and permissions (v0.2 Scope §6.4).

The fakes in ``test_permissions.py`` prove the request-flow contract but never
execute SQL, so the seed data migration and the role-assignment SQL could
silently regress. These tests run the real migration, the real permission query
and the real role-assignment service against a reachable PostgreSQL, using the
same skip pattern as ``test_db.py`` and ``test_organisations_db.py``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.permissions.constants import PERMISSIONS, ROLE_PERMISSION_MAP
from app.modules.permissions.models import Permission, Role, RolePermission
from app.modules.permissions.queries import permission_codes_for_membership
from app.modules.permissions.service import assign_role, list_membership_roles, remove_role
from app.modules.users.models import User
from app.modules.users.service import get_me_payload

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _database_reachable(database_url: str) -> bool:
    """Probe the configured database with a short async engine connect."""

    async def _probe() -> bool:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(_probe())


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards.

    Requires a reachable PostgreSQL as configured by ``DATABASE_URL``; skipped
    otherwise. Reverting to base keeps the test database clean for the other
    migration smoke tests, whichever runs first.
    """
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


async def _permission_codes_for_role(session: AsyncSession, role_code: str) -> set[str]:
    rows = await session.scalars(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .where(Role.code == role_code)
    )
    return set(rows.all())


async def test_seed_migration_creates_catalogue_and_grants(migrated_database: str) -> None:
    """Acceptance §5.5 against real PostgreSQL: roles and permissions are seeded."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            role_count = await session.scalar(select(func.count()).select_from(Role))
            assert role_count == 5

            permission_count = await session.scalar(select(func.count()).select_from(Permission))
            assert permission_count == len(PERMISSIONS)

            for role_code, expected in ROLE_PERMISSION_MAP.items():
                assert await _permission_codes_for_role(session, role_code) == set(expected)
    finally:
        await engine.dispose()


async def test_default_deny_without_roles(migrated_database: str) -> None:
    """A membership with no roles grants nothing (default deny)."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            user = User(workos_user_id="user_perm_none", email="none@example.com", name="No Roles")
            organisation = Organisation(name="No Roles Ltd")
            session.add_all([user, organisation])
            await session.commit()

            membership = OrganisationMembership(
                user_id=user.id,
                organisation_id=organisation.id,
                status=MembershipStatus.ACTIVE,
            )
            session.add(membership)
            await session.commit()

            assert await permission_codes_for_membership(session, membership.id) == set()
    finally:
        await engine.dispose()


async def test_assign_and_remove_role_changes_grants(migrated_database: str) -> None:
    """Role assignment works: grants follow the assigned role, removal revokes."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            user = User(
                workos_user_id="user_perm_assign",
                email="assign@example.com",
                name="Role Assign",
            )
            organisation = Organisation(name="Assign Ltd")
            session.add_all([user, organisation])
            await session.commit()

            membership = OrganisationMembership(
                user_id=user.id,
                organisation_id=organisation.id,
                status=MembershipStatus.ACTIVE,
            )
            session.add(membership)
            await session.commit()
            # Capture the ids up front: assign_role's conflict path rolls back,
            # which expires session instances and breaks lazy attribute access.
            membership_id = membership.id
            user_id = user.id

            await assign_role(session, membership_id=membership_id, role_code="viewer")
            assert await permission_codes_for_membership(session, membership_id) == {
                "records.read",
                "properties.read",
                "documents.read",
            }

            # Duplicate assignment conflicts with the unique constraint.
            with pytest.raises(ConflictError):
                await assign_role(session, membership_id=membership_id, role_code="viewer")

            # Unknown role codes are not found.
            with pytest.raises(NotFoundError):
                await assign_role(session, membership_id=membership_id, role_code="superadmin")

            # Unknown memberships are not found (never a misleading conflict).
            with pytest.raises(NotFoundError):
                await assign_role(session, membership_id=uuid.uuid4(), role_code="viewer")

            roles = await list_membership_roles(session, membership_id)
            assert [role.code for role in roles] == ["viewer"]

            # /me (v0.2 Scope §6.4) surfaces the same role codes the permission
            # checks enforce. Re-fetch the user because the conflict-path
            # rollback expired the earlier instance.
            user_row = await session.get(User, user_id)
            assert user_row is not None
            _memberships, me_roles = await get_me_payload(session, user_row)
            assert me_roles == ["viewer"]

            await remove_role(session, membership_id=membership_id, role_code="viewer")
            assert await permission_codes_for_membership(session, membership_id) == set()

            # Removing a role the member does not hold is not found.
            with pytest.raises(NotFoundError):
                await remove_role(session, membership_id=membership_id, role_code="viewer")
    finally:
        await engine.dispose()
