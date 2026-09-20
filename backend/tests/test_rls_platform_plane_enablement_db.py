"""Real-PostgreSQL production enablement suite for the platform-only plane (P4, 7).

Plan P4 / ``docs/rls-rollout.md`` §3 group 7. It proves the production
enablement migration (``b4c5d6e7f8a9``) that promotes the platform-only
authorisation plane — ``platform_roles``, ``platform_role_permissions``,
``platform_memberships`` and ``bootstrap_states`` — into the permanent policy
chain, and the narrow transaction-local contexts the platform-plane access paths
bind:

- the group policies are installed with matching ``USING``/``WITH CHECK`` and
  RLS is enabled **and forced** on all four tables, so a tenant-plane query with
  a missed predicate reads no platform row;
- the platform catalogue (``platform_roles``/``platform_role_permissions``) is
  readable to the runtime role and not writable by it;
- without context ``platform_memberships`` returns nothing, a self-bound user
  reads only their own membership and can never insert, update or delete one,
  and the validated platform context (or the trusted service context) admits the
  cross-user platform administration;
- the identity-bearing bootstrap singleton is context-gated for read, insert
  and delete, is never UPDATE-able, and its table grants are narrowed;
- the validated platform context grants **no** ordinary tenant-row access, so
  platform status alone is not a tenant bypass;
- the transaction-local service context does not survive commit or
  pooled-connection reuse;
- ``require_platform_permission`` resolves the caller's own platform membership
  under the pre-authorisation user context, then binds the platform context, and
  denies a user with no platform membership;
- the one-time bootstrap grant, the operator recovery CLI and the cross-tenant
  teardown bind the service context so the platform-only writes and cross-user
  reads work on the restricted runtime role without a bypass; and
- the migration upgrades, downgrades one revision and re-upgrades cleanly.

The suite runs against real PostgreSQL with the restricted runtime login (the
migrations create the roles ``NOLOGIN``; the test grants a throwaway credential).
``migrated_database`` reverts to base at teardown, so the rollout leaves no
residue.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alembic import command
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.rls_helpers import (
    alembic_config,
    database_reachable,
    downgrade_to_base,
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_platform_plane,
    seed_two_organisation_records,
    upgrade_to_head,
)

from app.api.dependencies import require_platform_permission
from app.core.exceptions import PermissionDenied
from app.core.security import UserProfile, UserProfileClient
from app.db.rls import (
    bind_organisation_context,
    bind_platform_context,
    bind_platform_service_context,
    bind_user_context,
    bound_platform_context,
    bound_platform_service_context,
)
from app.modules.platform_admin import service as platform_admin_service
from app.modules.users.models import User
from app.modules.webhooks.schemas import WorkOSWebhookEvent
from app.modules.webhooks.service import process_webhook_event

#: The group 6 revision this migration revises.
GROUP6_REVISION = "a2b3c4d5e6f7"

#: Every table enabled by the group 7 migration.
PLATFORM_TABLES = (
    "platform_roles",
    "platform_role_permissions",
    "platform_memberships",
    "bootstrap_states",
)

_PLATFORM_ROLES_READ = "platform_roles_runtime_read"
_PLATFORM_ROLE_PERMISSIONS_READ = "platform_role_permissions_runtime_read"
_PLATFORM_MEMBERSHIPS_SELF = "platform_memberships_self_isolation"
_PLATFORM_MEMBERSHIPS_PLATFORM = "platform_memberships_platform_access"
_BOOTSTRAP_SERVICE_READ = "bootstrap_states_service_read"
_BOOTSTRAP_SERVICE_INSERT = "bootstrap_states_service_insert"
_BOOTSTRAP_SERVICE_DELETE = "bootstrap_states_service_delete"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
    database_url = os.environ["DATABASE_URL"]
    if not database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")
    upgrade_to_head()
    yield database_url
    downgrade_to_base()


@pytest.fixture
def runtime_database_url(migrated_database: str) -> str:
    """Provision the runtime login and return its database URL."""
    provision_runtime_login(migrated_database)
    return runtime_url(migrated_database)


def _detached_user(user_id: uuid.UUID, email: str = "platform@example.com") -> User:
    return User(
        id=user_id,
        workos_user_id=f"user_platform_{uuid.uuid4().hex[:10]}",
        email=email,
        name="Platform Actor",
        is_active=True,
    )


async def _policy_count(database_url: str, table: str, policy: str) -> int:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_policies WHERE tablename = :table "
                    "AND policyname = :policy"
                ),
                {"table": table, "policy": policy},
            )
        return int(value or 0)
    finally:
        await engine.dispose()


async def _count(session: AsyncSession, table: str) -> int:
    return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)


_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")


async def _table_privileges(database_url: str, table: str) -> set[str]:
    """Return the privileges the runtime role holds on ``table``.

    Queried with the owner credential (the runtime login cannot introspect its
    own privileges through ``information_schema``), using
    ``has_table_privilege`` so inherited ``ON ALL TABLES`` grants are visible.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            held: set[str] = set()
            for privilege in _TABLE_PRIVILEGES:
                granted = await connection.scalar(
                    text("SELECT has_table_privilege('app_runtime', :table, :privilege)"),
                    {"table": table, "privilege": privilege},
                )
                if granted:
                    held.add(privilege)
            return held
    finally:
        await engine.dispose()


