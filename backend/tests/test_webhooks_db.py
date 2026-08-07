"""Real-database integration tests for the webhook consumer (Scope §6.8).

The fakes in ``test_webhooks.py`` prove the request-flow contract but never
execute SQL, so the persistence of the best-effort refreshes (the status flip
on ``invitations``, the deactivation on ``users`` and their audit rows) could
silently regress. These tests run the real migration and the real consumer
service against a reachable PostgreSQL, using the same skip pattern as
``test_audit_db.py`` / ``test_invitations_db.py``: migrated to head up front,
reverted to base afterwards.
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.audit.models import AuditEvent
from app.modules.audit.service import ACTION_INVITATION_REVOKED, ACTION_USER_DEACTIVATED
from app.modules.invitations.models import Invitation, InvitationStatus
from app.modules.organisations.models import Organisation
from app.modules.users.models import User
from app.modules.webhooks.schemas import WorkOSWebhookEvent
from app.modules.webhooks.service import process_webhook_event

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


async def test_invitation_revoked_webhook_persists_status_and_audit(
    migrated_database: str,
) -> None:
    """A revoked delivery flips the row, commits, and writes the audit trail."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            inviter = User(
                workos_user_id=f"inviter_{uuid.uuid4().hex[:8]}",
                email="platform@example.com",
                name="Platform Admin",
            )
            session.add(inviter)
            await session.commit()
            inviter_id = inviter.id
            organisation = Organisation(name="Webhook Ltd")
            session.add(organisation)
            await session.commit()
            organisation_id = organisation.id
            invitation = Invitation(
                organisation_id=organisation_id,
                email="ada@example.com",
                role_code="member",
                workos_invitation_id="inv_workos_wh_1",
                invited_by_user_id=inviter_id,
                status=InvitationStatus.SENT,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
            session.add(invitation)
            await session.commit()
            invitation_id = invitation.id

        async with session_factory() as session:
            event = WorkOSWebhookEvent(
                id="evt_revoke_wh",
                event="invitation.revoked",
                data={"id": "inv_workos_wh_1", "state": "revoked"},
            )
            changed = await process_webhook_event(session, event)
            assert changed is True

        async with session_factory() as session:
            row = await session.get(Invitation, invitation_id)
            assert row is not None
            assert row.status == InvitationStatus.REVOKED
            audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.resource_id == str(invitation_id))
            )
            assert audit is not None
            assert audit.action == ACTION_INVITATION_REVOKED
            assert audit.actor_user_id is None
            assert audit.event_metadata["source"] == "webhook"
    finally:
        await engine.dispose()


async def test_user_deleted_webhook_persists_deactivation_and_audit(
    migrated_database: str,
) -> None:
    """A deleted-user delivery deactivates the row, commits, and audits it."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            user = User(
                workos_user_id="user_wh_deleted",
                email="ada@example.com",
                name="Ada Lovelace",
            )
            session.add(user)
            await session.commit()
            user_id = user.id

        async with session_factory() as session:
            event = WorkOSWebhookEvent(
                id="evt_user_wh",
                event="user.deleted",
                data={"id": "user_wh_deleted", "email": "ada@example.com"},
            )
            changed = await process_webhook_event(session, event)
            assert changed is True

        async with session_factory() as session:
            row = await session.get(User, user_id)
            assert row is not None
            assert row.is_active is False
            audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.resource_id == str(user_id))
            )
            assert audit is not None
            assert audit.action == ACTION_USER_DEACTIVATED
            assert audit.actor_user_id is None
    finally:
        await engine.dispose()


async def test_unknown_event_persists_nothing(migrated_database: str) -> None:
    """Unknown event types are tolerated and leave no audit rows behind."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            before = (await session.execute(text("SELECT count(*) FROM audit_events"))).scalar_one()
            event = WorkOSWebhookEvent(id="evt_unknown", event="some.future.event", data={})
            changed = await process_webhook_event(session, event)
            assert changed is False
            after = (await session.execute(text("SELECT count(*) FROM audit_events"))).scalar_one()
            assert after == before  # nothing was written
    finally:
        await engine.dispose()
