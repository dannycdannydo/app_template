"""Real-database integration tests for membership administration (Scope §6.6).

The fakes in ``test_membership_admin.py`` prove the request-flow contract but
never execute SQL, so the persistence of the role round-trip, the suspension
status and invitation revocation, and the membership-role cascade could
silently regress. These tests run the real migration and the real services
against a reachable PostgreSQL, using the same skip pattern as
``test_invitations_db.py``: migrated to head up front, reverted to base
afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.context_helpers import FakeWorkOSInvitationsProvider

from app.modules.invitations.models import Invitation, InvitationStatus
from app.modules.organisations.models import MembershipStatus, Organisation, OrganisationMembership
from app.modules.platform_admin import service
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

    Reverting to base keeps the test database clean for the other migration
    smoke tests, whichever runs first.
    """
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


def _session_factory(database_url: str):
    engine = create_async_engine(database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_membership(
    session: AsyncSession, *, actor: User, member: User
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create an organisation and an active membership; return their ids."""
    organisation = Organisation(
        name=f"Membership Ltd {uuid.uuid4().hex[:8]}", workos_organisation_id=None
    )
    session.add(organisation)
    await session.commit()
    organisation_id = organisation.id
    membership = OrganisationMembership(
        user_id=member.id,
        organisation_id=organisation_id,
        status=MembershipStatus.ACTIVE,
    )
    session.add(membership)
    await session.commit()
    return organisation_id, membership.id


async def test_role_assignment_and_removal_round_trip(migrated_database: str) -> None:
    """Scope §6.6: assign then remove a role, both persisting with audit rows."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = User(
                workos_user_id=f"admin_{uuid.uuid4().hex[:8]}",
                email="platform@example.com",
                name="Platform Admin",
            )
            member = User(
                workos_user_id=f"member_{uuid.uuid4().hex[:8]}",
                email="grace@example.com",
                name="Grace Hopper",
            )
            session.add_all([actor, member])
            await session.commit()
            organisation_id, membership_id = await _seed_membership(
                session, actor=actor, member=member
            )

        # Assign the member role through the real service.
        async with session_factory() as session:
            detail = await service.assign_role(
                session,
                actor=actor,
                organisation_id=organisation_id,
                membership_id=membership_id,
                role_code="member",
            )
            await session.commit()
            assert detail.roles == ["member"]

        # Remove it again; the membership ends up with no roles.
        async with session_factory() as session:
            detail = await service.remove_role(
                session,
                actor=actor,
                organisation_id=organisation_id,
                membership_id=membership_id,
                role_code="member",
            )
            await session.commit()
            assert detail.roles == []

            role_rows = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM membership_roles WHERE membership_id = :mid"
                    ).bindparams(mid=membership_id)
                )
            ).scalar_one()
            assert role_rows == 0

            actions = (
                await session.execute(
                    text(
                        "SELECT action, metadata FROM audit_events "
                        "WHERE resource_type = 'membership' AND resource_id = :rid"
                    ).bindparams(rid=str(membership_id))
                )
            ).all()
            assert [row.action for row in actions] == [
                "membership.role_changed",
                "membership.role_changed",
            ]
            assert actions[0].metadata["action"] == "assigned"
            assert actions[0].metadata["role_code"] == "member"
            assert actions[1].metadata["action"] == "removed"
    finally:
        await engine.dispose()


