"""Real-PostgreSQL conformance suite for indirect organisation ownership (P4).

Plan P4 / ADR-0022 decision 6. The plan requires the indirectly owned rows to be
protected by **one of two approved strategies**; the P4 final-strategy note
under decision 6 records which strategy each table took:

- the **denormalised-key** strategy: a copied, non-null ``organisation_id`` is
  added to the child, the parent foreign key is retained (as a composite key so
  the copy cannot diverge), and a single direct policy applies; or
- the **parent** strategy: the child carries no tenant key and a parent-existence
  policy reaches the parent through an ``EXISTS``.

This suite is the machine-checked P4 evidence that each indirect table uses the
strategy the inventory records and that the restricted ``app_runtime`` login is
genuinely bound by it:

- ``job_attempts`` uses the denormalised key (group 4b migration ``d0e1f2a3b4c5``)
  tied to its parent job by the composite ``(job_id, organisation_id)`` foreign
  key, with the canonical ``job_attempts_organisation_isolation`` policy;
- ``notification_deliveries`` uses the parent strategy (group 2 migration
  ``f5a6b7c8d9e0``) with the ``FOR ALL``
  ``notification_deliveries_parent_isolation`` policy that mirrors the
  organisation **and recipient** predicate of its parent notification;
- ``membership_roles`` uses the parent strategy (group 5 migration
  ``f1a2b3c4d5e6``) with the read visibility of
  ``membership_roles_parent_isolation`` **split from** the tenant-checked write
  authority of ``membership_roles_organisation_isolation`` so a pre-tenant user
  context can read its own grants but never mutate one.

The suite adds the cross-cutting conformance proof the per-group suites do not:
that the *strategy classification itself* is installed — the schema shape
(column presence/nullability, parent foreign keys) matches the approved
strategy, RLS is enabled and forced on all three tables, the approved policy is
forced default-deny, missing context fails closed, and the specific
parent/denormalised boundary denies foreign and divergent rows on the real
runtime role. It does not repeat each group suite's full CRUD matrix.

``migrated_database`` reverts to base at teardown, so the rollout leaves no
residue.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.rls_helpers import (
    RUNTIME_ROLE,
    database_reachable,
    downgrade_to_base,
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_two_organisation_identity,
    seed_two_organisation_jobs,
    seed_two_organisation_notifications,
    upgrade_to_head,
)

from app.db.rls import bind_organisation_context, bind_user_context

#: The table the inventory records as using the denormalised-key strategy.
DENORMALISED_TABLE = "job_attempts"

#: The tables the inventory records as using the parent strategy, with the
#: parent table and local parent column the policy reaches through.
PARENT_STRATEGY_TABLES = {
    "notification_deliveries": ("notification_id", "notifications"),
    "membership_roles": ("membership_id", "organisation_memberships"),
}

#: Every indirectly owned table, so the default-deny proof covers all three.
INDIRECT_TABLES = (DENORMALISED_TABLE, *PARENT_STRATEGY_TABLES)

#: The policies the approved strategies install, keyed by policy name.
APPROVED_POLICIES = {
    "job_attempts_organisation_isolation": "job_attempts",
    "notification_deliveries_parent_isolation": "notification_deliveries",
    "membership_roles_parent_isolation": "membership_roles",
    "membership_roles_organisation_isolation": "membership_roles",
}


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
    """Provision the restricted runtime login and return its database URL."""
    provision_runtime_login(migrated_database)
    return runtime_url(migrated_database)


async def _columns(session: AsyncConnection | AsyncSession, table: str) -> dict[str, bool]:
    """Return ``{column_name: is_nullable}`` for one public table."""
    rows = (
        await session.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table"
            ),
            {"table": table},
        )
    ).all()
    return {row.column_name: row.is_nullable == "YES" for row in rows}


async def _foreign_keys(
    session: AsyncConnection | AsyncSession, table: str
) -> list[dict[str, Any]]:
    """Return every foreign key on ``table`` as local/foreign column lists."""
    rows = (
        await session.execute(
            text(
                """
                SELECT c.conname AS name,
                       cl.relname AS parent_table,
                       array_agg(a.attname ORDER BY k.ord) AS local_columns,
                       array_agg(fa.attname ORDER BY k.ord) AS foreign_columns
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                JOIN pg_class cl ON cl.oid = c.confrelid
                JOIN LATERAL unnest(c.conkey, c.confkey)
                    WITH ORDINALITY AS k(attnum, fattnum, ord) ON true
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                JOIN pg_attribute fa ON fa.attrelid = cl.oid AND fa.attnum = k.fattnum
                WHERE c.contype = 'f' AND n.nspname = 'public' AND t.relname = :table
                GROUP BY c.conname, cl.relname
                """
            ),
            {"table": table},
        )
    ).all()
    return [
        {
            "name": row.name,
            "parent_table": row.parent_table,
            "local_columns": set(row.local_columns),
            "foreign_columns": set(row.foreign_columns),
        }
        for row in rows
    ]


async def _role_ids(owner_url: str) -> dict[str, uuid.UUID]:
    """Return the seeded role catalogue keyed by code (owner credential)."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(text("SELECT code, id FROM roles"))).all()
            return {row.code: row.id for row in rows}
    finally:
        await engine.dispose()


