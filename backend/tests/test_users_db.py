"""Real-database integration tests for user identity queries (blueprint §9).

The users module's reusable statements (WorkOS-id lookup, membership listing
and the role-graph join) were extracted into ``queries.py`` so the join is
named in one place, matching every sibling module. These tests run the real
migration and the extracted statements against a reachable PostgreSQL to lock
in the extraction, using the same skip pattern as ``test_permissions_db.py``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.permissions.models import MembershipRole, Role
from app.modules.users.models import User
from app.modules.users.queries import (
    memberships_for_user_statement,
    role_codes_for_user_statement,
    user_by_workos_id_statement,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _database_reachable(database_url: str) -> bool:
    """Probe the configured database with a short async engine connect."""

    async def _probe() -> bool:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(select(1))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(_probe())


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


async def test_user_by_workos_id_statement_finds_existing(migrated_database: str) -> None:
    """The statement resolves a user row by WorkOS identity."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            user = User(
                workos_user_id="user_query_lookup",
                email="lookup@example.com",
                name="Lookup",
            )
            session.add(user)
            await session.commit()

            found = await session.scalar(user_by_workos_id_statement("user_query_lookup"))
            assert found is not None
            assert found.id == user.id

            missing = await session.scalar(user_by_workos_id_statement("unknown_identity"))
            assert missing is None
    finally:
        await engine.dispose()


async def test_memberships_for_user_statement_orders_oldest_first(migrated_database: str) -> None:
    """The statement returns the user's memberships ordered by creation time."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            user = User(
                workos_user_id="user_query_memberships",
                email="memberships@example.com",
                name="Memberships",
            )
            org_a = Organisation(name="Org A")
            org_b = Organisation(name="Org B")
            session.add_all([user, org_a, org_b])
            await session.commit()

            first = OrganisationMembership(
                user_id=user.id,
                organisation_id=org_a.id,
                status=MembershipStatus.ACTIVE,
            )
            second = OrganisationMembership(
                user_id=user.id,
                organisation_id=org_b.id,
                status=MembershipStatus.ACTIVE,
            )
            session.add_all([first, second])
            await session.commit()

            memberships = (await session.scalars(memberships_for_user_statement(user.id))).all()
            assert [m.id for m in memberships] == [first.id, second.id]
    finally:
        await engine.dispose()


async def test_role_codes_for_user_statement_joins_role_graph(migrated_database: str) -> None:
    """The statement resolves distinct role codes across the user's memberships."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            user = User(
                workos_user_id="user_query_roles",
                email="roles@example.com",
                name="Roles",
            )
            organisation = Organisation(name="Roles Ltd")
            session.add_all([user, organisation])
            await session.commit()

            membership = OrganisationMembership(
                user_id=user.id,
                organisation_id=organisation.id,
                status=MembershipStatus.ACTIVE,
            )
            session.add(membership)
            await session.commit()

            # No roles assigned yet: default deny yields no codes.
            assert (await session.scalars(role_codes_for_user_statement(user.id))).all() == []

            viewer_role = await session.scalar(select(Role).where(Role.code == "viewer"))
            admin_role = await session.scalar(select(Role).where(Role.code == "administrator"))
            assert viewer_role is not None
            assert admin_role is not None
            session.add_all(
                [
                    MembershipRole(membership_id=membership.id, role_id=viewer_role.id),
                    MembershipRole(membership_id=membership.id, role_id=admin_role.id),
                ]
            )
            await session.commit()

            codes = (await session.scalars(role_codes_for_user_statement(user.id))).all()
            assert codes == ["administrator", "viewer"]
    finally:
        await engine.dispose()