async def _function_exists(database_url: str, name: str) -> bool:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(
                text("SELECT count(*) FROM pg_proc WHERE proname = :name"),
                {"name": name},
            )
        return bool(count)
    finally:
        await engine.dispose()


async def _rls_flags(database_url: str, table: str) -> tuple[bool, bool]:
    """Return ``(enabled, forced)`` RLS flags for ``table``."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = :table"
                    ),
                    {"table": table},
                )
            ).one()
        return bool(row.relrowsecurity), bool(row.relforcerowsecurity)
    finally:
        await engine.dispose()


# --- Installed production policy ---------------------------------------------


async def test_production_policies_are_installed_and_forced(migrated_database: str) -> None:
    """RLS is enabled and forced and the group policies reference the helpers."""
    expected = {
        "platform_roles": (_PLATFORM_ROLES_READ,),
        "platform_role_permissions": (_PLATFORM_ROLE_PERMISSIONS_READ,),
        "platform_memberships": (
            _PLATFORM_MEMBERSHIPS_SELF,
            _PLATFORM_MEMBERSHIPS_PLATFORM,
        ),
        "bootstrap_states": (
            _BOOTSTRAP_SERVICE_READ,
            _BOOTSTRAP_SERVICE_INSERT,
            _BOOTSTRAP_SERVICE_DELETE,
        ),
    }
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            for table, policies in expected.items():
                for policy in policies:
                    row = (
                        await connection.execute(
                            text(
                                "SELECT qual, with_check FROM pg_policies "
                                "WHERE tablename = :table AND policyname = :policy"
                            ),
                            {"table": table, "policy": policy},
                        )
                    ).one_or_none()
                    assert row is not None, f"missing {policy} on {table}"
                flags = (
                    await connection.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname = :table"
                        ),
                        {"table": table},
                    )
                ).one()
                assert flags.relrowsecurity is True, table
                assert flags.relforcerowsecurity is True, table

            self_qual = (
                await connection.execute(
                    text(
                        "SELECT qual FROM pg_policies WHERE tablename = 'platform_memberships' "
                        "AND policyname = :policy"
                    ),
                    {"policy": _PLATFORM_MEMBERSHIPS_SELF},
                )
            ).scalar_one()
            assert "app_current_user_id()" in self_qual
            platform_qual = (
                await connection.execute(
                    text(
                        "SELECT qual FROM pg_policies WHERE tablename = 'platform_memberships' "
                        "AND policyname = :policy"
                    ),
                    {"policy": _PLATFORM_MEMBERSHIPS_PLATFORM},
                )
            ).scalar_one()
            assert "app_current_platform_admin()" in platform_qual
            assert "app_current_platform_service()" in platform_qual

            # The identity-bearing sentinel is not readable runtime-wide: every
            # bootstrap policy predicate is gated to the platform/service
            # context, and none is an unconditional ``true``.
            bootstrap_policies = (
                await connection.execute(
                    text(
                        "SELECT policyname, qual, with_check FROM pg_policies "
                        "WHERE tablename = 'bootstrap_states'"
                    )
                )
            ).all()
            assert {row.policyname for row in bootstrap_policies} == {
                _BOOTSTRAP_SERVICE_READ,
                _BOOTSTRAP_SERVICE_INSERT,
                _BOOTSTRAP_SERVICE_DELETE,
            }
            for row in bootstrap_policies:
                predicate = row.qual if row.qual is not None else row.with_check
                assert predicate is not None
                assert "app_current_platform_service()" in predicate
                assert predicate.strip().lower() != "true"
    finally:
        await engine.dispose()


async def test_group7_grant_level_least_privilege(migrated_database: str) -> None:
    """The runtime role's table grants are narrowed to the platform-plane need."""
    assert await _table_privileges(migrated_database, "platform_roles") == {"SELECT"}
    assert await _table_privileges(migrated_database, "platform_role_permissions") == {"SELECT"}
    assert await _table_privileges(migrated_database, "platform_memberships") == {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
    }
    assert await _table_privileges(migrated_database, "bootstrap_states") == {
        "SELECT",
        "INSERT",
        "DELETE",
    }


