"""Real-PostgreSQL production enablement suite for the notifications group (P3).

Plan P3 / ``docs/rls-rollout.md`` §3 group 2. It proves the production
enablement migration (``f5a6b7c8d9e0``) that promotes the user-private
``notifications`` table and its indirectly owned ``notification_deliveries``
ledger into the permanent policy chain:

- ``notifications`` carries the user-private policy requiring both the
  transaction-local organisation and user, and ``notification_deliveries``
  inherits the parent boundary through an ``EXISTS`` on the parent notification;
- both tables have RLS enabled **and forced**;
- default denial: no bound context returns no rows and fails closed for writes;
- the transaction-local ``app.user_id`` key is validated, its absent/empty and
  malformed forms deny, and it does not survive a commit, rollback or pooled
  connection reuse;
- both **read and write** policies deny select, insert, update and delete of
  another organisation's rows *and* another recipient's rows in the same
  organisation, including through an unscoped query and a tenant/user-key move;
- the non-bypass operational aggregate reports the true attention-required
  count (the ``attention_required_email_deliveries`` metric) with no tenant
  context, while the runtime role still cannot read the delivery rows directly;
- a foreign notification is indistinguishable from a missing one on the real
  runtime-role app path;
- the email worker binds the organisation and recipient user from the durable
  ``jobs`` row and drives the delivery to ``succeeded`` under the enforced
  policy across its self-committing service steps;
- representative list/detail plans use the indexes; and
- the migration upgrades, downgrades one revision and re-upgrades cleanly.

The suite runs against real PostgreSQL with the restricted runtime login (the
migration creates the role ``NOLOGIN``; the test grants a throwaway credential).
``migrated_database`` reverts to base at teardown, so the rollout leaves no
residue.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import pytest
from alembic import command
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.auth_helpers import generate_key_pair
from tests.org_isolation_helpers import (
    NOT_FOUND,
    IsolationWorld,
    auth_headers,
    build_isolation_app,
    seed_isolation_world,
)
from tests.rls_helpers import (
    METRICS_ROLE,
    RUNTIME_ROLE,
    alembic_config,
    database_reachable,
    downgrade_to_base,
    metrics_url,
    provision_metrics_login,
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_representative_notifications,
    seed_two_organisation_notifications,
    upgrade_to_head,
)

from app.db.rls import (
    RLS_USER_SETTING,
    bind_organisation_context,
    bind_user_context,
    clear_user_context,
)
from app.modules.jobs import execution as jobs_execution
from app.modules.jobs.models import Job, JobStatus
from app.modules.notifications import service as notifications_service
from app.modules.notifications import tasks as notifications_tasks

#: The group 1 revision this migration revises.
GROUP1_REVISION = "e3f4a5b6c7d8"

#: Production policies installed by the group 2 migration.
NOTIFICATIONS_POLICY = "notifications_user_isolation"
DELIVERIES_POLICY = "notification_deliveries_parent_isolation"

#: The narrow operational read the in-process metrics loop uses.
OPERATIONAL_METRICS_POLICY = "notification_deliveries_operational_count"
OPERATIONAL_COUNT_FUNCTION = "app_attention_required_delivery_count"

_LATENCY_BUDGET_MS = 500.0
_LIST_PAGE_SQL = (
    "SELECT id, title FROM notifications WHERE organisation_id = :org "
    "AND user_id = :user ORDER BY created_at DESC, id DESC LIMIT 50"
)


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


@pytest.fixture
def metrics_database_url(migrated_database: str) -> str:
    """Provision the operational-metrics login and return its database URL."""
    provision_metrics_login(migrated_database)
    return metrics_url(migrated_database)


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    """One module-local RSA key for minting WorkOS-style tokens."""
    key, _ = generate_key_pair()
    return key


# --- Installed production policies ------------------------------------------


async def test_production_policies_are_installed_and_forced(migrated_database: str) -> None:
    """RLS is enabled and forced with the canonical user-private policies."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT tablename, policyname, qual, with_check FROM pg_policies "
                        "WHERE policyname IN (:notifications, :deliveries)"
                    ),
                    {
                        "notifications": NOTIFICATIONS_POLICY,
                        "deliveries": DELIVERIES_POLICY,
                    },
                )
            ).all()
            policies = {row.policyname: row for row in rows}
            assert set(policies) == {NOTIFICATIONS_POLICY, DELIVERIES_POLICY}
            notification_policy = policies[NOTIFICATIONS_POLICY]
            assert notification_policy.tablename == "notifications"
            assert "app_current_tenant_id()" in notification_policy.qual
            assert "app_current_user_id()" in notification_policy.qual
            assert "app_current_tenant_id()" in notification_policy.with_check
            assert "app_current_user_id()" in notification_policy.with_check
            delivery_policy = policies[DELIVERIES_POLICY]
            assert delivery_policy.tablename == "notification_deliveries"
            assert "app_current_tenant_id()" in delivery_policy.qual
            assert "app_current_user_id()" in delivery_policy.qual
            assert "app_current_tenant_id()" in delivery_policy.with_check
            assert "app_current_user_id()" in delivery_policy.with_check

            flags = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('notifications', 'notification_deliveries')"
                    )
                )
            ).all()
            assert {row.relname for row in flags} == {
                "notifications",
                "notification_deliveries",
            }
            assert all(row.relrowsecurity and row.relforcerowsecurity for row in flags)

            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role"),
                    {"role": RUNTIME_ROLE},
                )
            ).one()
            assert role.rolsuper is False
            assert role.rolbypassrls is False

            metrics_role = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
                        "WHERE rolname = :role"
                    ),
                    {"role": METRICS_ROLE},
                )
            ).one()
            assert metrics_role.rolsuper is False
            assert metrics_role.rolbypassrls is False
            assert metrics_role.rolcanlogin is False, "the operational role is NOLOGIN"

            operational_policy = (
                await connection.execute(
                    text(
                        "SELECT qual, roles::text AS roles FROM pg_policies "
                        "WHERE tablename = 'notification_deliveries' AND policyname = :policy"
                    ),
                    {"policy": OPERATIONAL_METRICS_POLICY},
                )
            ).one()
            assert "attention_required" in operational_policy.qual
            assert METRICS_ROLE in operational_policy.roles

            function_owner = await connection.scalar(
                text(
                    "SELECT r.rolname FROM pg_proc p "
                    "JOIN pg_roles r ON r.oid = p.proowner "
                    "WHERE p.proname = :name"
                ),
                {"name": OPERATIONAL_COUNT_FUNCTION},
            )
            assert function_owner == METRICS_ROLE
            function_flags = await connection.scalar(
                text("SELECT prosecdef FROM pg_proc WHERE proname = :name"),
                {"name": OPERATIONAL_COUNT_FUNCTION},
            )
            assert function_flags is True, "the aggregate read must be SECURITY DEFINER"
    finally:
        await engine.dispose()