# --- Strategy conformance: the schema shape matches the approved strategy ---


async def test_job_attempts_uses_the_denormalised_key_strategy(
    migrated_database: str,
) -> None:
    """The child carries a non-null copied tenant key tied to its parent job."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            columns = await _columns(connection, DENORMALISED_TABLE)
            assert "organisation_id" in columns, "the denormalised tenant key is present"
            assert columns["organisation_id"] is False, "the copied tenant key is NOT NULL"

            foreign_keys = await _foreign_keys(connection, DENORMALISED_TABLE)
            composite = [
                fk
                for fk in foreign_keys
                if fk["parent_table"] == "jobs"
                and fk["local_columns"] == {"job_id", "organisation_id"}
            ]
            assert composite, "the copied key is tied to its parent by a composite foreign key"
            assert composite[0]["foreign_columns"] == {"id", "organisation_id"}, (
                "the composite key references the parent's own id and tenant key, so an "
                "attempt cannot be assigned to another organisation's job"
            )
    finally:
        await engine.dispose()


async def test_parent_strategy_tables_carry_no_tenant_key(migrated_database: str) -> None:
    """A parent-strategy child has no organisation_id and a real parent key."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            for table, (parent_column, parent_table) in PARENT_STRATEGY_TABLES.items():
                columns = await _columns(connection, table)
                assert "organisation_id" not in columns, (
                    f"{table} must inherit the boundary through its parent, not a copied key"
                )
                foreign_keys = await _foreign_keys(connection, table)
                parent_fks = [fk for fk in foreign_keys if fk["parent_table"] == parent_table]
                assert parent_fks, f"{table} must have a foreign key to {parent_table}"
                assert any(parent_column in fk["local_columns"] for fk in parent_fks), (
                    f"{table}.{parent_column} must be the parent key"
                )
    finally:
        await engine.dispose()


# --- Strategy conformance: the approved policy is installed and forced ---


