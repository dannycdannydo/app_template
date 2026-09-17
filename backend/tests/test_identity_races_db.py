"""Real-database, two-session race tests for plan P7.

The fakes prove the request-flow contract but never exercise PostgreSQL row or
advisory locking, so the exact races plan P7 closes — accept-vs-revoke,
duplicate first login, concurrent platform-admin removals and a provider
deactivation that removes the last enabled administrator — could silently
regress. These tests run the real migration and the real services against a
reachable PostgreSQL, using the same skip pattern as ``test_invitations_db.py``:
migrated to head up front, reverted to base afterwards.

Every race is deterministic: a blocking transaction is asserted to still be
pending before the other side commits, so a green result is not timing-luck.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.context_helpers import FakeWorkOSInvitationsProvider

from app.core.exceptions import BadRequestError, ConflictError
from app.core.security import UserProfile, UserProfileClient
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import (
    ACTION_PLATFORM_ADMIN_LOCKOUT,
    ACTION_PLATFORM_ADMIN_RECOVERY_GRANTED,
    ACTION_PLATFORM_ADMIN_REVOKED,
)
from app.modules.invitations import service as invitations_service
from app.modules.invitations.models import Invitation, InvitationStatus
from app.modules.organisations.models import Organisation, OrganisationMembership
from app.modules.permissions.constants import PLATFORM_ADMIN_ROLE_CODE
from app.modules.platform_admin import service as platform_admin_service
from app.modules.platform_admin.models import PlatformMembership, PlatformRole
from app.modules.platform_admin.queries import acquire_platform_admin_lock
from app.modules.users.models import User
from app.modules.webhooks.schemas import WorkOSWebhookEvent
from app.modules.webhooks.service import process_webhook_event

BACKEND_ROOT = Path(__file__).resolve().parents[1]

BLOCKED_TIMEOUT_SECONDS = 0.25


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
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
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


def _unique() -> str:
    return uuid.uuid4().hex[:10]


async def _seed_user(session: AsyncSession, *, label: str, is_active: bool = True) -> User:
    unique = _unique()
    user = User(
        workos_user_id=f"{label}_{unique}",
        email=f"{label}_{unique}@example.com",
        name=f"{label.title()} User",
        is_active=is_active,
    )
    session.add(user)
    await session.commit()
    return user


async def _seed_invitation(
    session: AsyncSession, *, invitee: User, role_code: str = "member"
) -> tuple[Organisation, Invitation]:
    organisation = Organisation(name=f"Race Org {_unique()}")
    session.add(organisation)
    await session.flush()
    invitation = Invitation(
        organisation_id=organisation.id,
        email=invitee.email,
        role_code=role_code,
        workos_invitation_id=f"inv_{_unique()}",
        invited_by_user_id=invitee.id,
        status=InvitationStatus.SENT,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    session.add(invitation)
    await session.commit()
    return organisation, invitation


async def _accept_with_session(
    session: AsyncSession, *, user_id: uuid.UUID, email: str
) -> list[Invitation]:
    user = await session.get(User, user_id)
    assert user is not None
    return await invitations_service.link_invitation_on_login(
        session, user, _VerifiedProfileClient(email)
    )


async def _accept(
    session_factory: async_sessionmaker[AsyncSession], *, user_id: uuid.UUID, email: str
) -> list[Invitation]:
    async with session_factory() as session:
        return await _accept_with_session(session, user_id=user_id, email=email)


async def _revoke(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: uuid.UUID,
    org_id: uuid.UUID,
    invitation_id: uuid.UUID,
) -> Invitation:
    async with session_factory() as session:
        actor = await session.get(User, actor_id)
        assert actor is not None
        return await invitations_service.revoke_invitation(
            session,
            actor,
            organisation_id=org_id,
            invitation_id=invitation_id,
            workos=FakeWorkOSInvitationsProvider(),
        )


async def _webhook(
    session_factory: async_sessionmaker[AsyncSession], event: WorkOSWebhookEvent
) -> bool:
    async with session_factory() as session:
        return await process_webhook_event(session, event)


async def _delete_provisioned_user(
    session_factory: async_sessionmaker[AsyncSession], *, user_id: uuid.UUID
) -> bool:
    async with session_factory() as session:
        return await platform_admin_service.delete_provisioned_user(
            session, user_id=user_id, source="bootstrap_teardown"
        )


class _RollbackHookSession:
    """Wrap a real session, failing the first commit then running a rollback hook.

    Exercises the login-time acceptance retry deterministically: the first
    commit is made to look like a lost unique-constraint race, and ``hook``
    runs immediately after the rollback that follows it — the exact window in
    which a revoke or webhook can commit between the failed attempt and the
    retry.
    """

    def __init__(self, session: AsyncSession, hook: Callable[[], Awaitable[None]]) -> None:
        self._session = session
        self._hook: Callable[[], Awaitable[None]] | None = hook
        self._commits = 0

    async def commit(self) -> None:
        self._commits += 1
        if self._commits == 1:
            raise IntegrityError("INSERT", {}, Exception("duplicate key value"))
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
        if self._hook is not None:
            hook, self._hook = self._hook, None
            await hook()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


async def test_accept_vs_revoke_serialises_on_the_invitation_row(
    migrated_database: str,
) -> None:
    """A committed acceptance cannot be overwritten by a concurrent revoke.

    Session A holds the invitation row lock (as an in-flight acceptance does),
    the real revoke service blocks on that lock, A commits the acceptance, and
    the revoke then observes a terminal row and refuses.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            invitee = await _seed_user(session, label="accept_invitee")
            inviter = await _seed_user(session, label="accept_inviter")
            organisation, invitation = await _seed_invitation(session, invitee=invitee)

        session_a = session_factory()
        locked = await session_a.scalar(
            select(Invitation).where(Invitation.id == invitation.id).with_for_update()
        )
        assert locked is not None and locked.status is InvitationStatus.SENT

        revoke_task = asyncio.create_task(
            _revoke(
                session_factory,
                actor_id=inviter.id,
                org_id=organisation.id,
                invitation_id=invitation.id,
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(revoke_task), timeout=BLOCKED_TIMEOUT_SECONDS)

        accepted = await _accept_with_session(session_a, user_id=invitee.id, email=invitee.email)
        assert len(accepted) == 1
        await session_a.close()

        with pytest.raises(ConflictError) as exc_info:
            await revoke_task
        assert exc_info.value.code == "invitation_not_revocable"

        async with session_factory() as session:
            fresh = await session.get(Invitation, invitation.id)
            assert fresh is not None
            assert fresh.status is InvitationStatus.ACCEPTED
    finally:
        await engine.dispose()


async def test_acceptance_never_grants_after_a_committed_revoke(
    migrated_database: str,
) -> None:
    """A login-time acceptance blocked behind a revoke grants nothing.

    Session A holds the row lock and commits ``revoked``; the real acceptance
    service was blocked on the same row and, once unblocked, observes no
    grantable invitation and creates no membership.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            invitee = await _seed_user(session, label="revoke_invitee")
            organisation, invitation = await _seed_invitation(session, invitee=invitee)

        session_a = session_factory()
        locked = await session_a.scalar(
            select(Invitation).where(Invitation.id == invitation.id).with_for_update()
        )
        assert locked is not None

        accept_task = asyncio.create_task(
            _accept(session_factory, user_id=invitee.id, email=invitee.email)
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(accept_task), timeout=BLOCKED_TIMEOUT_SECONDS)

        locked.status = InvitationStatus.REVOKED
        await session_a.commit()
        await session_a.close()

        accepted = await accept_task
        assert accepted == []

        async with session_factory() as session:
            fresh = await session.get(Invitation, invitation.id)
            assert fresh is not None and fresh.status is InvitationStatus.REVOKED
            membership_count = await session.scalar(
                select(func.count())
                .select_from(OrganisationMembership)
                .where(
                    OrganisationMembership.user_id == invitee.id,
                    OrganisationMembership.organisation_id == organisation.id,
                )
            )
            assert membership_count == 0
    finally:
        await engine.dispose()


async def test_duplicate_first_login_grants_one_membership(
    migrated_database: str,
) -> None:
    """Two concurrent logins for one invitation create exactly one membership."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            invitee = await _seed_user(session, label="dupe_invitee")
            _organisation, invitation = await _seed_invitation(session, invitee=invitee)

        first, second = await asyncio.gather(
            _accept(session_factory, user_id=invitee.id, email=invitee.email),
            _accept(session_factory, user_id=invitee.id, email=invitee.email),
        )
        assert sorted([len(first), len(second)]) == [0, 1]

        async with session_factory() as session:
            membership_count = await session.scalar(
                select(func.count())
                .select_from(OrganisationMembership)
                .where(
                    OrganisationMembership.user_id == invitee.id,
                    OrganisationMembership.organisation_id == invitation.organisation_id,
                )
            )
            assert membership_count == 1
            fresh = await session.get(Invitation, invitation.id)
            assert fresh is not None and fresh.status is InvitationStatus.ACCEPTED
    finally:
        await engine.dispose()


async def _platform_admin_role(session: AsyncSession) -> PlatformRole:
    role = await session.scalar(
        select(PlatformRole).where(PlatformRole.code == PLATFORM_ADMIN_ROLE_CODE)
    )
    assert role is not None, "the platform_admin role seed is missing"
    return role


async def _clear_platform_admins(session: AsyncSession) -> None:
    """Remove every platform membership so a race test starts from a known set.

    Tests in this module share one migrated database; the admin-invariant tests
    each need a clean plane so an administrator seeded by an earlier test can
    never change the expected active count.
    """
    await session.execute(delete(PlatformMembership))
    await session.commit()


async def _seed_platform_admin(
    session: AsyncSession, *, label: str, is_active: bool = True
) -> tuple[User, PlatformMembership]:
    role = await _platform_admin_role(session)
    user = await _seed_user(session, label=label, is_active=is_active)
    membership = PlatformMembership(user_id=user.id, platform_role_id=role.id)
    session.add(membership)
    await session.commit()
    return user, membership


async def test_concurrent_admin_revocation_preserves_one_active_admin(
    migrated_database: str,
) -> None:
    """Two concurrent revocations cannot each observe the other admin."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            await _clear_platform_admins(session)
            actor = await _seed_user(session, label="revoke_actor")
            _admin_a, membership_a = await _seed_platform_admin(session, label="admin_a")
            _admin_b, membership_b = await _seed_platform_admin(session, label="admin_b")

        async def _revoke_one(membership_id: uuid.UUID) -> str:
            async with session_factory() as session:
                revoked_actor = await session.get(User, actor.id)
                assert revoked_actor is not None
                try:
                    await platform_admin_service.revoke_platform_admin(
                        session, actor=revoked_actor, platform_membership_id=membership_id
                    )
                    return "revoked"
                except BadRequestError as exc:
                    assert exc.code == "last_platform_admin"
                    return "rejected"

        outcomes = await asyncio.gather(_revoke_one(membership_a.id), _revoke_one(membership_b.id))
        assert sorted(outcomes) == ["rejected", "revoked"]

        async with session_factory() as session:
            role = await _platform_admin_role(session)
            remaining = await platform_admin_service.count_active_platform_admins(
                session, role_id=role.id
            )
        assert remaining == 1
    finally:
        await engine.dispose()


async def test_revoke_inactive_membership_is_allowed(
    migrated_database: str,
) -> None:
    """An inactive member is not a recovery principal, so the revoke proceeds."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            await _clear_platform_admins(session)
            actor = await _seed_user(session, label="inactive_actor")
            _disabled, membership = await _seed_platform_admin(
                session, label="inactive_admin", is_active=False
            )

        async with session_factory() as session:
            revoked_actor = await session.get(User, actor.id)
            assert revoked_actor is not None
            detail = await platform_admin_service.revoke_platform_admin(
                session, actor=revoked_actor, platform_membership_id=membership.id
            )
            assert detail.membership.id == membership.id

            audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.action == ACTION_PLATFORM_ADMIN_REVOKED)
            )
            assert audit is not None
    finally:
        await engine.dispose()