# --- Default denial ----------------------------------------------------------


async def test_default_denial_without_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """No bound context: zero rows and every write is rejected."""
    await seed_two_organisation_notifications(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
            assert await session.scalar(text("SELECT count(*) FROM notification_deliveries")) == 0
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO notifications "
                        "(id, organisation_id, user_id, type, title, body) "
                        "VALUES (:id, :org, :user, 'notification.test_sent', 'x', '')"
                    ),
                    {"id": uuid.uuid4(), "org": uuid.uuid4(), "user": uuid.uuid4()},
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Transaction-local user context lifecycle -------------------------------


async def test_empty_and_malformed_user_context_deny(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Empty or non-UUID user context resolves to NULL and matches no row."""
    seed = await seed_two_organisation_notifications(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            # The supported clear path resolves to NULL, not "all users".
            await clear_user_context(session)
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
            # A malformed value must not raise into an unrestricted read.
            await session.execute(
                text("SELECT set_config(:setting, 'not-a-uuid', true)"),
                {"setting": RLS_USER_SETTING},
            )
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
            # Python-side validation rejects a malformed id before any SQL runs.
            with pytest.raises(ValueError):
                await bind_user_context(session, "not-a-uuid")
            # A valid context still works afterwards.
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 1
            await session.rollback()
    finally:
        await engine.dispose()


async def test_user_context_does_not_survive_commit_rollback_or_pool_reuse(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The user key clears on every transaction boundary and pool reuse.

    The group-2 policies require both keys, so an organisation bound without a
    user must still be default-denied for the user-private rows. This mirrors
    the P2 ``app.organisation_id`` lifetime proof for the new ``app.user_id``
    security context.
    """
    seed = await seed_two_organisation_notifications(migrated_database)
    engine = runtime_engine(runtime_database_url, pooled=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 1

            # Commit: the same session's next transaction has no user context.
            await session.commit()
            assert await session.scalar(
                text("SELECT current_setting(:s, true)"), {"s": RLS_USER_SETTING}
            ) in (None, "")
            await bind_organisation_context(session, seed.org_a)
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
            # A validated rebind restores access.
            await bind_user_context(session, seed.user_a1)
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 1

            # Rollback clears it too.
            await session.rollback()
            await bind_organisation_context(session, seed.org_a)
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0

        # A brand-new session on the freed pooled connection is default-denied.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            assert await session.scalar(text("SELECT count(*) FROM notifications")) == 0
    finally:
        await engine.dispose()


# --- Cross-organisation and cross-recipient reads ---------------------------


async def test_notifications_are_org_and_user_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Own rows are visible; a foreign or other-recipient row is invisible."""
    seed = await seed_two_organisation_notifications(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            unscoped = (
                await session.execute(text("SELECT organisation_id, user_id FROM notifications"))
            ).all()
            assert {(row.organisation_id, row.user_id) for row in unscoped} == {
                (seed.org_a, seed.user_a1)
            }
            own = await session.scalar(
                text("SELECT id FROM notifications WHERE id = :id"),
                {"id": seed.notification_a1},
            )
            same_org_other_user = await session.scalar(
                text("SELECT id FROM notifications WHERE id = :id"),
                {"id": seed.notification_a2},
            )
            other_org = await session.scalar(
                text("SELECT id FROM notifications WHERE id = :id"),
                {"id": seed.notification_b1},
            )
            assert own == seed.notification_a1
            assert same_org_other_user is None
            assert other_org is None
            await session.rollback()
    finally:
        await engine.dispose()


async def test_deliveries_inherit_parent_boundary(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A delivery is visible only through its own organisation+recipient parent."""
    seed = await seed_two_organisation_notifications(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            unscoped = (await session.execute(text("SELECT id FROM notification_deliveries"))).all()
            assert {row.id for row in unscoped} == {seed.delivery_a1}
            assert (
                await session.scalar(
                    text("SELECT id FROM notification_deliveries WHERE id = :id"),
                    {"id": seed.delivery_a2},
                )
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT id FROM notification_deliveries WHERE id = :id"),
                    {"id": seed.delivery_b1},
                )
                is None
            )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Cross-organisation and cross-recipient writes --------------------------


async def test_notifications_crud_matrix_is_org_and_user_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Every CRUD operation is allowed in scope and denied across both boundaries.

    Plan P3 completion evidence ("real select/insert/update/delete policy tests")
    and ``docs/rls-rollout.md`` §2.3: the same-scope success case and the
    cross-organisation *and* cross-recipient denial are proven for each of
    select, insert, update and delete, plus the organisation and recipient key
    moves.
    """
    seed = await seed_two_organisation_notifications(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # SELECT: own visible, both foreign boundaries invisible.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            assert (
                await session.scalar(
                    text("SELECT id FROM notifications WHERE id = :id"),
                    {"id": seed.notification_a1},
                )
                == seed.notification_a1
            )
            assert (
                await session.scalar(
                    text("SELECT id FROM notifications WHERE id = :id"),
                    {"id": seed.notification_a2},
                )
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT id FROM notifications WHERE id = :id"),
                    {"id": seed.notification_b1},
                )
                is None
            )
            await session.rollback()

        # INSERT/UPDATE/DELETE in scope all succeed and commit.
        new_notification = uuid.uuid4()
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            await session.execute(
                text(
                    "INSERT INTO notifications "
                    "(id, organisation_id, user_id, type, title, body) "
                    "VALUES (:id, :org, :user, 'notification.test_sent', 'own', '')"
                ),
                {"id": new_notification, "org": seed.org_a, "user": seed.user_a1},
            )
            own_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE notifications SET title = 'updated' WHERE id = :id"),
                    {"id": new_notification},
                ),
            )
            assert own_update.rowcount == 1
            own_delete = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM notifications WHERE id = :id"),
                    {"id": new_notification},
                ),
            )
            assert own_delete.rowcount == 1
            await session.commit()

        # INSERT across the organisation boundary is rejected by WITH CHECK.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO notifications "
                        "(id, organisation_id, user_id, type, title, body) "
                        "VALUES (:id, :org, :user, 'notification.test_sent', 'foreign', '')"
                    ),
                    {"id": uuid.uuid4(), "org": seed.org_b, "user": seed.user_a1},
                )
            await session.rollback()

        # INSERT across the recipient boundary is rejected too.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO notifications "
                        "(id, organisation_id, user_id, type, title, body) "
                        "VALUES (:id, :org, :user, 'notification.test_sent', 'other', '')"
                    ),
                    {"id": uuid.uuid4(), "org": seed.org_a, "user": seed.user_a2},
                )
            await session.rollback()

        # UPDATE of a foreign or other-recipient row matches nothing.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            other_recipient_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE notifications SET title = 'hijacked' WHERE id = :id"),
                    {"id": seed.notification_a2},
                ),
            )
            assert other_recipient_update.rowcount == 0
            other_org_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE notifications SET title = 'hijacked' WHERE id = :id"),
                    {"id": seed.notification_b1},
                ),
            )
            assert other_org_update.rowcount == 0
            await session.rollback()

        # DELETE of a foreign or other-recipient row matches nothing.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            other_recipient_delete = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM notifications WHERE id = :id"),
                    {"id": seed.notification_a2},
                ),
            )
            assert other_recipient_delete.rowcount == 0
            other_org_delete = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM notifications WHERE id = :id"),
                    {"id": seed.notification_b1},
                ),
            )
            assert other_org_delete.rowcount == 0
            await session.rollback()

        # Moving a same-scope row into another tenant is rejected.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text("UPDATE notifications SET organisation_id = :org WHERE id = :id"),
                    {"org": seed.org_b, "id": seed.notification_a1},
                )
            await session.rollback()

        # Moving a same-scope row to another recipient is rejected.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text("UPDATE notifications SET user_id = :user WHERE id = :id"),
                    {"user": seed.user_a2, "id": seed.notification_a1},
                )
            await session.rollback()
    finally:
        await engine.dispose()

    # The own delete committed; the foreign rows are untouched for the owner.
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT title FROM notifications WHERE id = :id"),
                    {"id": seed.notification_a2},
                )
                == "a2 notification"
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM notifications WHERE id = :id"),
                    {"id": new_notification},
                )
                == 0
            )
    finally:
        await owner_engine.dispose()


async def test_deliveries_crud_matrix_is_parent_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Every CRUD operation on the delivery ledger follows the parent boundary."""
    seed = await seed_two_organisation_notifications(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # SELECT: only the delivery under the bound org+recipient parent.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            assert (
                await session.scalar(
                    text("SELECT id FROM notification_deliveries WHERE id = :id"),
                    {"id": seed.delivery_a1},
                )
                == seed.delivery_a1
            )
            assert (
                await session.scalar(
                    text("SELECT id FROM notification_deliveries WHERE id = :id"),
                    {"id": seed.delivery_a2},
                )
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT id FROM notification_deliveries WHERE id = :id"),
                    {"id": seed.delivery_b1},
                )
                is None
            )
            await session.rollback()

        # INSERT/UPDATE/DELETE under the bound parent all succeed and commit.
        new_delivery = uuid.uuid4()
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            await session.execute(
                text(
                    "INSERT INTO notification_deliveries "
                    "(id, notification_id, channel, recipient, delivery_identity, status) "
                    "VALUES (:id, :notification, 'email', 'own@example.com', :identity, 'queued')"
                ),
                {
                    "id": new_delivery,
                    "notification": seed.notification_a1,
                    "identity": str(uuid.uuid4()),
                },
            )
            own_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text(
                        "UPDATE notification_deliveries SET recipient = 'updated@example.com' "
                        "WHERE id = :id"
                    ),
                    {"id": new_delivery},
                ),
            )
            assert own_update.rowcount == 1
            own_delete = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM notification_deliveries WHERE id = :id"),
                    {"id": new_delivery},
                ),
            )
            assert own_delete.rowcount == 1
            await session.commit()

        # INSERT under another recipient's parent is rejected.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO notification_deliveries "
                        "(id, notification_id, channel, recipient, delivery_identity, status) "
                        "VALUES (:id, :notification, 'email', 'x@example.com', :identity, 'queued')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "notification": seed.notification_a2,
                        "identity": str(uuid.uuid4()),
                    },
                )
            await session.rollback()

        # INSERT under another organisation's parent is rejected too.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO notification_deliveries "
                        "(id, notification_id, channel, recipient, delivery_identity, status) "
                        "VALUES (:id, :notification, 'email', 'y@example.com', :identity, 'queued')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "notification": seed.notification_b1,
                        "identity": str(uuid.uuid4()),
                    },
                )
            await session.rollback()

        # UPDATE of a foreign or other-recipient delivery matches nothing.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            other_recipient_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text(
                        "UPDATE notification_deliveries SET recipient = 'hijacked@example.com' "
                        "WHERE id = :id"
                    ),
                    {"id": seed.delivery_a2},
                ),
            )
            assert other_recipient_update.rowcount == 0
            other_org_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text(
                        "UPDATE notification_deliveries SET recipient = 'hijacked@example.com' "
                        "WHERE id = :id"
                    ),
                    {"id": seed.delivery_b1},
                ),
            )
            assert other_org_update.rowcount == 0
            await session.rollback()

        # DELETE of a foreign or other-recipient delivery matches nothing.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            other_recipient_delete = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM notification_deliveries WHERE id = :id"),
                    {"id": seed.delivery_a2},
                ),
            )
            assert other_recipient_delete.rowcount == 0
            other_org_delete = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM notification_deliveries WHERE id = :id"),
                    {"id": seed.delivery_b1},
                ),
            )
            assert other_org_delete.rowcount == 0
            await session.rollback()
    finally:
        await engine.dispose()

    # The foreign rows survived; the own delivery delete committed.
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM notification_deliveries WHERE id = :id"),
                    {"id": seed.delivery_b1},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM notification_deliveries WHERE id = :id"),
                    {"id": new_delivery},
                )
                == 0
            )
    finally:
        await owner_engine.dispose()