async def test_approved_indirect_policies_are_installed_and_forced(
    migrated_database: str,
) -> None:
    """Every indirect table has RLS forced and only approved runtime policies."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT tablename, policyname, cmd, qual, with_check, roles::text AS roles "
                        "FROM pg_policies WHERE tablename = ANY(:tables)"
                    ),
                    {"tables": list(INDIRECT_TABLES)},
                )
            ).all()
            policies = {row.policyname: row for row in rows}
            # A policy that binds the restricted runtime login - by name or via
            # PUBLIC - is exactly the surface that could silently widen access
            # (permissive policies OR together), so every one must be approved.
            # Narrower policies for other roles (the coordinator, app_metrics)
            # are permitted.
            unapproved = [
                row.policyname
                for row in rows
                if (RUNTIME_ROLE in row.roles or "public" in row.roles)
                and row.policyname not in APPROVED_POLICIES
            ]
            assert unapproved == [], f"runtime-applicable policies not approved: {unapproved}"
            assert set(APPROVED_POLICIES) <= set(policies)

            for name, table in APPROVED_POLICIES.items():
                assert policies[name].tablename == table, name
                # A policy either names the runtime role or applies to PUBLIC
                # (which includes it); either way the restricted login is bound.
                assert RUNTIME_ROLE in policies[name].roles or "public" in policies[name].roles, (
                    name
                )

            # The denormalised-key table takes the canonical direct policy.
            direct = policies["job_attempts_organisation_isolation"]
            assert direct.cmd == "ALL"
            assert "app_current_tenant_id()" in direct.qual
            assert "app_current_tenant_id()" in direct.with_check

            # The parent-strategy deliveries table is FOR ALL and mirrors the
            # parent's organisation **and recipient** predicate in both clauses.
            deliveries = policies["notification_deliveries_parent_isolation"]
            assert deliveries.cmd == "ALL"
            for clause in (deliveries.qual, deliveries.with_check):
                assert "EXISTS" in clause
                assert "app_current_tenant_id()" in clause
                assert "app_current_user_id()" in clause

            # membership_roles splits read visibility from tenant-checked write.
            read = policies["membership_roles_parent_isolation"]
            assert read.cmd == "SELECT"
            assert "EXISTS" in read.qual
            assert "organisation_memberships" in read.qual
            write = policies["membership_roles_organisation_isolation"]
            assert write.cmd == "ALL"
            assert "organisation_memberships" in write.qual
            assert "app_current_tenant_id()" in write.qual
            assert "app_current_tenant_id()" in write.with_check

            flags = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = ANY(:tables)"
                    ),
                    {"tables": list(INDIRECT_TABLES)},
                )
            ).all()
            assert {row.relname for row in flags} == set(INDIRECT_TABLES)
            assert all(row.relrowsecurity and row.relforcerowsecurity for row in flags)

            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role"),
                    {"role": RUNTIME_ROLE},
                )
            ).one()
            assert role.rolsuper is False
            assert role.rolbypassrls is False
    finally:
        await engine.dispose()


# --- Strategy conformance: missing context fails closed on every table ---


async def test_missing_context_fails_closed_for_every_indirect_table(
    migrated_database: str, runtime_database_url: str
) -> None:
    """With no bound context every indirect table returns no row and denies a write."""
    jobs = await seed_two_organisation_jobs(migrated_database)
    notifications = await seed_two_organisation_notifications(migrated_database)
    identity = await seed_two_organisation_identity(migrated_database)
    role_ids = await _role_ids(migrated_database)

    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for table in INDIRECT_TABLES:
                count = await session.scalar(text(f"SELECT count(*) FROM {table}"))
                assert count == 0, table
            await session.rollback()

        # A write with no tenant context is default-denied, never unrestricted.
        # The parents are real, so only each table's policy denies the insert.
        async with factory() as session:
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO job_attempts "
                        "(id, job_id, organisation_id, dispatch_id, owner_token, "
                        "attempt_number, status, lease_expires_at, started_at, taken_over) "
                        "VALUES (:id, :job, :org, :dispatch, :owner, 9, 'running', now(), "
                        "now(), false)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "job": jobs.job_a,
                        "org": jobs.org_a,
                        "dispatch": uuid.uuid4(),
                        "owner": uuid.uuid4(),
                    },
                )
            await session.rollback()

            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO notification_deliveries "
                        "(id, notification_id, channel, recipient, delivery_identity, status) "
                        "VALUES (:id, :notification, 'email', 'x@example.com', :identity, 'queued')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "notification": notifications.notification_a1,
                        "identity": str(uuid.uuid4()),
                    },
                )
            await session.rollback()

            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO membership_roles (id, membership_id, role_id) "
                        "VALUES (:id, :membership, :role)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "membership": identity.membership_a,
                        "role": role_ids["viewer"],
                    },
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Denormalised-key enforcement on the real runtime role ---


async def test_job_attempt_enforcement_is_denormalised_key_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The copied key scopes reads and rejects a mismatched or moved key."""
    seed = await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            # An unscoped read sees only the authorised organisation's attempts.
            orgs = (await session.execute(text("SELECT organisation_id FROM job_attempts"))).all()
            assert {row.organisation_id for row in orgs} == {seed.org_a}
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM job_attempts WHERE id = :id"),
                    {"id": seed.attempt_b},
                )
                == 0
            )
            await session.rollback()

        # An attempt that names a foreign organisation cannot be inserted.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO job_attempts "
                        "(id, job_id, organisation_id, dispatch_id, owner_token, "
                        "attempt_number, status, lease_expires_at, started_at, taken_over) "
                        "VALUES (:id, :job, :org, :dispatch, :owner, 2, 'running', now(), "
                        "now(), false)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "job": seed.job_b,
                        "org": seed.org_b,
                        "dispatch": uuid.uuid4(),
                        "owner": uuid.uuid4(),
                    },
                )
            await session.rollback()

        # The copied key cannot be moved to another organisation.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text("UPDATE job_attempts SET organisation_id = :org WHERE id = :id"),
                    {"org": seed.org_b, "id": seed.attempt_a},
                )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_composite_parent_fk_prevents_a_divergent_attempt_tenant_key(
    migrated_database: str,
) -> None:
    """A matching tenant context still cannot move the copied key off its parent.

    The row is written with the org-B tenant context bound, so its
    ``organisation_id`` satisfies the runtime ``WITH CHECK``; only the composite
    ``(job_id, organisation_id)`` foreign key to the org-A job can reject it.
    """
    seed = await seed_two_organisation_jobs(migrated_database)
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_b)
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO job_attempts "
                        "(id, job_id, organisation_id, dispatch_id, owner_token, "
                        "attempt_number, status, lease_expires_at, started_at, taken_over) "
                        "VALUES (:id, :job, :org, :dispatch, :owner, 3, 'running', now(), "
                        "now(), false)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "job": seed.job_a,
                        "org": seed.org_b,
                        "dispatch": uuid.uuid4(),
                        "owner": uuid.uuid4(),
                    },
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Parent-strategy enforcement on the real runtime role ---