async def test_suspension_persists_and_revokes_pending_invitations(
    migrated_database: str,
) -> None:
    """Scope §6.6 / design plan §9 item 5: suspension sticks and cleans up."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = User(
                workos_user_id=f"admin_{uuid.uuid4().hex[:8]}",
                email="platform@example.com",
                name="Platform Admin",
            )
            member = User(
                workos_user_id=f"member_{uuid.uuid4().hex[:8]}",
                email="grace@example.com",
                name="Grace Hopper",
            )
            session.add_all([actor, member])
            await session.commit()
            organisation_id, membership_id = await _seed_membership(
                session, actor=actor, member=member
            )
            session.add(
                Invitation(
                    organisation_id=organisation_id,
                    email="grace@example.com",
                    role_code="member",
                    workos_invitation_id="inv_workos_suspend",
                    invited_by_user_id=actor.id,
                    status=InvitationStatus.SENT,
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                )
            )
            await session.commit()

        provider = FakeWorkOSInvitationsProvider()
        async with session_factory() as session:
            detail = await service.set_membership_status(
                session,
                actor=actor,
                organisation_id=organisation_id,
                membership_id=membership_id,
                status=MembershipStatus.SUSPENDED,
                workos_invitations=provider,
            )
            await session.commit()
            assert detail.membership.status == MembershipStatus.SUSPENDED

            fresh = await session.get(OrganisationMembership, membership_id)
            assert fresh is not None
            assert fresh.status == MembershipStatus.SUSPENDED

            invitation = await session.scalar(
                select(Invitation).where(Invitation.workos_invitation_id == "inv_workos_suspend")
            )
            assert invitation is not None
            assert invitation.status == InvitationStatus.REVOKED
            assert provider.revoked == ["inv_workos_suspend"]

            actions = (
                (
                    await session.execute(
                        text(
                            "SELECT action FROM audit_events "
                            "WHERE resource_type = 'membership' AND resource_id = :rid"
                        ).bindparams(rid=str(membership_id))
                    )
                )
                .scalars()
                .all()
            )
            assert actions == ["membership.suspended"]
            revoked_actions = (
                (
                    await session.execute(
                        text(
                            "SELECT action FROM audit_events "
                            "WHERE resource_type = 'invitation' AND resource_id = :rid"
                        ).bindparams(rid=str(invitation.id))
                    )
                )
                .scalars()
                .all()
            )
            assert revoked_actions == ["invitation.revoked"]
    finally:
        await engine.dispose()


async def test_reactivation_persists(migrated_database: str) -> None:
    """Reactivating a suspended membership restores the active status."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = User(
                workos_user_id=f"admin_{uuid.uuid4().hex[:8]}",
                email="platform@example.com",
                name="Platform Admin",
            )
            member = User(
                workos_user_id=f"member_{uuid.uuid4().hex[:8]}",
                email="grace@example.com",
                name="Grace Hopper",
            )
            session.add_all([actor, member])
            await session.commit()
            organisation_id, membership_id = await _seed_membership(
                session, actor=actor, member=member
            )

        async with session_factory() as session:
            await service.set_membership_status(
                session,
                actor=actor,
                organisation_id=organisation_id,
                membership_id=membership_id,
                status=MembershipStatus.SUSPENDED,
                workos_invitations=FakeWorkOSInvitationsProvider(),
            )
            await session.commit()

        async with session_factory() as session:
            detail = await service.set_membership_status(
                session,
                actor=actor,
                organisation_id=organisation_id,
                membership_id=membership_id,
                status=MembershipStatus.ACTIVE,
                workos_invitations=FakeWorkOSInvitationsProvider(),
            )
            await session.commit()
            assert detail.membership.status == MembershipStatus.ACTIVE

            fresh = await session.get(OrganisationMembership, membership_id)
            assert fresh is not None
            assert fresh.status == MembershipStatus.ACTIVE
            actions = (
                (
                    await session.execute(
                        text(
                            "SELECT action FROM audit_events "
                            "WHERE resource_type = 'membership' AND resource_id = :rid"
                        ).bindparams(rid=str(membership_id))
                    )
                )
                .scalars()
                .all()
            )
            assert actions == ["membership.suspended", "membership.reactivated"]
    finally:
        await engine.dispose()


async def test_removal_deletes_membership_cascades_roles_and_revokes_invitations(
    migrated_database: str,
) -> None:
    """Scope §6.6: removal deletes the row, cascades role grants, revokes invites."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = User(
                workos_user_id=f"admin_{uuid.uuid4().hex[:8]}",
                email="platform@example.com",
                name="Platform Admin",
            )
            member = User(
                workos_user_id=f"member_{uuid.uuid4().hex[:8]}",
                email="grace@example.com",
                name="Grace Hopper",
            )
            session.add_all([actor, member])
            await session.commit()
            organisation_id, membership_id = await _seed_membership(
                session, actor=actor, member=member
            )
            session.add(
                Invitation(
                    organisation_id=organisation_id,
                    email="grace@example.com",
                    role_code="member",
                    workos_invitation_id="inv_workos_remove",
                    invited_by_user_id=actor.id,
                    status=InvitationStatus.SENT,
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                )
            )
            await session.commit()

        async with session_factory() as session:
            await service.assign_role(
                session,
                actor=actor,
                organisation_id=organisation_id,
                membership_id=membership_id,
                role_code="member",
            )
            await session.commit()

        provider = FakeWorkOSInvitationsProvider()
        async with session_factory() as session:
            detail = await service.remove_membership(
                session,
                actor=actor,
                organisation_id=organisation_id,
                membership_id=membership_id,
                workos_invitations=provider,
            )
            await session.commit()
            assert detail.user_email == "grace@example.com"
            assert detail.roles == ["member"]  # the response carries the pre-removal roles

            # The membership row is gone and its role grants cascaded.
            assert await session.get(OrganisationMembership, membership_id) is None
            role_rows = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM membership_roles WHERE membership_id = :mid"
                    ).bindparams(mid=membership_id)
                )
            ).scalar_one()
            assert role_rows == 0

            # The pending invitation was revoked locally and at WorkOS.
            invitation = await session.scalar(
                select(Invitation).where(Invitation.workos_invitation_id == "inv_workos_remove")
            )
            assert invitation is not None
            assert invitation.status == InvitationStatus.REVOKED
            assert provider.revoked == ["inv_workos_remove"]

            actions = (
                (
                    await session.execute(
                        text(
                            "SELECT action FROM audit_events "
                            "WHERE resource_type = 'membership' AND resource_id = :rid"
                        ).bindparams(rid=str(membership_id))
                    )
                )
                .scalars()
                .all()
            )
            assert actions == ["membership.role_changed", "membership.removed"]
    finally:
        await engine.dispose()


async def test_membership_roles_foreign_key_cascades(migrated_database: str) -> None:
    """The membership_roles FK really is ondelete CASCADE at the database."""
    engine, _ = _session_factory(migrated_database)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT confdeltype FROM pg_constraint "
                        "WHERE conname = 'fk_membership_roles_membership_id_organisation_memberships'"
                    )
                )
            ).scalar_one()
            # 'c' = CASCADE (asyncpg returns bytes for the char column)
            assert rows.decode() == "c"
    finally:
        await engine.dispose()
