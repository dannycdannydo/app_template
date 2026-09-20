"""Real-PostgreSQL production enablement suite for the operational ledgers (P4, 6).

Plan P4 / ``docs/rls-rollout.md`` §3 group 6. It proves the production
enablement migration (``a2b3c4d5e6f7``) that promotes the operational ledgers
``audit_events``, ``outbox_events``, ``maintenance_runs`` and
``webhook_events`` into the permanent policy chain, and provisions the isolated
``app_operator`` credential:

- the group policies are installed with matching ``USING``/``WITH CHECK`` and
  RLS is enabled **and forced** on all four tables;
- a ``NULL`` tenant key never means "all rows": without context a tenant-keyed
  read returns nothing, and a tenant context sees only its own ``audit_events``
  and ``outbox_events`` rows — never another organisation's and never a global
  (null-tenant) row;
- the validated, transaction-local platform context admits the cross-tenant and
  global ``audit_events`` history the platform audit screen lists, while the
  same context admits nothing on the tenant-keyed ``outbox_events``;
- the ordinary runtime role can append (the ledger is append-only) but cannot
  update or delete ``audit_events``, cannot attribute an event to a foreign
  organisation, and cannot mutate another organisation's ``outbox_events`` row;
- ``app_coordinator`` reads the whole dispatch ledger, moves a dispatch through
  its lifecycle, cannot delete a live dispatch and can delete a published one,
  cannot rewrite any immutable outbox column (tenant key, event
  identity/contract, payload, aggregate, identity/deduplication or immutable
  timestamp) and schedules/recovers the global maintenance runs;
- the global ``maintenance_runs`` and ``webhook_events`` ledgers admit the roles
  that own their paths and deny the others;
- the isolated ``app_operator`` role is non-superuser, owns no table, is not a
  member of any other application role, carries ``BYPASSRLS``, can read the
  cross-tenant rows a policy cannot express, and cannot be assumed by the
  runtime role; a pre-existing entangled operator role is normalised in both
  membership directions and its downgrade removes only the migration's grants;
- the ORM ``available_at`` default is app-clock UTC within the documented skew
  bound; transaction-local context does not survive commit or pooled-connection
  reuse; and
- the migration upgrades, downgrades one revision and re-upgrades cleanly.

The suite runs against real PostgreSQL with the restricted runtime login (the
migrations create the roles ``NOLOGIN``; the test grants throwaway credentials).
``migrated_database`` reverts to base at teardown, so the rollout leaves no
residue.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.rls_helpers import (
    METRICS_ROLE,
    OPERATOR_ROLE,
    RUNTIME_ROLE,
    alembic_config,
    coordinator_url,
    database_reachable,
    downgrade_to_base,
    force_drop_operator_role,
    operator_url,
    provision_coordinator_login,
    provision_operator_login,
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_two_organisation_ledgers,
    upgrade_to_head,
)

from app.api.dependencies import require_platform_permission
from app.core.exceptions import PermissionDenied
from app.db.rls import bind_organisation_context, bind_platform_context, bound_platform_context
from app.modules.users.models import User

#: The group 5 revision this migration revises.
GROUP5_REVISION = "f1a2b3c4d5e6"

#: Every table enabled by the group 6 migration.
LEDGER_TABLES = ("audit_events", "outbox_events", "maintenance_runs", "webhook_events")

_AUDIT_ISOLATION = "audit_events_organisation_isolation"
_AUDIT_PLATFORM = "audit_events_platform_read"
_AUDIT_APPEND = "audit_events_append"
_OUTBOX_ISOLATION = "outbox_events_organisation_isolation"
_OUTBOX_APPEND = "outbox_events_append"
_OUTBOX_COORDINATOR_READ = "outbox_events_coordinator_read"
_OUTBOX_COORDINATOR_UPDATE = "outbox_events_coordinator_update"
_OUTBOX_COORDINATOR_DELETE = "outbox_events_coordinator_delete"
_MAINTENANCE_RUNTIME_READ = "maintenance_runs_runtime_read"
_MAINTENANCE_COORDINATOR = "maintenance_runs_coordinator_access"
_WEBHOOK_RUNTIME_READ = "webhook_events_runtime_read"


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
def coordinator_database_url(migrated_database: str) -> str:
    """Provision the coordinator login and return its database URL."""
    provision_coordinator_login(migrated_database)
    return coordinator_url(migrated_database)


@pytest.fixture
def operator_database_url(migrated_database: str) -> str:
    """Provision the isolated operational login and return its database URL."""
    provision_operator_login(migrated_database)
    return operator_url(migrated_database)


async def _set_context(connection: AsyncConnection, setting: str, value: str) -> None:
    """Bind a transaction-local setting on a raw connection."""
    await connection.execute(
        text("SELECT set_config(:setting, :value, true)"),
        {"setting": setting, "value": value},
    )


def _detached_user(user_id: uuid.UUID) -> User:
    return User(
        id=user_id,
        workos_user_id=f"user_ledger_{uuid.uuid4().hex[:10]}",
        email=f"ledger-{user_id}@example.com",
        name="Ledger Actor",
        is_active=True,
    )


# --- Installed production policy and credential -------------------------------


async def test_production_policies_are_installed_and_forced(migrated_database: str) -> None:
    """RLS is enabled and forced and the group policies reference the helpers."""
    expected = {
        "audit_events": (_AUDIT_ISOLATION, _AUDIT_PLATFORM, _AUDIT_APPEND),
        "outbox_events": (_OUTBOX_ISOLATION, _OUTBOX_APPEND),
        "maintenance_runs": (_MAINTENANCE_RUNTIME_READ, _MAINTENANCE_COORDINATOR),
        "webhook_events": (_WEBHOOK_RUNTIME_READ,),
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

            platform_qual = (
                await connection.execute(
                    text(
                        "SELECT qual FROM pg_policies WHERE tablename = 'audit_events' "
                        "AND policyname = :policy"
                    ),
                    {"policy": _AUDIT_PLATFORM},
                )
            ).scalar_one()
            assert "app_current_platform_admin()" in platform_qual
            runtime_qual = (
                await connection.execute(
                    text(
                        "SELECT qual FROM pg_policies WHERE tablename = 'outbox_events' "
                        "AND policyname = :policy"
                    ),
                    {"policy": _OUTBOX_ISOLATION},
                )
            ).scalar_one()
            assert "app_current_tenant_id()" in runtime_qual

            # The append policy is tenant-checked, not ``true``: a foreign
            # tenant attribution is denied even if a service predicate is
            # missed, while own-tenant and global rows stay writable.
            append_check = (
                await connection.execute(
                    text(
                        "SELECT with_check FROM pg_policies WHERE tablename = 'audit_events' "
                        "AND policyname = :policy"
                    ),
                    {"policy": _AUDIT_APPEND},
                )
            ).scalar_one()
            assert "app_current_tenant_id()" in append_check
            assert append_check.strip() != "true"
    finally:
        await engine.dispose()


async def test_operator_role_is_isolated_and_bypasses_rls(migrated_database: str) -> None:
    """``app_operator`` owns no table, is no member, and may bypass RLS."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
                        "rolcanlogin FROM pg_roles WHERE rolname = :role"
                    ),
                    {"role": OPERATOR_ROLE},
                )
            ).one()
            # The migration creates it NOLOGIN; a deployment grants the
            # credential out of band (the test fixture does the same).
            assert role.rolsuper is False
            assert role.rolbypassrls is True
            assert role.rolcreatedb is False
            assert role.rolcreaterole is False
            assert role.rolcanlogin is False

            owned = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_roles r "
                    "ON r.oid = c.relowner WHERE r.rolname = :role"
                ),
                {"role": OPERATOR_ROLE},
            )
            assert owned == 0

            memberships = (
                await connection.execute(
                    text(
                        "SELECT granted.rolname FROM pg_auth_members m "
                        "JOIN pg_roles granted ON granted.oid = m.roleid "
                        "JOIN pg_roles member ON member.oid = m.member "
                        "WHERE member.rolname = :role"
                    ),
                    {"role": OPERATOR_ROLE},
                )
            ).scalars()
            assert set(memberships) == set()
    finally:
        await engine.dispose()