async def test_platform_catalogue_is_readable_and_not_writable(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The runtime role reads the platform catalogue but cannot write it."""
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            roles = await session.scalar(text("SELECT count(*) FROM platform_roles"))
            assert int(roles or 0) >= 1
            grants = await session.scalar(text("SELECT count(*) FROM platform_role_permissions"))
            assert int(grants or 0) >= 1
            await session.rollback()

        async with factory() as session:
            with pytest.raises((DBAPIError, ProgrammingError)):
                await session.execute(
                    text(
                        "INSERT INTO platform_roles (id, code, name) VALUES (:id, :code, 'Sneaky')"
                    ),
                    {"id": uuid.uuid4(), "code": f"sneaky_{uuid.uuid4().hex[:8]}"},
                )
            await session.rollback()

        # Both catalogue tables deny UPDATE and DELETE at the grant level, not
        # merely by the absence of a policy: the earlier rollout groups' table-
        # wide DML grant is revoked by group 7.
        for table in ("platform_roles", "platform_role_permissions"):
            async with factory() as session:
                with pytest.raises((DBAPIError, ProgrammingError)):
                    await session.execute(text(f"UPDATE {table} SET name = 'Sneaky'"))
                await session.rollback()
            async with factory() as session:
                with pytest.raises((DBAPIError, ProgrammingError)):
                    await session.execute(text(f"DELETE FROM {table}"))
                await session.rollback()
    finally:
        await engine.dispose()


# --- platform_memberships isolation ------------------------------------------


async def test_no_context_reads_no_platform_memberships(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Without a user or platform context, platform memberships are default-denied."""
    await seed_platform_plane(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            assert await _count(session, "platform_memberships") == 0
            await session.rollback()
    finally:
        await engine.dispose()


async def test_self_context_reads_only_own_membership(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A user context admits exactly the caller's own platform membership."""
    seed = await seed_platform_plane(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            ids = set(
                (await session.execute(text("SELECT id FROM platform_memberships"))).scalars()
            )
            assert ids == {seed.membership_a}
            await session.rollback()
    finally:
        await engine.dispose()


async def test_platform_and_service_contexts_read_cross_user(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The validated platform and trusted service contexts read every membership."""
    seed = await seed_platform_plane(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_platform_context(session)
            ids = set(
                (await session.execute(text("SELECT id FROM platform_memberships"))).scalars()
            )
            assert {seed.membership_a, seed.membership_b} <= ids
            await session.rollback()

        async with factory() as session:
            await bind_platform_service_context(session)
            ids = set(
                (await session.execute(text("SELECT id FROM platform_memberships"))).scalars()
            )
            assert {seed.membership_a, seed.membership_b} <= ids
            await session.rollback()
    finally:
        await engine.dispose()


async def test_self_context_cannot_write_platform_authority(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A user context can read its own membership but never grant itself platform status."""
    seed = await seed_platform_plane(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_user_context(session, seed.user_c)
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        "INSERT INTO platform_memberships (id, user_id, platform_role_id) "
                        "VALUES (:id, :user, :role)"
                    ),
                    {"id": uuid.uuid4(), "user": seed.user_c, "role": seed.role_id},
                )
            await session.rollback()

        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            # The caller's *visible* own row (membership_a) must be write-denied
            # too, not just another user's row: a self-bound user can read their
            # own membership but can never rewrite or delete their own platform
            # authority.
            updated = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE platform_memberships SET platform_role_id = :role WHERE id = :id"),
                    {"role": seed.role_id, "id": seed.membership_a},
                ),
            )
            assert updated.rowcount == 0
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM platform_memberships WHERE id = :id"),
                    {"id": seed.membership_a},
                ),
            )
            assert deleted.rowcount == 0
            await session.rollback()
    finally:
        await engine.dispose()


