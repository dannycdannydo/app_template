"""Real-database integration tests for invitations (Scope §6.5).

The fakes in ``test_invitations.py`` prove the request-flow contract but never
execute SQL, so the migrated column shape, the status check constraint, the
unique ``workos_invitation_id`` and the persistence of the invite→accept
journey could silently regress. These tests run the real migration and the
real services against a reachable PostgreSQL, using the same skip pattern as
``test_audit_db.py`` / ``test_workos_org_mapping_db.py``: migrated to head up
front, reverted to base afterwards.
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
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.context_helpers import (
    FakeWorkOSInvitationsProvider,
    FakeWorkOSOrganizationsProvider,
)

from app.core.security import UserProfile, UserProfileClient
from app.modules.invitations import service
from app.modules.invitations.models import InvitationStatus
from app.modules.organisations.models import MembershipStatus, Organisation, OrganisationMembership
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


class _VerifiedProfileClient(UserProfileClient):
    """A profile client reporting one fixed verified email."""

    def __init__(self, email: str) -> None:
        self._email = email

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        return UserProfile(email=self._email, name="Invitee", email_verified=True)


async def test_invitations_table_shape(migrated_database: str) -> None:
    """Scope §6.5: the migrated table matches the model and the design plan."""
    engine, _ = _session_factory(migrated_database)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT column_name, is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'invitations'"
                    )
                )
            ).all()
            constraints = (
                await connection.execute(
                    text(
                        "SELECT conname, contype FROM pg_constraint "
                        "WHERE conrelid = 'invitations'::regclass"
                    )
                )
            ).all()
            indexes = (
                await connection.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'invitations'")
                )
            ).all()
        columns = {row.column_name: row for row in rows}
        required = {
            "id",
            "organisation_id",
            "email",
            "role_code",
            "workos_invitation_id",
            "invited_by_user_id",
            "status",
            "expires_at",
            "created_at",
            "updated_at",
        }
        assert required <= set(columns)
        assert columns["workos_invitation_id"].is_nullable == "YES"
        for name in ("organisation_id", "email", "invited_by_user_id", "expires_at"):
            assert columns[name].is_nullable == "NO"

        named = {row.conname: row.contype.decode() for row in constraints}
        assert named["uq_invitations_workos_invitation_id"] == "u"
        assert named["ck_invitations_invitation_status"] == "c"
        assert named["pk_invitations"] == "p"

        index_names = {row.indexname for row in indexes}
        assert "ix_invitations_organisation_id" in index_names
        assert "ix_invitations_email" in index_names
        assert "ix_invitations_invited_by_user_id" in index_names
    finally:
        await engine.dispose()


async def test_invite_to_accept_journey_round_trips(migrated_database: str) -> None:
    """Acceptance §5.6: invite, then login-time linking, both persist."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            inviter = User(
                workos_user_id=f"inviter_{uuid.uuid4().hex[:8]}",
                email="platform@example.com",
                name="Platform Admin",
            )
            invitee = User(
                workos_user_id=f"invitee_{uuid.uuid4().hex[:8]}",
                email="ada@example.com",
                name="Ada Lovelace",
            )
            session.add_all([inviter, invitee])
            await session.commit()

        async with session_factory() as session:
            organisation = Organisation(name="Journey Ltd")
            session.add(organisation)
            await session.commit()
            organisation_id = organisation.id

        async with session_factory() as session:
            organisation = await session.get(Organisation, organisation_id)
            assert organisation is not None
            # The pre-existing org has no mapping; the lazy backfill runs at
            # first invite and persists with the same transaction.
            invitation = await service.invite_user(
                session,
                actor=inviter,
                organisation_id=organisation_id,
                email="ada@example.com",
                role_code="member",
                workos_invitations=FakeWorkOSInvitationsProvider(),
                workos_organisations=FakeWorkOSOrganizationsProvider(),
            )
            assert organisation.workos_organisation_id is not None
            assert invitation.workos_invitation_id is not None
            assert invitation.status == InvitationStatus.SENT
            await session.commit()

            # No membership row exists before acceptance (acceptance §5.6).
            pre = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM organisation_memberships "
                        "WHERE user_id = :uid AND organisation_id = :oid"
                    ).bindparams(uid=invitee.id, oid=organisation_id)
                )
            ).scalar_one()
            assert pre == 0

        async with session_factory() as session:
            # Login-time linking grants the membership and marks the invite.
            invitee = await session.get(User, invitee.id)
            assert invitee is not None
            accepted = await service.link_invitation_on_login(
                session, invitee, _VerifiedProfileClient("ada@example.com")
            )
            await session.commit()
            assert len(accepted) == 1

            rows = (
                await session.execute(
                    text(
                        "SELECT m.status, r.code FROM organisation_memberships m "
                        "JOIN membership_roles mr ON mr.membership_id = m.id "
                        "JOIN roles r ON r.id = mr.role_id "
                        "WHERE m.user_id = :uid AND m.organisation_id = :oid"
                    ).bindparams(uid=invitee.id, oid=organisation_id)
                )
            ).all()
            assert [(row.status, row.code) for row in rows] == [("active", "member")]

            fresh_invitation = await session.get(type(invitation), invitation.id)
            assert fresh_invitation is not None
            assert fresh_invitation.status == InvitationStatus.ACCEPTED

            audit_actions = (
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
            assert set(audit_actions) == {"invitation.sent", "invitation.accepted"}

        async with session_factory() as session:
            # Idempotence: a second login links nothing new.
            invitee = await session.get(User, invitee.id)
            assert invitee is not None
            accepted_again = await service.link_invitation_on_login(
                session, invitee, _VerifiedProfileClient("ada@example.com")
            )
            await session.commit()
            assert accepted_again == []

            membership_count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM organisation_memberships "
                        "WHERE user_id = :uid AND organisation_id = :oid"
                    ).bindparams(uid=invitee.id, oid=organisation_id)
                )
            ).scalar_one()
            assert membership_count == 1
    finally:
        await engine.dispose()


async def test_membership_unique_constraint_blocks_double_grant(
    migrated_database: str,
) -> None:
    """Scope §6.5: the (user, org) unique constraint is the race guard."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            user = User(
                workos_user_id=f"race_user_{uuid.uuid4().hex[:8]}",
                email="race@example.com",
                name="Race User",
            )
            session.add(user)
            await session.commit()
            user_id = user.id
            organisation = Organisation(name="Race Ltd", workos_organisation_id=None)
            session.add(organisation)
            await session.commit()
            organisation_id = organisation.id

        async with session_factory() as session:
            session.add(
                OrganisationMembership(
                    user_id=user_id,
                    organisation_id=organisation_id,
                    status=MembershipStatus.ACTIVE,
                )
            )
            await session.commit()

            # A second membership for the same pair violates the constraint —
            # exactly what a concurrent double first login hits.
            session.add(
                OrganisationMembership(
                    user_id=user_id,
                    organisation_id=organisation_id,
                    status=MembershipStatus.ACTIVE,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()