async def test_runtime_cannot_assume_operator_role(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The runtime role is not a member of ``app_operator`` and cannot SET ROLE."""
    engine = runtime_engine(runtime_database_url)
    try:
        async with engine.begin() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text(f"SET ROLE {OPERATOR_ROLE}"))
    finally:
        await engine.dispose()


async def test_operator_reads_the_cross_tenant_ledger(
    migrated_database: str, operator_database_url: str
) -> None:
    """The isolated credential reads all tenants' rows `BYPASSRLS` cannot express."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = create_async_engine(operator_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            audits = (await connection.execute(text("SELECT id FROM audit_events"))).scalars()
            assert set(audits) >= {seed.audit_a, seed.audit_b, seed.audit_global}
            outbox = (await connection.execute(text("SELECT id FROM outbox_events"))).scalars()
            assert set(outbox) >= {seed.outbox_a, seed.outbox_b, seed.outbox_global}
    finally:
        await engine.dispose()


# --- Default denial and tenant isolation --------------------------------------


async def test_default_denial_without_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """No context returns no tenant-keyed ledger rows and rejects a tenant write."""
    await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for table in ("audit_events", "outbox_events"):
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO outbox_events "
                        "(id, organisation_id, event_type, event_version, payload, "
                        "deduplication_key, status) "
                        "VALUES (:id, :org, 'job.dispatch_requested', 1, '{}'::jsonb, "
                        ":dedup, 'pending')"
                    ),
                    {"id": uuid.uuid4(), "org": uuid.uuid4(), "dedup": uuid.uuid4().hex},
                )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_tenant_reads_only_its_own_ledger_rows(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Organisation A sees its rows only: never B's and never a global row."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            audits = (await session.execute(text("SELECT id FROM audit_events"))).scalars()
            assert set(audits) == {seed.audit_a}
            outbox = (await session.execute(text("SELECT id FROM outbox_events"))).scalars()
            assert set(outbox) == {seed.outbox_a}
            await session.rollback()
    finally:
        await engine.dispose()


async def test_platform_context_reads_cross_tenant_and_global_audit(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The validated platform context admits the cross-tenant audit history."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            # No context, no hint of the global or foreign rows.
            assert await session.scalar(text("SELECT count(*) FROM audit_events")) == 0
            await session.rollback()

        async with factory() as session:
            await bind_platform_context(session)
            audits = (await session.execute(text("SELECT id FROM audit_events"))).scalars()
            assert {seed.audit_a, seed.audit_b, seed.audit_global} <= set(audits)
            # The platform context is not a tenant: the tenant-keyed outbox
            # remains default-denied.
            assert await session.scalar(text("SELECT count(*) FROM outbox_events")) == 0
            await session.rollback()
    finally:
        await engine.dispose()


async def test_platform_permission_dependency_binds_the_platform_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The platform permission dependency binds the context only after it authorises.

    A platform administrator with the seeded ``platform.admin`` grant gets the
    cross-tenant audit read; a plain user is rejected and no platform context is
    left bound.
    """
    seed = await seed_two_organisation_ledgers(migrated_database)
    admin_id, plain_id = uuid.uuid4(), uuid.uuid4()
    owner = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, workos_user_id, email, name, is_active) "
                    "VALUES (:id, :workos, :email, 'Admin', true), "
                    "(:pid, :pworkos, :pemail, 'Plain', true)"
                ),
                {
                    "id": admin_id,
                    "workos": f"user_platform_{uuid.uuid4().hex[:10]}",
                    "email": f"platform-{admin_id}@example.com",
                    "pid": plain_id,
                    "pworkos": f"user_plain_{uuid.uuid4().hex[:10]}",
                    "pemail": f"plain-{plain_id}@example.com",
                },
            )
            role_id = await connection.scalar(
                text("SELECT id FROM platform_roles WHERE code = 'platform_admin'")
            )
            assert role_id is not None, "platform_admin role is not seeded"
            await connection.execute(
                text(
                    "INSERT INTO platform_memberships (id, user_id, platform_role_id) "
                    "VALUES (:id, :user, :role)"
                ),
                {"id": uuid.uuid4(), "user": admin_id, "role": role_id},
            )
    finally:
        await owner.dispose()

    dependency = require_platform_permission("platform.admin")
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await dependency(session, _detached_user(admin_id))
            assert bound_platform_context(session) is True
            audits = (await session.execute(text("SELECT id FROM audit_events"))).scalars()
            assert {seed.audit_a, seed.audit_b, seed.audit_global} <= set(audits)
            await session.rollback()

        async with factory() as session:
            with pytest.raises(PermissionDenied):
                await dependency(session, _detached_user(plain_id))
            assert bound_platform_context(session) is False
            await session.rollback()
    finally:
        await engine.dispose()


async def test_runtime_append_and_cross_tenant_outbox_denial(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Runtime appends its own/global rows and cannot write a foreign outbox row."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            # Append: a tenant-attributed audit row and a global (system) one.
            await session.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, organisation_id, action, resource_type, resource_id) "
                    "VALUES (:id, :org, 'record.created', 'record', 'r'), "
                    "(:gid, NULL, 'system.event', 'system', 's')"
                ),
                {"id": uuid.uuid4(), "org": seed.org_a, "gid": uuid.uuid4()},
            )
            # Append: an own-tenant outbox row and the global maintenance row.
            await session.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(id, organisation_id, event_type, event_version, payload, "
                    "deduplication_key, status) "
                    "VALUES (:id, :org, 'job.dispatch_requested', 1, '{}'::jsonb, :d, "
                    "'pending'), "
                    "(:gid, NULL, 'ai.retention', 1, '{}'::jsonb, :gd, 'pending')"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": seed.org_a,
                    "d": uuid.uuid4().hex,
                    "gid": uuid.uuid4(),
                    "gd": uuid.uuid4().hex,
                },
            )
            await session.commit()

        # A foreign-organisation outbox append is rejected by ``WITH CHECK``.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO outbox_events "
                        "(id, organisation_id, event_type, event_version, payload, "
                        "deduplication_key, status) "
                        "VALUES (:id, :org, 'job.dispatch_requested', 1, '{}'::jsonb, "
                        ":dedup, 'pending')"
                    ),
                    {"id": uuid.uuid4(), "org": seed.org_b, "dedup": uuid.uuid4().hex},
                )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_runtime_cannot_attribute_audit_to_a_foreign_tenant(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The tenant-checked append policy denies foreign-organisation attribution.

    A session bound to organisation A cannot append an event attributed to
    organisation B, and neither can an unbound ordinary runtime session; only
    the writer's own tenant or a global (null-tenant) row is admissible without
    the explicit platform context.
    """
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    append = text(
        "INSERT INTO audit_events "
        "(id, organisation_id, action, resource_type, resource_id) "
        "VALUES (:id, :org, 'record.created', 'record', 'r')"
    )
    try:
        # A tenant context may not attach an event to another tenant.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(append, {"id": uuid.uuid4(), "org": seed.org_b})
            await session.rollback()

        # An unbound ordinary session may not either: a foreign organisation is
        # not the same as the permitted global (null-tenant) attribution.
        async with factory() as session:
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(append, {"id": uuid.uuid4(), "org": seed.org_b})
            await session.rollback()
    finally:
        await engine.dispose()


async def test_outbox_available_at_default_is_bounded_to_the_database_clock(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The ORM ``available_at`` default is app-clock UTC within accepted skew.

    Plan P4 group 6 moved the ORM default from the database clock to the
    application clock so a global (null-tenant) runtime insert never needs the
    ``RETURNING`` read the append-only policy does not grant. This asserts the
    documented accepted bound (NTP-synchronised host/database clocks) and that
    the flush itself needs no read.
    """
    from app.modules.outbox.models import OutboxEvent, OutboxEventStatus

    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    event_id = uuid.uuid4()
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            event = OutboxEvent(
                id=event_id,
                organisation_id=seed.org_a,
                event_type="job.dispatch_requested",
                event_version=1,
                payload={},
                deduplication_key=uuid.uuid4().hex,
                status=OutboxEventStatus.PENDING,
            )
            session.add(event)
            # The flush must succeed without a RETURNING read under the
            # INSERT-only runtime policy.
            await session.flush()
            await session.commit()
    finally:
        await engine.dispose()

    owner = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner.connect() as connection:
            database_now = await connection.scalar(text("SELECT now()"))
            due = await connection.scalar(
                text("SELECT available_at FROM outbox_events WHERE id = :id"),
                {"id": event_id},
            )
    finally:
        await owner.dispose()
    assert database_now is not None
    assert due is not None
    assert abs((due - database_now).total_seconds()) < 5


async def test_runtime_cannot_update_or_delete_audit(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The append-only ledger stays append-only under RLS (no UPDATE/DELETE policy)."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url)
    try:
        async with engine.begin() as connection:
            await _set_context(connection, "app.organisation_id", str(seed.org_a))
            # No UPDATE policy exists, so PostgreSQL filters every row out and
            # the append-only trigger never fires: zero rows are affected.
            updated = await connection.execute(
                text("UPDATE audit_events SET action = 'tampered' WHERE id = :id"),
                {"id": seed.audit_a},
            )
            assert updated.rowcount == 0
            # DELETE is denied the same way, before the trigger can reject it.
            deleted = await connection.execute(
                text("DELETE FROM audit_events WHERE id = :id"), {"id": seed.audit_a}
            )
            assert deleted.rowcount == 0
    finally:
        await engine.dispose()

    owner = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner.connect() as connection:
            action = await connection.scalar(
                text("SELECT action FROM audit_events WHERE id = :id"),
                {"id": seed.audit_a},
            )
            assert action == "record.created"
    finally:
        await owner.dispose()


async def test_runtime_cannot_update_outbox(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The runtime role has no outbox UPDATE policy, so a claim stays unchanged."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url)
    try:
        async with engine.begin() as connection:
            await _set_context(connection, "app.organisation_id", str(seed.org_a))
            result = await connection.execute(
                text("UPDATE outbox_events SET status = 'dead' WHERE id = :id"),
                {"id": seed.outbox_a},
            )
            assert result.rowcount == 0
    finally:
        await engine.dispose()

    owner = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner.connect() as connection:
            status = await connection.scalar(
                text("SELECT status FROM outbox_events WHERE id = :id"),
                {"id": seed.outbox_a},
            )
            assert status == "pending"
    finally:
        await owner.dispose()


# --- Coordinator and global ledgers -------------------------------------------


async def test_coordinator_dispatch_lifecycle(
    migrated_database: str, coordinator_database_url: str
) -> None:
    """The coordinator reads all dispatches, settles one, and purges a published one."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = create_async_engine(coordinator_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            rows = (await connection.execute(text("SELECT id FROM outbox_events"))).scalars()
            assert {seed.outbox_a, seed.outbox_b, seed.outbox_global} <= set(rows)

            claimed = await connection.execute(
                text(
                    "UPDATE outbox_events SET status = 'publishing', claimed_at = now(), "
                    "claim_token = 'token' WHERE id = :id AND status = 'pending'"
                ),
                {"id": seed.outbox_a},
            )
            assert claimed.rowcount == 1
            settled = await connection.execute(
                text(
                    "UPDATE outbox_events SET status = 'published', processed_at = now() "
                    "WHERE id = :id AND status = 'publishing' AND claim_token = 'token'"
                ),
                {"id": seed.outbox_a},
            )
            assert settled.rowcount == 1

            # A live dispatch is not deletable; a published one is.
            live = await connection.execute(
                text("DELETE FROM outbox_events WHERE id = :id"), {"id": seed.outbox_b}
            )
            assert live.rowcount == 0
            purged = await connection.execute(
                text("DELETE FROM outbox_events WHERE id = :id"), {"id": seed.outbox_a}
            )
            assert purged.rowcount == 1
    finally:
        await engine.dispose()


async def test_checkpoint_locking_read_of_published_rows(
    migrated_database: str, coordinator_database_url: str
) -> None:
    """The coordinator may take the retention sweep's lock on a published row."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = create_async_engine(coordinator_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE outbox_events SET status = 'published' WHERE id = :id"),
                {"id": seed.outbox_global},
            )
            locked = await connection.execute(
                text(
                    "SELECT id FROM outbox_events WHERE id = :id AND status = 'published' "
                    "FOR UPDATE SKIP LOCKED"
                ),
                {"id": seed.outbox_global},
            )
            assert locked.scalar_one() == seed.outbox_global
    finally:
        await engine.dispose()


async def test_coordinator_cannot_rewrite_immutable_outbox_columns(
    migrated_database: str, coordinator_database_url: str
) -> None:
    """Column-level grants deny every non-lifecycle outbox rewrite.

    The coordinator owns the dispatch lifecycle, but it must never be able to
    move a dispatch to another organisation, rewrite its event identity or
    contract, change its payload, retarget its aggregate, change its
    deduplication key or rewrite its immutable identity/timestamp columns.
    """
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(coordinator_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements = [
        (
            "UPDATE outbox_events SET organisation_id = :value WHERE id = :id",
            seed.org_b,
            seed.outbox_a,
        ),
        (
            "UPDATE outbox_events SET event_type = :value WHERE id = :id",
            "job.dispatch_requested",
            seed.outbox_a,
        ),
        ("UPDATE outbox_events SET event_version = :value WHERE id = :id", 2, seed.outbox_a),
        (
            "UPDATE outbox_events SET payload = CAST(:value AS jsonb) WHERE id = :id",
            '{"tampered": true}',
            seed.outbox_a,
        ),
        ("UPDATE outbox_events SET aggregate_type = :value WHERE id = :id", "x", seed.outbox_a),
        (
            "UPDATE outbox_events SET aggregate_id = :value WHERE id = :id",
            uuid.uuid4(),
            seed.outbox_a,
        ),
        (
            "UPDATE outbox_events SET deduplication_key = :value WHERE id = :id",
            uuid.uuid4().hex,
            seed.outbox_a,
        ),
        (
            "UPDATE outbox_events SET created_at = :value WHERE id = :id",
            datetime.now(UTC),
            seed.outbox_a,
        ),
        ("UPDATE outbox_events SET id = :value WHERE id = :id", uuid.uuid4(), seed.outbox_a),
    ]
    try:
        for statement, value, target in statements:
            async with factory() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement), {"value": value, "id": target})
                await session.rollback()
    finally:
        await engine.dispose()


async def test_global_ledgers_admit_runtime_and_coordinator(
    migrated_database: str,
    runtime_database_url: str,
    coordinator_database_url: str,
) -> None:
    """Maintenance runs and webhook dedup rows are reachable only by the owning roles."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url)
    try:
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM maintenance_runs")) >= 1
            assert await connection.scalar(text("SELECT count(*) FROM webhook_events")) >= 1
            # The maintenance worker claims and settles its own run.
            claimed = await connection.execute(
                text(
                    "UPDATE maintenance_runs SET status = 'running', attempt_count = 1, "
                    "owner_token = :token, lease_expires_at = now() + interval '1 hour', "
                    "started_at = now() WHERE id = :id AND status = 'queued'"
                ),
                {"token": uuid.uuid4(), "id": seed.maintenance_run},
            )
            assert claimed.rowcount == 1
            # The webhook consumer deduplicates and inserts one event id.
            new_event = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO webhook_events (id, event_id, event_type) "
                    "VALUES (:id, :event_id, 'invitation.revoked')"
                ),
                {"id": new_event, "event_id": f"event_{uuid.uuid4().hex}"},
            )
    finally:
        await engine.dispose()

    engine = create_async_engine(coordinator_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            # The coordinator schedules/recovery reads and writes runs.
            assert await connection.scalar(text("SELECT count(*) FROM maintenance_runs")) >= 1
            scheduled = await connection.execute(
                text(
                    "INSERT INTO maintenance_runs "
                    "(id, task_type, schedule_key, scheduled_for, status) "
                    "VALUES (:id, 'ai.transfer_reconcile', :key, now(), 'queued')"
                ),
                {"id": uuid.uuid4(), "key": f"ai.transfer_reconcile:{uuid.uuid4().hex}"},
            )
            assert scheduled.rowcount == 1
    finally:
        await engine.dispose()


# --- Context lifetime ---------------------------------------------------------


async def test_context_does_not_survive_commit_or_pool_reuse(
    migrated_database: str, runtime_database_url: str
) -> None:
    """After a commit the transaction-local context is gone on the pooled connection."""
    seed = await seed_two_organisation_ledgers(migrated_database)
    engine = runtime_engine(runtime_database_url, pooled=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            visible = await session.scalar(text("SELECT count(*) FROM audit_events"))
            assert visible == 1
            await session.commit()
            # A new transaction on the same pooled connection has no context.
            hidden = await session.scalar(text("SELECT count(*) FROM audit_events"))
            assert hidden == 0
            await session.rollback()
    finally:
        await engine.dispose()


async def test_runtime_credential_cannot_disable_policy_or_alter_schema(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The runtime role is non-owner and cannot weaken the operational policies."""
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            statements = [
                "ALTER ROLE app_runtime BYPASSRLS",
                "ALTER TABLE audit_events DISABLE ROW LEVEL SECURITY",
                "ALTER TABLE outbox_events NO FORCE ROW LEVEL SECURITY",
                "DROP POLICY audit_events_append ON audit_events",
                "ALTER TABLE maintenance_runs ADD COLUMN hacked integer",
            ]
            for statement in statements:
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
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


def test_group6_migration_downgrade_and_reupgrade(migrated_database: str) -> None:
    """One revision down removes only the ledger policies and the operator role."""
    config = alembic_config()
    try:
        command.downgrade(config, GROUP5_REVISION)
        for table, policy in (
            ("audit_events", _AUDIT_ISOLATION),
            ("audit_events", _AUDIT_PLATFORM),
            ("outbox_events", _OUTBOX_ISOLATION),
            ("maintenance_runs", _MAINTENANCE_RUNTIME_READ),
            ("webhook_events", _WEBHOOK_RUNTIME_READ),
        ):
            assert asyncio.run(_policy_count(migrated_database, table, policy)) == 0, policy
        assert asyncio.run(_role_exists(migrated_database, OPERATOR_ROLE)) is False
        # The earlier group-5 identity policies stay intact.
        assert (
            asyncio.run(
                _policy_count(
                    migrated_database,
                    "organisation_memberships",
                    "organisation_memberships_organisation_isolation",
                )
            )
            == 1
        )

        command.upgrade(config, "head")
        for table, policy in (
            ("audit_events", _AUDIT_ISOLATION),
            ("audit_events", _AUDIT_PLATFORM),
            ("outbox_events", _OUTBOX_ISOLATION),
            ("outbox_events", _OUTBOX_COORDINATOR_DELETE),
            ("maintenance_runs", _MAINTENANCE_RUNTIME_READ),
            ("webhook_events", _WEBHOOK_RUNTIME_READ),
        ):
            assert asyncio.run(_policy_count(migrated_database, table, policy)) == 1, policy
        assert asyncio.run(_role_exists(migrated_database, OPERATOR_ROLE)) is True
    finally:
        command.upgrade(alembic_config(), "head")


async def _operator_state(database_url: str) -> tuple[bool, bool, bool, bool, bool]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
                        "rolcanlogin FROM pg_roles WHERE rolname = :role"
                    ),
                    {"role": OPERATOR_ROLE},
                )
            ).one()
    finally:
        await engine.dispose()
    return (
        bool(row.rolsuper),
        bool(row.rolbypassrls),
        bool(row.rolcreatedb),
        bool(row.rolcreaterole),
        bool(row.rolcanlogin),
    )


async def _operator_membership_count(database_url: str) -> int:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_auth_members m "
                    "JOIN pg_roles granted ON granted.oid = m.roleid "
                    "JOIN pg_roles member ON member.oid = m.member "
                    "WHERE granted.rolname = :role OR member.rolname = :role"
                ),
                {"role": OPERATOR_ROLE},
            )
    finally:
        await engine.dispose()
    return int(value or 0)


async def _operator_can_read_table(database_url: str) -> bool:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            table = await connection.scalar(
                text("SELECT has_table_privilege(:role, 'audit_events', 'SELECT')"),
                {"role": OPERATOR_ROLE},
            )
    finally:
        await engine.dispose()
    return bool(table)


def test_group6_migration_normalises_an_adversarial_operator_role(
    migrated_database: str,
) -> None:
    """A pre-existing entangled operator role is normalised, never trusted.

    Start from the group-5 boundary and create an adversarial ``app_operator``
    (superuser/createdb/createrole, with ``app_runtime`` as a member of it so
    the ordinary role could ``SET ROLE`` the bypass credential, and itself
    inheriting the ordinary ``app_metrics`` role). The upgrade must strip the
    privileged attributes, revoke membership in both directions, preserve the
    adopted role's own login, and the downgrade must remove the migration-added
    operational read grants without dropping the adopted role.
    """
    config = alembic_config()

    async def _run(sql: str) -> None:
        engine = create_async_engine(migrated_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(sql))
        finally:
            await engine.dispose()

    try:
        command.downgrade(config, GROUP5_REVISION)
        # The migration-created role is gone at this boundary; provision an
        # adversarial replacement that entangles both membership directions:
        # ``app_runtime`` becomes a member of the operator (so the ordinary role
        # could ``SET ROLE`` the bypass credential) and the operator inherits the
        # ordinary ``app_metrics`` role.
        asyncio.run(
            _run(f"CREATE ROLE {OPERATOR_ROLE} LOGIN SUPERUSER BYPASSRLS CREATEDB CREATEROLE")
        )
        asyncio.run(_run(f"GRANT {OPERATOR_ROLE} TO {RUNTIME_ROLE}"))
        asyncio.run(_run(f"GRANT {METRICS_ROLE} TO {OPERATOR_ROLE}"))

        command.upgrade(config, "head")

        super_, bypass, createdb, createrole, canlogin = asyncio.run(
            _operator_state(migrated_database)
        )
        assert super_ is False
        assert bypass is True
        assert createdb is False
        assert createrole is False
        assert canlogin is True, "the adopted credential's own login is preserved"
        assert asyncio.run(_operator_membership_count(migrated_database)) == 0

        # The ordinary runtime role can no longer assume the bypass credential.
        provision_runtime_login(migrated_database)

        async def _runtime_set_role() -> None:
            engine = runtime_engine(runtime_url(migrated_database))
            try:
                async with engine.begin() as connection:
                    with pytest.raises(DBAPIError):
                        await connection.execute(text(f"SET ROLE {OPERATOR_ROLE}"))
            finally:
                await engine.dispose()

        asyncio.run(_runtime_set_role())

        command.downgrade(config, GROUP5_REVISION)
        # The adopted role survives, but this migration's read surface does not.
        assert asyncio.run(_role_exists(migrated_database, OPERATOR_ROLE)) is True
        # Schema ``USAGE`` is held via PUBLIC, so the meaningful migration-owned
        # grant to check is the direct table ``SELECT`` the operator read path
        # depends on.
        assert asyncio.run(_operator_can_read_table(migrated_database)) is False, (
            "migration SELECT grant must not survive downgrade"
        )
    finally:
        # Restore a clean, migration-created operator at head for the fixture.
        command.downgrade(config, "base")
        force_drop_operator_role(migrated_database)
        command.upgrade(alembic_config(), "head")