# --- Operational aggregate read ---------------------------------------------


async def _set_delivery_attention_required(owner_url: str, delivery_ids: list[uuid.UUID]) -> None:
    """Owner-role seed: mark the given deliveries as needing operator attention."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE notification_deliveries SET status = 'attention_required' "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": delivery_ids},
            )
    finally:
        await engine.dispose()


async def test_operational_aggregate_read_is_truthful_and_narrow(
    migrated_database: str, runtime_database_url: str, metrics_database_url: str
) -> None:
    """The metrics aggregate reports the true count without a bypass.

    Group 2 enforces the user-private policy, so the previous direct read of
    ``notification_deliveries`` returned zero and made the
    ``attention_required_email_deliveries`` safety metric silently false. The
    operational read must report the real cross-tenant count via a non-bypass
    path that exposes no delivery rows.
    """
    seed = await seed_two_organisation_notifications(migrated_database)
    await _set_delivery_attention_required(migrated_database, [seed.delivery_a1, seed.delivery_b1])

    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            # The direct read is still default-denied without tenant context...
            assert await session.scalar(text("SELECT count(*) FROM notification_deliveries")) == 0
            # ...but the operational aggregate reports the true count.
            assert await session.scalar(text(f"SELECT {OPERATIONAL_COUNT_FUNCTION}()")) == 2
            await session.rollback()
    finally:
        await engine.dispose()

    # The narrow policy is load-bearing: even the operational role sees only
    # the attention-required rows, never the queued rows.
    metrics_engine = create_async_engine(metrics_database_url, poolclass=NullPool)
    try:
        async with metrics_engine.connect() as connection:
            rows = (
                await connection.execute(text("SELECT id, status FROM notification_deliveries"))
            ).all()
        assert {row.id for row in rows} == {seed.delivery_a1, seed.delivery_b1}
        assert {row.status for row in rows} == {"attention_required"}
        # The supporting notifications grant is still default-denied without
        # context, so the operational role cannot read notification content.
        async with metrics_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM notifications")) == 0
    finally:
        await metrics_engine.dispose()


# --- Worker context propagation ---------------------------------------------


async def test_worker_binds_context_from_durable_job(
    migrated_database: str,
    runtime_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The email worker drives the delivery to ``succeeded`` under enforced RLS.

    Plan P3 / ADR-0022 decision 3: the worker reads its durable ``jobs`` row,
    then binds ``app.organisation_id`` and ``app.user_id`` from the row's own
    values. The handler crosses several self-committing service transactions,
    so the notifications service must bind the context for each protected
    transaction; this test proves that with the restricted runtime role and the
    group-2 policies enforced.
    """
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    try:
        async with owner_factory() as session:
            from app.modules.organisations.models import Organisation
            from app.modules.users.models import User

            organisation = Organisation(name="RLS Notifications Worker Ltd")
            session.add(organisation)
            await session.flush()
            user = User(
                workos_user_id=f"user_rls_notif_{uuid.uuid4().hex[:10]}",
                email="recipient@example.com",
                name="Recipient",
            )
            session.add(user)
            await session.flush()
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=organisation.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            delivery_id, job_id = delivery.id, job.id
    finally:
        await owner_engine.dispose()

    runtime_engine_instance = runtime_engine(runtime_database_url)
    runtime_factory = async_sessionmaker(runtime_engine_instance, expire_on_commit=False)
    monkeypatch.setattr(notifications_tasks, "async_session_factory", runtime_factory)
    monkeypatch.setattr(jobs_execution, "async_session_factory", runtime_factory)
    try:
        await notifications_tasks.send_notification_email(str(job_id))
    finally:
        await runtime_engine_instance.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    try:
        async with owner_factory() as session:
            from app.modules.notifications.models import (
                NotificationDelivery,
                NotificationDeliveryStatus,
            )

            delivery_row = await session.get(NotificationDelivery, delivery_id)
            assert delivery_row is not None
            assert delivery_row.status == NotificationDeliveryStatus.SUCCEEDED
            assert delivery_row.provider_message_id is not None
            job_row = await session.get(Job, job_id)
            assert job_row is not None
            assert job_row.status == JobStatus.SUCCEEDED, job_row.error_message
    finally:
        await owner_engine.dispose()


