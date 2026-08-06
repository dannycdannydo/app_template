"""Real-database integration tests for the platform bootstrap (Scope §6.4).

The fakes in ``test_bootstrap.py`` prove the request-flow contract but never
execute SQL, so the migrated table shape, the sentinel constraint and the
atomicity of the grant could silently regress. These tests run the real
migration and the real service against a reachable PostgreSQL, using the same
skip pattern as ``test_audit_db.py``: migrated to head up front, reverted to
base afterwards. The WorkOS profile is stubbed because only the database and
the service are under test here.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.security import UserProfile, UserProfileClient
from app.modules.audit.models import AuditEvent
from app.modules.platform_admin import service
from app.modules.platform_admin.models import (
    BOOTSTRAP_SINGLETON_ID,
    BootstrapState,
    PlatformMembership,
)
from app.modules.users.models import User

BACKEND_ROOT = Path(__file__).resolve().parents[1]

BOOTSTRAP_EMAIL = "admin@example.com"


class StubProfileClient(UserProfileClient):
    """Returns a fixed profile so the grant path never touches WorkOS."""

    def __init__(self, *, email: str, email_verified: bool) -> None:
        self._email = email
        self._email_verified = email_verified

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        return UserProfile(
            email=self._email,
            name="Bootstrap Admin",
            email_verified=self._email_verified,
        )


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


async def _seed_actor(session: AsyncSession) -> User:
    """Seed one unique user the bootstrap can be granted to."""
    unique = uuid.uuid4().hex[:8]
    user = User(
        workos_user_id=f"bootstrap_db_user_{unique}",
        email=f"bootstrap_{unique}@example.com",
        name="Bootstrap User",
    )
    session.add(user)
    await session.commit()
    return user


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: SimpleNamespace(bootstrap_platform_admin_email=BOOTSTRAP_EMAIL),
    )


async def _grant_counts(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> tuple[int, int, int]:
    """Return (memberships, bootstrap rows, audit rows) for one user's grant.

    Every count is scoped to the user so tests stay deterministic even though
    the module-scoped migration fixture leaves earlier tests' rows in the
    shared database (the same technique as ``test_audit_db.py``).
    """
    async with session_factory() as session:
        memberships = await session.scalar(
            select(func.count())
            .select_from(PlatformMembership)
            .where(PlatformMembership.user_id == user_id)
        )
        bootstrap_rows = await session.scalar(
            select(func.count())
            .select_from(BootstrapState)
            .where(BootstrapState.consumed_by_user_id == user_id)
        )
        audit_rows = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == service.ACTION_PLATFORM_BOOTSTRAP_GRANTED,
                AuditEvent.actor_user_id == user_id,
            )
        )
        return memberships or 0, bootstrap_rows or 0, audit_rows or 0


async def _clean_bootstrap(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Remove leftover bootstrap rows so the sentinel is free for this test."""
    async with session_factory() as session:
        await session.execute(delete(BootstrapState))
        await session.commit()


async def test_bootstrap_grant_writes_membership_state_and_audit(
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope §6.4: the grant persists all three rows atomically, exactly once."""
    _configured(monkeypatch)
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            actor_id = actor.id

            membership = await service.maybe_grant_bootstrap_platform_admin(
                session,
                actor,
                StubProfileClient(email=BOOTSTRAP_EMAIL, email_verified=True),
            )
            assert membership is not None

            memberships, bootstrap_rows, audit_rows = await _grant_counts(session_factory, actor_id)
            assert memberships == 1
            assert bootstrap_rows == 1
            assert audit_rows == 1

            # A second login is a no-op: no new rows anywhere.
            again = await service.maybe_grant_bootstrap_platform_admin(
                session,
                actor,
                StubProfileClient(email=BOOTSTRAP_EMAIL, email_verified=True),
            )
            assert again is None
            memberships, bootstrap_rows, audit_rows = await _grant_counts(session_factory, actor_id)
            assert memberships == 1
            assert bootstrap_rows == 1
            assert audit_rows == 1
    finally:
        await engine.dispose()


async def test_bootstrap_table_shape_and_sentinel_constraint(
    migrated_database: str,
) -> None:
    """Scope §6.4: the single-row sentinel is enforced by the database itself."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        await _clean_bootstrap(session_factory)  # free the sentinel for this test
        async with session_factory() as session:
            actor = await _seed_actor(session)
            session.add(
                BootstrapState(
                    id=BOOTSTRAP_SINGLETON_ID,
                    email=BOOTSTRAP_EMAIL,
                    consumed_by_user_id=actor.id,
                )
            )
            await session.commit()

            # A second row with the sentinel id violates the primary key.
            session.add(
                BootstrapState(
                    id=BOOTSTRAP_SINGLETON_ID,
                    email=BOOTSTRAP_EMAIL,
                    consumed_by_user_id=actor.id,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


async def test_bootstrap_wrong_email_never_grants(
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance §5.5: a different WorkOS email leaves the database untouched."""
    _configured(monkeypatch)
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            membership = await service.maybe_grant_bootstrap_platform_admin(
                session,
                actor,
                StubProfileClient(email="other@example.com", email_verified=True),
            )
            assert membership is None
            memberships, bootstrap_rows, audit_rows = await _grant_counts(session_factory, actor.id)
            assert (memberships, bootstrap_rows, audit_rows) == (0, 0, 0)
    finally:
        await engine.dispose()


async def test_bootstrap_unverified_email_never_grants(
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance §5.5: email_verified is required even for the right email."""
    _configured(monkeypatch)
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            actor = await _seed_actor(session)
            membership = await service.maybe_grant_bootstrap_platform_admin(
                session,
                actor,
                StubProfileClient(email=BOOTSTRAP_EMAIL, email_verified=False),
            )
            assert membership is None
            memberships, bootstrap_rows, audit_rows = await _grant_counts(session_factory, actor.id)
            assert (memberships, bootstrap_rows, audit_rows) == (0, 0, 0)
    finally:
        await engine.dispose()


async def test_concurrent_first_logins_grant_exactly_once(
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance §5.5: a concurrent double first-login cannot double-grant.

    Two sessions run the hook concurrently over the same seeded user. Both
    may read the unconsumed bootstrap before either commits; the sentinel
    primary key then forces exactly one transaction to win and the loser to
    recover through the IntegrityError path. Whichever interleaving happens,
    exactly one membership, one bootstrap row and one audit row survive.
    """
    _configured(monkeypatch)
    engine, session_factory = _session_factory(migrated_database)
    try:
        await _clean_bootstrap(session_factory)  # no leftover sentinel row to lose to
        async with session_factory() as session:
            actor = await _seed_actor(session)
            actor_id = actor.id

        async def _race(session_factory: async_sessionmaker[AsyncSession]) -> object:
            async with session_factory() as session:
                fresh = await session.get(User, actor_id)
                assert fresh is not None
                return await service.maybe_grant_bootstrap_platform_admin(
                    session,
                    fresh,
                    StubProfileClient(email=BOOTSTRAP_EMAIL, email_verified=True),
                )

        results = await asyncio.gather(_race(session_factory), _race(session_factory))
        assert sum(membership is not None for membership in results) == 1

        memberships, bootstrap_rows, audit_rows = await _grant_counts(session_factory, actor_id)
        assert (memberships, bootstrap_rows, audit_rows) == (1, 1, 1)
    finally:
        await engine.dispose()