async def test_platform_context_can_grant_and_revoke(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The validated platform context grants and revokes another user's membership."""
    seed = await seed_platform_plane(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_platform_context(session)
            membership_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO platform_memberships (id, user_id, platform_role_id) "
                    "VALUES (:id, :user, :role)"
                ),
                {"id": membership_id, "user": seed.user_c, "role": seed.role_id},
            )
            await session.commit()

        async with factory() as session:
            await bind_platform_context(session)
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM platform_memberships WHERE id = :id"),
                    {"id": membership_id},
                ),
            )
            assert deleted.rowcount == 1
            await session.commit()
    finally:
        await engine.dispose()


async def test_bootstrap_singleton_write_is_gated(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The identity-bearing sentinel is context-gated for read and write, and immutable."""
    seed = await seed_platform_plane(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            # Not readable without a context: the row carries the consuming
            # administrator's identity, so even the existence check is gated.
            assert await _count(session, "bootstrap_states") == 0
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(text("DELETE FROM bootstrap_states")),
            )
            assert deleted.rowcount == 0
            await session.rollback()

        async with factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        "INSERT INTO bootstrap_states (id, email, consumed_by_user_id) "
                        "VALUES (1, :email, :user)"
                    ),
                    {"email": "no-context@example.com", "user": seed.user_a},
                )
            await session.rollback()

        async with factory() as session:
            await bind_platform_service_context(session)
            assert await _count(session, "bootstrap_states") >= 1
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(text("DELETE FROM bootstrap_states")),
            )
            assert deleted.rowcount == 1
            await session.execute(
                text(
                    "INSERT INTO bootstrap_states (id, email, consumed_by_user_id) "
                    "VALUES (1, :email, :user)"
                ),
                {"email": "service@example.com", "user": seed.user_a},
            )
            await session.commit()

        async with factory() as session:
            # UPDATE is denied at the grant level even under the service
            # context: the sentinel is never modified once consumed.
            await bind_platform_service_context(session)
            with pytest.raises((DBAPIError, ProgrammingError)):
                await session.execute(
                    text("UPDATE bootstrap_states SET email = :email WHERE id = 1"),
                    {"email": "tampered@example.com"},
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Platform status is not tenant authority ---------------------------------


async def test_platform_context_grants_no_tenant_rows(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The validated platform context is not a tenant: ordinary tenant rows stay hidden."""
    org_a, _org_b, _record_a, _record_b = await seed_two_organisation_records(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_platform_context(session)
            assert await _count(session, "records") == 0
            await session.rollback()

        async with factory() as session:
            await bind_platform_context(session)
            await bind_organisation_context(session, org_a)
            assert await _count(session, "records") == 1
            await session.rollback()
    finally:
        await engine.dispose()


async def test_platform_service_context_grants_no_tenant_rows(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The narrow service context is not a tenant bypass either."""
    org_a, _org_b, _record_a, _record_b = await seed_two_organisation_records(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_platform_service_context(session)
            assert await _count(session, "records") == 0
            await session.rollback()

        async with factory() as session:
            await bind_platform_service_context(session)
            await bind_organisation_context(session, org_a)
            assert await _count(session, "records") == 1
            await session.rollback()
    finally:
        await engine.dispose()


# --- Context lifetime ---------------------------------------------------------


async def test_service_context_survives_neither_commit_nor_pool_reuse(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The service context clears on commit and never leaks across pooled connections."""
    await seed_platform_plane(migrated_database)
    engine = runtime_engine(runtime_database_url, pooled=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_platform_service_context(session)
            assert bound_platform_service_context(session) is True
            assert await _count(session, "platform_memberships") >= 1
            await session.commit()
            value = await session.scalar(
                text("SELECT current_setting('app.platform_service', true)")
            )
            assert value in (None, ""), "service context must clear on commit"
            await session.rollback()

        # A later checkout of the same pooled connection is default-denied.
        async with factory() as session:
            assert await _count(session, "platform_memberships") == 0
            await session.rollback()
    finally:
        await engine.dispose()


# --- Platform permission dependency ------------------------------------------


async def test_platform_permission_dependency_resolves_under_self_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The dependency resolves the caller's own platform membership, then binds the context."""
    seed = await seed_platform_plane(migrated_database)
    dependency = require_platform_permission("platform.admin")
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await dependency(session, _detached_user(seed.user_a, seed.user_a_email))
            assert bound_platform_context(session) is True
            assert bound_platform_service_context(session) is False
            await session.rollback()

        async with factory() as session:
            with pytest.raises(PermissionDenied):
                await dependency(session, _detached_user(seed.user_c, seed.user_c_email))
            assert bound_platform_context(session) is False
            await session.rollback()
    finally:
        await engine.dispose()


# --- Service-path bindings ----------------------------------------------------


class _StubProfileClient(UserProfileClient):
    """Return a fixed verified profile so the bootstrap never calls WorkOS."""

    def __init__(self, *, email: str) -> None:
        self._email = email

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        return UserProfile(email=self._email, name="Platform Bootstrap", email_verified=True)


async def test_bootstrap_grant_binds_the_service_context(
    migrated_database: str, runtime_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-time bootstrap grant writes platform authority on the restricted role."""
    seed = await seed_platform_plane(migrated_database)
    # Reset the singleton so this run's grant fires.
    owner = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner.begin() as connection:
            await connection.execute(text("DELETE FROM bootstrap_states"))
    finally:
        await owner.dispose()
    monkeypatch.setattr(
        platform_admin_service,
        "get_settings",
        lambda: SimpleNamespace(
            bootstrap_platform_admin_email=seed.user_c_email,
            bootstrap_platform_admin_org="",
        ),
    )
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user = _detached_user(seed.user_c, seed.user_c_email)
            membership = await platform_admin_service.maybe_grant_bootstrap_platform_admin(
                session, user, _StubProfileClient(email=seed.user_c_email)
            )
            assert membership is not None
    finally:
        await engine.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            count = await connection.scalar(
                text("SELECT count(*) FROM platform_memberships WHERE user_id = :user"),
                {"user": seed.user_c},
            )
            assert int(count or 0) == 1
    finally:
        await owner_engine.dispose()


async def test_recovery_cli_binds_the_service_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The break-glass recovery CLI grants platform_admin with no administrator present."""
    seed = await seed_platform_plane(migrated_database)
    owner = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner.begin() as connection:
            await connection.execute(text("DELETE FROM platform_memberships"))
    finally:
        await owner.dispose()
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user = await platform_admin_service.recover_platform_admin(
                session, email=seed.user_c_email, reason="rls group 7 test"
            )
            assert user.id == seed.user_c
    finally:
        await engine.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            count = await connection.scalar(
                text("SELECT count(*) FROM platform_memberships WHERE user_id = :user"),
                {"user": seed.user_c},
            )
            assert int(count or 0) == 1
    finally:
        await owner_engine.dispose()


async def test_teardown_removes_platform_rows_without_a_bypass(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The cross-tenant teardown removes the target's platform membership and sentinel."""
    seed = await seed_platform_plane(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            removed = await platform_admin_service.delete_provisioned_user(
                session, user_id=seed.user_a, source="test"
            )
            assert removed is True
    finally:
        await engine.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            memberships = await connection.scalar(
                text("SELECT count(*) FROM platform_memberships WHERE user_id = :user"),
                {"user": seed.user_a},
            )
            assert int(memberships or 0) == 0
            sentinels = await connection.scalar(
                text("SELECT count(*) FROM bootstrap_states WHERE consumed_by_user_id = :user"),
                {"user": seed.user_a},
            )
            assert int(sentinels or 0) == 0
    finally:
        await owner_engine.dispose()


async def test_user_deleted_webhook_binds_the_service_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The signature-verified user.deleted path reads cross-user authority on the restricted role."""
    seed = await seed_platform_plane(migrated_database)
    owner = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner.begin() as connection:
            # Leave exactly one enabled platform administrator so the webhook's
            # lockout check has to read the cross-user platform membership set
            # through the narrow service context.
            await connection.execute(
                text("DELETE FROM platform_memberships WHERE id <> :membership"),
                {"membership": seed.membership_a},
            )
            workos_user_id = await connection.scalar(
                text("SELECT workos_user_id FROM users WHERE id = :user"),
                {"user": seed.user_a},
            )
    finally:
        await owner.dispose()

    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            event = WorkOSWebhookEvent(
                id=f"evt_user_deleted_{uuid.uuid4().hex[:8]}",
                event="user.deleted",
                data={"id": workos_user_id, "email": seed.user_a_email},
            )
            changed = await process_webhook_event(session, event)
            assert changed is True
    finally:
        await engine.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            active = await connection.scalar(
                text("SELECT is_active FROM users WHERE id = :user"),
                {"user": seed.user_a},
            )
            assert active is False
            lockouts = await connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = 'platform.admin_lockout' "
                    "AND resource_id = :user"
                ),
                {"user": str(seed.user_a)},
            )
            assert int(lockouts or 0) >= 1
    finally:
        await owner_engine.dispose()


# --- Migration reversibility --------------------------------------------------


def test_group7_migration_downgrade_and_reupgrade(migrated_database: str) -> None:
    """One revision down removes only the platform-only policies, flags, helper and grants."""
    config = alembic_config()
    group7_policies = (
        ("platform_roles", _PLATFORM_ROLES_READ),
        ("platform_role_permissions", _PLATFORM_ROLE_PERMISSIONS_READ),
        ("platform_memberships", _PLATFORM_MEMBERSHIPS_SELF),
        ("platform_memberships", _PLATFORM_MEMBERSHIPS_PLATFORM),
        ("bootstrap_states", _BOOTSTRAP_SERVICE_READ),
        ("bootstrap_states", _BOOTSTRAP_SERVICE_INSERT),
        ("bootstrap_states", _BOOTSTRAP_SERVICE_DELETE),
    )
    full_dml = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    try:
        command.downgrade(config, GROUP6_REVISION)
        for table, policy in group7_policies:
            assert asyncio.run(_policy_count(migrated_database, table, policy)) == 0, policy
        # The group-7 helper is removed and RLS is disabled.
        assert asyncio.run(_function_exists(migrated_database, "app_current_platform_service")) is (
            False
        )
        for table in PLATFORM_TABLES:
            assert asyncio.run(_rls_flags(migrated_database, table)) == (False, False), table
        # The group-6 privilege state is restored: the earlier groups' table-wide
        # DML grant is what group 7 narrowed, so downgrade must put it back.
        for table in PLATFORM_TABLES:
            assert asyncio.run(_table_privileges(migrated_database, table)) == full_dml, table
        # The earlier group-6 audit policy stays intact.
        assert (
            asyncio.run(_policy_count(migrated_database, "audit_events", "audit_events_append"))
            == 1
        )

        command.upgrade(config, "head")
        for table, policy in group7_policies:
            assert asyncio.run(_policy_count(migrated_database, table, policy)) == 1, policy
        assert asyncio.run(_function_exists(migrated_database, "app_current_platform_service")) is (
            True
        )
        for table in PLATFORM_TABLES:
            assert asyncio.run(_rls_flags(migrated_database, table)) == (True, True), table
        assert asyncio.run(_table_privileges(migrated_database, "platform_roles")) == {"SELECT"}
        assert asyncio.run(_table_privileges(migrated_database, "platform_role_permissions")) == {
            "SELECT"
        }
        assert asyncio.run(_table_privileges(migrated_database, "bootstrap_states")) == {
            "SELECT",
            "INSERT",
            "DELETE",
        }
    finally:
        command.upgrade(alembic_config(), "head")