# --- Error non-disclosure on the runtime app path ---------------------------


@pytest.fixture
async def runtime_client(
    migrated_database: str,
    runtime_database_url: str,
    private_key: rsa.RSAPrivateKey,
) -> AsyncIterator[AsyncClient]:
    """The real ASGI app wired to the restricted runtime role."""
    built: FastAPI = build_isolation_app(runtime_database_url, private_key)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=built, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        await built.state.isolation_engine.dispose()


async def test_foreign_notification_is_indistinguishable_from_missing(
    migrated_database: str,
    runtime_client: AsyncClient,
    private_key: rsa.RSAPrivateKey,
) -> None:
    """A foreign notification and an absent one return the identical 404."""
    world: IsolationWorld = await seed_isolation_world(migrated_database)
    headers = auth_headers(private_key, world.a_owner, org_id=world.org_a)
    foreign = await runtime_client.patch(
        f"/api/v1/notifications/{world.notification_b}/read", headers=headers
    )
    missing = await runtime_client.patch(
        f"/api/v1/notifications/{uuid.uuid4()}/read", headers=headers
    )
    assert foreign.status_code == NOT_FOUND, foreign.text
    assert missing.status_code == NOT_FOUND, missing.text
    assert foreign.json()["code"] == "notification_not_found"
    assert missing.json()["code"] == "notification_not_found"
    assert foreign.json()["message"] == missing.json()["message"]
    assert foreign.json().get("details") == missing.json().get("details")