async def test_webhook_deactivation_of_last_admin_records_lockout(
    migrated_database: str,
) -> None:
    """A provider deletion of the last enabled admin audits the lockout state."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            await _clear_platform_admins(session)
            admin, _membership = await _seed_platform_admin(session, label="lockout_admin")
            admin_workos_id = admin.workos_user_id
            admin_id = admin.id

        async with session_factory() as session:
            changed = await process_webhook_event(
                session,
                WorkOSWebhookEvent(
                    id=f"evt_{_unique()}",
                    event="user.deleted",
                    data={"id": admin_workos_id},
                ),
            )
            assert changed is True

        async with session_factory() as session:
            fresh = await session.get(User, admin_id)
            assert fresh is not None and fresh.is_active is False
            lockout = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == ACTION_PLATFORM_ADMIN_LOCKOUT,
                    AuditEvent.resource_id == str(admin_id),
                )
            )
            assert lockout is not None
            assert lockout.actor_user_id is None
    finally:
        await engine.dispose()


async def test_recover_platform_admin_only_when_locked_out(
    migrated_database: str,
) -> None:
    """Break-glass grants only a locked-out plane, and audits it."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            await _clear_platform_admins(session)
            target = await _seed_user(session, label="recover_target")
            target_id = target.id
            target_email = target.email

        async with session_factory() as session:
            recovered = await platform_admin_service.recover_platform_admin(
                session,
                email=target_email,
                reason="test break-glass recovery",
            )
            assert recovered.id == target_id

        async with session_factory() as session:
            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == ACTION_PLATFORM_ADMIN_RECOVERY_GRANTED,
                    AuditEvent.resource_id == str(target_id),
                )
            )
            assert audit is not None
            assert audit.actor_user_id is None
            assert audit.event_metadata["source"] == "break_glass"
            assert audit.event_metadata["reason"] == "test break-glass recovery"

        # With an active admin now present, the break-glass path refuses.
        async with session_factory() as session:
            with pytest.raises(ConflictError) as exc_info:
                await platform_admin_service.recover_platform_admin(
                    session,
                    email=target_email,
                    reason="second attempt",
                )
            assert exc_info.value.code == "platform_admin_not_locked_out"
    finally:
        await engine.dispose()


