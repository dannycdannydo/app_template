"""Real-database integration tests for organisation creation (v0.2 Scope §6.3).

The fakes in ``context_helpers.py`` prove the request-flow contract (wiring,
status codes, error mapping) but never execute SQL, so the enum-persistence bug
fixed in this work unit (the ORM storing enum member names ``"ACTIVE"`` instead
of values ``"active"`` against the check constraint) could silently regress.
These tests run the real ``create_organisation`` service and raw membership
INSERTs against a reachable PostgreSQL, using the same skip pattern as the
migration smoke test in ``test_db.py``, and assert the raw stored values so
that exact bug class is locked in.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.organisations.service import create_organisation
from app.modules.permissions.models import OWNER_ROLE_CODE, MembershipRole, Role
from app.modules.users.models import User

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
    otherwise. Reverting to base keeps the test database clean for the
    migration smoke test in ``test_db.py``, whichever runs first.
    """
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


async def _raw_status(session: AsyncSession, membership_id: object) -> str:
    statement = text("SELECT status FROM organisation_memberships WHERE id = :membership_id")
    return await session.scalar(statement, {"membership_id": membership_id})


async def test_create_organisation_end_to_end_persists_owner_membership(
    migrated_database: str,
) -> None:
    """Acceptance §5.3 against real PostgreSQL: creator becomes the owner.

    Exercises the SQL path the v0.2 §6.3 fakes cannot: a real INSERT of an
    organisation, an active membership, and the owner role link, then reads the
    raw ``status`` value back to prove the enum fix holds against the check
    constraint.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            creator = User(workos_user_id="user_e2e_owner", email="owner@example.com", name="Ada")
            session.add(creator)
            await session.commit()

            organisation = await create_organisation(session, creator, "End-to-End Ltd")

            owner_role = await session.scalar(select(Role).where(Role.code == OWNER_ROLE_CODE))
            assert owner_role is not None

            membership = await session.scalar(
                select(OrganisationMembership).where(
                    OrganisationMembership.organisation_id == organisation.id
                )
            )
            assert membership is not None
            assert membership.user_id == creator.id
            assert membership.status == MembershipStatus.ACTIVE

            raw_status = await _raw_status(session, membership.id)
            assert raw_status == "active"  # the value, not the member name "ACTIVE"

            role_link = await session.scalar(
                select(MembershipRole).where(MembershipRole.membership_id == membership.id)
            )
            assert role_link is not None
            assert role_link.role_id == owner_role.id
    finally:
        await engine.dispose()


async def test_membership_enum_persists_lowercase_values_on_real_database(
    migrated_database: str,
) -> None:
    """Every membership status stores its value, never its name (regression lock).

    Before the ``values_callable`` fix every membership INSERT against
    PostgreSQL failed the ``membership_status`` check constraint because the
    ORM wrote ``"ACTIVE"`` instead of ``"active"``.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            for index, status in enumerate(MembershipStatus):
                user = User(
                    workos_user_id=f"user_e2e_status_{index}",
                    email=f"status-{index}@example.com",
                    name="Bob",
                )
                organisation = Organisation(name=f"Status {status.value} Ltd")
                session.add_all([user, organisation])
                await session.commit()

                membership = OrganisationMembership(
                    user_id=user.id,
                    organisation_id=organisation.id,
                    status=status,
                )
                session.add(membership)
                await session.commit()

                raw_status = await _raw_status(session, membership.id)
                assert raw_status == status.value
    finally:
        await engine.dispose()