# --- Representative query plans ---------------------------------------------


async def test_representative_query_plans_use_the_indexes(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The notifications list plan uses the tenant index; no sequential scan."""
    org_a, user_a1, sample_id = await seed_representative_notifications(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)
            await bind_user_context(session, user_a1)
            list_plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(
                        text("EXPLAIN " + _LIST_PAGE_SQL),
                        {"org": org_a, "user": user_a1},
                    )
                ).all()
            )
            detail_plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(
                        text("EXPLAIN SELECT * FROM notifications WHERE id = :id"),
                        {"id": sample_id},
                    )
                ).all()
            )
            delivery_plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(
                        text("EXPLAIN SELECT * FROM notification_deliveries WHERE id = :id"),
                        {"id": uuid.uuid4()},
                    )
                ).all()
            )
            assert "Seq Scan" not in list_plan
            assert "ix_notifications_organisation_id_user_id_created_at" in list_plan
            assert "Seq Scan" not in detail_plan
            assert "Index" in detail_plan
            assert "Seq Scan" not in delivery_plan
            assert "pk_notification_deliveries" in delivery_plan

            loop = asyncio.get_running_loop()
            start = loop.time()
            rows = (
                await session.execute(text(_LIST_PAGE_SQL), {"org": org_a, "user": user_a1})
            ).all()
            list_ms = (loop.time() - start) * 1000
            assert rows
            assert list_ms < _LATENCY_BUDGET_MS
            await session.rollback()
    finally:
        await engine.dispose()


# --- Migration reversibility -------------------------------------------------


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


async def _rls_flags(database_url: str, table: str) -> tuple[bool, bool]:
    """Return ``(relrowsecurity, relforcerowsecurity)`` for ``table``."""
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


async def _user_function_exists(database_url: str) -> bool:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text("SELECT count(*) FROM pg_proc WHERE proname = 'app_current_user_id'")
            )
        return bool(value)
    finally:
        await engine.dispose()


async def _operational_function_exists(database_url: str) -> bool:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text("SELECT count(*) FROM pg_proc WHERE proname = :name"),
                {"name": OPERATIONAL_COUNT_FUNCTION},
            )
        return bool(value)
    finally:
        await engine.dispose()


async def _role_exists(database_url: str, role: str) -> bool:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text("SELECT count(*) FROM pg_roles WHERE rolname = :role"),
                {"role": role},
            )
        return bool(value)
    finally:
        await engine.dispose()


def test_group2_migration_downgrade_and_reupgrade(migrated_database: str) -> None:
    """One revision down removes only the group-2 state; re-upgrade re-installs it."""
    config = alembic_config()
    try:
        command.downgrade(config, GROUP1_REVISION)
        assert (
            asyncio.run(_policy_count(migrated_database, "notifications", NOTIFICATIONS_POLICY))
            == 0
        )
        assert (
            asyncio.run(
                _policy_count(migrated_database, "notification_deliveries", DELIVERIES_POLICY)
            )
            == 0
        )
        assert asyncio.run(_rls_flags(migrated_database, "notifications")) == (False, False)
        assert asyncio.run(_rls_flags(migrated_database, "notification_deliveries")) == (
            False,
            False,
        )
        assert asyncio.run(_user_function_exists(migrated_database)) is False
        assert asyncio.run(_operational_function_exists(migrated_database)) is False
        assert (
            asyncio.run(
                _policy_count(
                    migrated_database, "notification_deliveries", OPERATIONAL_METRICS_POLICY
                )
            )
            == 0
        )
        assert asyncio.run(_role_exists(migrated_database, METRICS_ROLE)) is False
        # Earlier groups stay intact.
        assert (
            asyncio.run(_policy_count(migrated_database, "files", "files_organisation_isolation"))
            == 1
        )
        assert asyncio.run(_rls_flags(migrated_database, "files")) == (True, True)
        assert (
            asyncio.run(
                _policy_count(migrated_database, "records", "records_organisation_isolation")
            )
            == 1
        )
        assert asyncio.run(_rls_flags(migrated_database, "records")) == (True, True)

        command.upgrade(config, "head")
        assert (
            asyncio.run(_policy_count(migrated_database, "notifications", NOTIFICATIONS_POLICY))
            == 1
        )
        assert (
            asyncio.run(
                _policy_count(migrated_database, "notification_deliveries", DELIVERIES_POLICY)
            )
            == 1
        )
        assert asyncio.run(_rls_flags(migrated_database, "notifications")) == (True, True)
        assert asyncio.run(_rls_flags(migrated_database, "notification_deliveries")) == (
            True,
            True,
        )
        assert asyncio.run(_user_function_exists(migrated_database)) is True
        assert asyncio.run(_operational_function_exists(migrated_database)) is True
        assert (
            asyncio.run(
                _policy_count(
                    migrated_database, "notification_deliveries", OPERATIONAL_METRICS_POLICY
                )
            )
            == 1
        )
        assert asyncio.run(_role_exists(migrated_database, METRICS_ROLE)) is True
    finally:
        command.upgrade(alembic_config(), "head")


# --- Restricted runtime credential ------------------------------------------


async def test_runtime_credential_cannot_disable_policy_or_alter_schema(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The runtime role is non-owner and cannot weaken the group-2 policies."""
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for statement in (
                "ALTER TABLE notifications DISABLE ROW LEVEL SECURITY",
                "ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY",
                "DROP POLICY notifications_user_isolation ON notifications",
                "DROP POLICY notification_deliveries_parent_isolation ON notification_deliveries",
                "DROP POLICY notification_deliveries_operational_count ON notification_deliveries",
                f"DROP FUNCTION {OPERATIONAL_COUNT_FUNCTION}()",
                f"ALTER FUNCTION {OPERATIONAL_COUNT_FUNCTION}() OWNER TO app_runtime",
                f"SET ROLE {METRICS_ROLE}",
                "DROP TABLE notifications",
                "ALTER TABLE notifications ADD COLUMN hacked integer",
                "ALTER ROLE app_runtime BYPASSRLS",
            ):
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
                await session.rollback()
    finally:
        await engine.dispose()