async def test_notification_delivery_parent_policy_mirrors_organisation_and_recipient(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A delivery is visible, and writable, only through its parent notification."""
    seed = await seed_two_organisation_notifications(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a1)
            visible = {
                row.id
                for row in (
                    await session.execute(text("SELECT id FROM notification_deliveries"))
                ).all()
            }
            assert visible == {seed.delivery_a1}
            await session.rollback()

        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a2)
            visible = {
                row.id
                for row in (
                    await session.execute(text("SELECT id FROM notification_deliveries"))
                ).all()
            }
            assert visible == {seed.delivery_a2}
            await session.rollback()

        # A delivery for a foreign parent, or for another recipient in the same
        # organisation, is rejected by the parent-existence WITH CHECK.
        for parent_notification in (seed.notification_b1, seed.notification_a2):
            async with factory() as session:
                await bind_organisation_context(session, seed.org_a)
                await bind_user_context(session, seed.user_a1)
                with pytest.raises(ProgrammingError, match="row-level security"):
                    await session.execute(
                        text(
                            "INSERT INTO notification_deliveries "
                            "(id, notification_id, channel, recipient, "
                            "delivery_identity, status) "
                            "VALUES (:id, :notification, 'email', 'x@example.com', "
                            ":identity, 'queued')"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "notification": parent_notification,
                            "identity": str(uuid.uuid4()),
                        },
                    )
                await session.rollback()
    finally:
        await engine.dispose()


async def test_membership_role_parent_policy_splits_read_from_write(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Grants are tenant-scoped for reads and tenant-checked for writes."""
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    role_ids = await _role_ids(migrated_database)
    try:
        # A tenant context reads only its own grants and cannot grant across the
        # parent boundary.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            visible = {
                row.id
                for row in (await session.execute(text("SELECT id FROM membership_roles"))).all()
            }
            assert visible == {seed.role_grant_a}
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO membership_roles (id, membership_id, role_id) "
                        "VALUES (:id, :membership, :role)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "membership": seed.membership_b,
                        "role": role_ids["viewer"],
                    },
                )
            await session.rollback()

            # A delete of a foreign grant affects no row.
            await bind_organisation_context(session, seed.org_a)
            deleted_foreign = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM membership_roles WHERE id = :id"),
                    {"id": seed.role_grant_b},
                ),
            )
            assert deleted_foreign.rowcount == 0
            await session.rollback()

        # The pre-tenant user context can read its own grant (the membership
        # read policy) but the write policy fails closed with no tenant key.
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            visible = {
                row.id
                for row in (await session.execute(text("SELECT id FROM membership_roles"))).all()
            }
            assert visible == {seed.role_grant_a}
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO membership_roles (id, membership_id, role_id) "
                        "VALUES (:id, :membership, :role)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "membership": seed.membership_a,
                        "role": role_ids["viewer"],
                    },
                )
            await session.rollback()
    finally:
        await engine.dispose()