async def test_recover_platform_admin_rejects_blank_reason(
    migrated_database: str,
) -> None:
    """A blank break-glass reason is refused so the required audit is meaningful."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            await _clear_platform_admins(session)
            target = await _seed_user(session, label="blank_reason_target")
            target_email = target.email

        async with session_factory() as session:
            with pytest.raises(BadRequestError) as exc_info:
                await platform_admin_service.recover_platform_admin(
                    session, email=target_email, reason="   "
                )
            assert exc_info.value.code == "recovery_reason_required"
    finally:
        await engine.dispose()


async def test_webhook_revoke_blocks_on_invitation_lock_and_acceptance_wins(
    migrated_database: str,
) -> None:
    """The actual invitation.revoked webhook waits on the acceptance row lock.

    Session A holds the invitation row ``FOR UPDATE`` (as an in-flight
    acceptance does); the real webhook consumer blocks on that lock, the
    acceptance commits terminal, and the webhook then observes a terminal row
    and no-ops instead of overwriting it.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            invitee = await _seed_user(session, label="webhook_accept_invitee")
            _organisation, invitation = await _seed_invitation(session, invitee=invitee)

        session_a = session_factory()
        locked = await session_a.scalar(
            select(Invitation).where(Invitation.id == invitation.id).with_for_update()
        )
        assert locked is not None and locked.status is InvitationStatus.SENT

        webhook_task = asyncio.create_task(
            _webhook(
                session_factory,
                WorkOSWebhookEvent(
                    id=f"evt_{_unique()}",
                    event="invitation.revoked",
                    data={"id": invitation.workos_invitation_id},
                ),
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(webhook_task), timeout=BLOCKED_TIMEOUT_SECONDS)

        accepted = await _accept_with_session(session_a, user_id=invitee.id, email=invitee.email)
        assert len(accepted) == 1
        await session_a.close()

        assert await webhook_task is False

        async with session_factory() as session:
            fresh = await session.get(Invitation, invitation.id)
            assert fresh is not None and fresh.status is InvitationStatus.ACCEPTED
    finally:
        await engine.dispose()


async def test_acceptance_never_grants_after_committed_webhook_revoke(
    migrated_database: str,
) -> None:
    """The real webhook commits a revoke and a blocked acceptance grants nothing."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            invitee = await _seed_user(session, label="webhook_revoke_invitee")
            organisation, invitation = await _seed_invitation(session, invitee=invitee)

        session_a = session_factory()
        locked = await session_a.scalar(
            select(Invitation).where(Invitation.id == invitation.id).with_for_update()
        )
        assert locked is not None

        accept_task = asyncio.create_task(
            _accept(session_factory, user_id=invitee.id, email=invitee.email)
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(accept_task), timeout=BLOCKED_TIMEOUT_SECONDS)

        changed = await process_webhook_event(
            session_a,
            WorkOSWebhookEvent(
                id=f"evt_{_unique()}",
                event="invitation.revoked",
                data={"id": invitation.workos_invitation_id},
            ),
        )
        assert changed is True
        await session_a.close()

        accepted = await accept_task
        assert accepted == []

        async with session_factory() as session:
            fresh = await session.get(Invitation, invitation.id)
            assert fresh is not None and fresh.status is InvitationStatus.REVOKED
            membership_count = await session.scalar(
                select(func.count())
                .select_from(OrganisationMembership)
                .where(
                    OrganisationMembership.user_id == invitee.id,
                    OrganisationMembership.organisation_id == organisation.id,
                )
            )
            assert membership_count == 0
    finally:
        await engine.dispose()


async def test_retry_reacquires_lock_and_honours_webhook_after_rollback(
    migrated_database: str,
) -> None:
    """The post-rollback retry re-reads state instead of reusing stale rows.

    The first acceptance commit is made to lose a unique-constraint race; a
    real ``invitation.revoked`` webhook then commits in the window between the
    rollback and the retry. The retry must re-run the locked pending-invitation
    query, observe the committed revocation and grant nothing.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            invitee = await _seed_user(session, label="retry_invitee")
            organisation, invitation = await _seed_invitation(session, invitee=invitee)
            invitee_id = invitee.id
            invitee_email = invitee.email
            invitation_id = invitation.id
            workos_invitation_id = invitation.workos_invitation_id

        injected = False

        async def hook() -> None:
            nonlocal injected
            injected = True
            changed = await _webhook(
                session_factory,
                WorkOSWebhookEvent(
                    id=f"evt_{_unique()}",
                    event="invitation.revoked",
                    data={"id": workos_invitation_id},
                ),
            )
            assert changed is True

        async with session_factory() as session:
            user = await session.get(User, invitee_id)
            assert user is not None
            proxy = _RollbackHookSession(session, hook)
            accepted = await invitations_service.link_invitation_on_login(
                cast(AsyncSession, proxy),
                user,
                _VerifiedProfileClient(invitee_email),
            )
        assert accepted == []
        assert injected is True

        async with session_factory() as session:
            fresh = await session.get(Invitation, invitation_id)
            assert fresh is not None and fresh.status is InvitationStatus.REVOKED
            membership_count = await session.scalar(
                select(func.count())
                .select_from(OrganisationMembership)
                .where(
                    OrganisationMembership.user_id == invitee_id,
                    OrganisationMembership.organisation_id == organisation.id,
                )
            )
            assert membership_count == 0
    finally:
        await engine.dispose()


async def test_user_deleted_webhook_blocks_on_platform_admin_lock(
    migrated_database: str,
) -> None:
    """The actual user.deleted webhook contends on the platform-admin advisory lock."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            await _clear_platform_admins(session)
            admin, _membership = await _seed_platform_admin(session, label="locked_deactivate")
            admin_id = admin.id
            admin_workos_id = admin.workos_user_id

        holder = session_factory()
        await acquire_platform_admin_lock(holder)

        webhook_task = asyncio.create_task(
            _webhook(
                session_factory,
                WorkOSWebhookEvent(
                    id=f"evt_{_unique()}",
                    event="user.deleted",
                    data={"id": admin_workos_id},
                ),
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(webhook_task), timeout=BLOCKED_TIMEOUT_SECONDS)

        await holder.commit()
        await holder.close()

        assert await webhook_task is True

        async with session_factory() as session:
            fresh = await session.get(User, admin_id)
            assert fresh is not None and fresh.is_active is False
            lockout = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == ACTION_PLATFORM_ADMIN_LOCKOUT,
                    AuditEvent.resource_id == str(admin_id),
                )
            )
            assert lockout is not None
    finally:
        await engine.dispose()


async def test_webhook_deactivation_races_admin_removal(
    migrated_database: str,
) -> None:
    """A concurrent provider deactivation and admin removal settle safely.

    Both paths contend on the platform-admin advisory lock, so they cannot each
    observe the other admin and both remove one. Whichever commits first, the
    surviving state is either the remaining enabled administrator (the removal
    refused) or a recorded lockout (the provider deactivation is unavoidable and
    audited).
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            await _clear_platform_admins(session)
            actor = await _seed_user(session, label="race_actor")
            webhook_admin, _webhook_membership = await _seed_platform_admin(
                session, label="race_webhook_admin"
            )
            _removed_admin, removed_membership = await _seed_platform_admin(
                session, label="race_removed_admin"
            )
            webhook_admin_id = webhook_admin.id
            webhook_admin_workos_id = webhook_admin.workos_user_id
            actor_id = actor.id
            removed_membership_id = removed_membership.id

        async def _deactivate() -> bool:
            return await _webhook(
                session_factory,
                WorkOSWebhookEvent(
                    id=f"evt_{_unique()}",
                    event="user.deleted",
                    data={"id": webhook_admin_workos_id},
                ),
            )

        async def _remove() -> str:
            async with session_factory() as session:
                removed_actor = await session.get(User, actor_id)
                assert removed_actor is not None
                try:
                    await platform_admin_service.revoke_platform_admin(
                        session,
                        actor=removed_actor,
                        platform_membership_id=removed_membership_id,
                    )
                    return "revoked"
                except BadRequestError as exc:
                    assert exc.code == "last_platform_admin"
                    return "rejected"

        deactivated, removal = await asyncio.gather(_deactivate(), _remove())
        assert deactivated is True

        async with session_factory() as session:
            role = await _platform_admin_role(session)
            remaining = await platform_admin_service.count_active_platform_admins(
                session, role_id=role.id
            )
            fresh = await session.get(User, webhook_admin_id)
            assert fresh is not None and fresh.is_active is False
            if removal == "revoked":
                # The removal won the lock first; the unavoidable provider
                # deactivation then emptied the plane and must have been audited.
                assert remaining == 0
                lockout = await session.scalar(
                    select(AuditEvent).where(
                        AuditEvent.action == ACTION_PLATFORM_ADMIN_LOCKOUT,
                        AuditEvent.resource_id == str(webhook_admin_id),
                    )
                )
                assert lockout is not None
            else:
                # The deactivation won; the removal observed one remaining admin
                # and refused, so an enabled administrator survives.
                assert removal == "rejected"
                assert remaining == 1
    finally:
        await engine.dispose()


async def test_delete_provisioned_user_blocks_on_platform_admin_lock(
    migrated_database: str,
) -> None:
    """The teardown deletion contends on the platform-admin advisory lock."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            await _clear_platform_admins(session)
            admin, _membership = await _seed_platform_admin(session, label="locked_teardown")
            admin_id = admin.id

        holder = session_factory()
        await acquire_platform_admin_lock(holder)

        delete_task = asyncio.create_task(
            _delete_provisioned_user(session_factory, user_id=admin_id)
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(delete_task), timeout=BLOCKED_TIMEOUT_SECONDS)

        await holder.commit()
        await holder.close()

        assert await delete_task is True

        async with session_factory() as session:
            assert await session.get(User, admin_id) is None
    finally:
        await engine.dispose()


async def test_delete_provisioned_user_records_lockout_for_last_admin(
    migrated_database: str,
) -> None:
    """A teardown that removes the last enabled admin records the lockout audit."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            await _clear_platform_admins(session)
            admin, _membership = await _seed_platform_admin(session, label="teardown_last")
            admin_id = admin.id

        assert await _delete_provisioned_user(session_factory, user_id=admin_id) is True

        async with session_factory() as session:
            assert await session.get(User, admin_id) is None
            lockout = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == ACTION_PLATFORM_ADMIN_LOCKOUT,
                    AuditEvent.resource_id == str(admin_id),
                )
            )
            assert lockout is not None
            assert lockout.actor_user_id is None
            assert lockout.event_metadata["source"] == "bootstrap_teardown"
    finally:
        await engine.dispose()
