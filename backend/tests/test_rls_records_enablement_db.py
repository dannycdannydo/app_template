"""Real-PostgreSQL production enablement suite for the records group (P3, group 0).

Plan P3 / ``docs/rls-rollout.md`` §3 group 0. The P2 prototype
(``c1d2e3f4a5b6``) proved the design; this suite proves the **production
enablement migration** (``d2e3f4a5b6c7``) that promotes the group into the
permanent policy chain:

- the canonical ``<table>_organisation_isolation`` policies are installed with
  matching ``USING``/``WITH CHECK`` and RLS is enabled **and forced** on
  ``records`` and ``record_revisions``;
- default denial: no bound context returns no rows and fails closed for writes;
- both **read and write** policies deny select, insert, update and delete of
  another organisation's rows, including through an unscoped query;
- a foreign row is indistinguishable from a missing one on the real
  runtime-role app path;
- representative query plans use the tenant index; and
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
    RUNTIME_ROLE,
    alembic_config,
    database_reachable,
    downgrade_to_base,
    force_drop_runtime_role,
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_representative_records,
    seed_two_organisation_records,
    upgrade_to_head,
)

from app.db.rls import bind_organisation_context

#: The P2 prototype revision this migration revises.
PROTOTYPE_REVISION = "c1d2e3f4a5b6"

#: Production policy names installed by the enablement migration.
PRODUCTION_POLICIES = {
    "records": "records_organisation_isolation",
    "record_revisions": "record_revisions_organisation_isolation",
}
#: Prototype policy names the enablement migration replaces.
PROTOTYPE_POLICIES = {
    "records": "records_tenant_isolation",
    "record_revisions": "record_revisions_tenant_isolation",
}

_LATENCY_BUDGET_MS = 500.0
_LIST_PAGE_SQL = (
    "SELECT id, title FROM records WHERE organisation_id = :org "
    "ORDER BY created_at DESC, id DESC LIMIT 50"
)
#: The revision history query from ``record_revisions_statement``: bounded by
#: organisation and record, oldest first.
_HISTORY_SQL = (
    "SELECT id, version, action, title, body FROM record_revisions "
    "WHERE organisation_id = :org AND record_id = :rid "
    "ORDER BY created_at, id"
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


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    """One module-local RSA key for minting WorkOS-style tokens."""
    key, _ = generate_key_pair()
    return key


# --- Installed production policy --------------------------------------------


async def test_production_policy_is_installed_and_forced(migrated_database: str) -> None:
    """RLS is enabled and forced, with canonical policies and no prototype residue."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            for table, policy in PRODUCTION_POLICIES.items():
                row = (
                    await connection.execute(
                        text(
                            "SELECT qual, with_check FROM pg_policies "
                            "WHERE tablename = :table AND policyname = :policy"
                        ),
                        {"table": table, "policy": policy},
                    )
                ).one_or_none()
                assert row is not None, f"missing production policy {policy} on {table}"
                assert "app_current_tenant_id()" in row.qual
                assert "app_current_tenant_id()" in row.with_check
                prototype = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_policies "
                        "WHERE tablename = :table AND policyname = :policy"
                    ),
                    {"table": table, "policy": PROTOTYPE_POLICIES[table]},
                )
                assert prototype == 0, f"prototype policy still present on {table}"

            for table in PRODUCTION_POLICIES:
                flags = (
                    await connection.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname = :table"
                        ),
                        {"table": table},
                    )
                ).one()
                assert flags.relrowsecurity is True
                assert flags.relforcerowsecurity is True

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


# --- Default denial ----------------------------------------------------------


async def test_default_denial_without_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """No bound context: zero tenant rows and every write is rejected."""
    await seed_two_organisation_records(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
            assert await session.scalar(text("SELECT count(*) FROM record_revisions")) == 0
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO records (id, organisation_id, title, body, version) "
                        "VALUES (:id, :org, 'x', '', 1)"
                    ),
                    {"id": uuid.uuid4(), "org": uuid.uuid4()},
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Cross-organisation read and write --------------------------------------


async def test_select_is_organisation_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Own rows are visible; a foreign row is invisible, even unscoped."""
    org_a, _org_b, record_a, record_b = await seed_two_organisation_records(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)
            unscoped = (await session.execute(text("SELECT organisation_id FROM records"))).all()
            assert {row.organisation_id for row in unscoped} == {org_a}
            own = await session.scalar(
                text("SELECT id FROM records WHERE id = :id"), {"id": record_a}
            )
            foreign = await session.scalar(
                text("SELECT id FROM records WHERE id = :id"), {"id": record_b}
            )
            assert own == record_a
            assert foreign is None
            await session.rollback()
    finally:
        await engine.dispose()


async def test_insert_update_delete_are_organisation_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A same-tenant write succeeds; every foreign write is denied or affects no row."""
    org_a, org_b, record_a, record_b = await seed_two_organisation_records(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)
            # A same-tenant insert is allowed.
            await session.execute(
                text(
                    "INSERT INTO records (id, organisation_id, title, body, version) "
                    "VALUES (:id, :org, 'own', '', 1)"
                ),
                {"id": uuid.uuid4(), "org": org_a},
            )
            # A mismatched insert is rejected by WITH CHECK.
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO records (id, organisation_id, title, body, version) "
                        "VALUES (:id, :org, 'foreign', '', 1)"
                    ),
                    {"id": uuid.uuid4(), "org": org_b},
                )
            await session.rollback()

        async with factory() as session:
            await bind_organisation_context(session, org_a)
            foreign_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE records SET title = 'hijacked' WHERE id = :id"),
                    {"id": record_b},
                ),
            )
            assert foreign_update.rowcount == 0
            foreign_delete = cast(
                "CursorResult[Any]",
                await session.execute(text("DELETE FROM records WHERE id = :id"), {"id": record_b}),
            )
            assert foreign_delete.rowcount == 0
            own_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE records SET title = 'updated' WHERE id = :id"),
                    {"id": record_a},
                ),
            )
            assert own_update.rowcount == 1
            await session.rollback()
    finally:
        await engine.dispose()

    # The foreign row is untouched when read by an unrestricted owner connection.
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            title = await connection.scalar(
                text("SELECT title FROM records WHERE id = :id"), {"id": record_b}
            )
            assert title == "B record"
    finally:
        await owner_engine.dispose()


async def test_record_revisions_enforce_the_same_boundary(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The append-only revision ledger carries the full production policy.

    Plan P3 requires real select/insert/update/delete evidence for every enabled
    table. The foreign row is invisible, so a foreign UPDATE/DELETE affects no
    row; an authorised mutation reaches the append-only trigger instead, which
    keeps the ledger immutable.
    """
    org_a, org_b, record_a, record_b = await seed_two_organisation_records(migrated_database)
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            revision_a = await connection.scalar(
                text("SELECT id FROM record_revisions WHERE record_id = :rid"),
                {"rid": record_a},
            )
            revision_b = await connection.scalar(
                text("SELECT id FROM record_revisions WHERE record_id = :rid"),
                {"rid": record_b},
            )
    finally:
        await owner_engine.dispose()
    assert revision_a is not None and revision_b is not None

    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)
            rows = (
                await session.execute(text("SELECT organisation_id FROM record_revisions"))
            ).all()
            assert {row.organisation_id for row in rows} == {org_a}
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO record_revisions "
                        "(id, record_id, organisation_id, version, action, title, body) "
                        "VALUES (:id, :rid, :org, 1, 'created', 'foreign', '')"
                    ),
                    {"id": uuid.uuid4(), "rid": uuid.uuid4(), "org": org_b},
                )
            await session.rollback()

        async with factory() as session:
            await bind_organisation_context(session, org_a)
            foreign_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE record_revisions SET title = 'hijacked' WHERE id = :id"),
                    {"id": revision_b},
                ),
            )
            assert foreign_update.rowcount == 0
            foreign_delete = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM record_revisions WHERE id = :id"), {"id": revision_b}
                ),
            )
            assert foreign_delete.rowcount == 0
            await session.rollback()

        # An authorised mutation is visible to the policy but the ledger stays
        # append-only: the database trigger rejects the change. Each mutation
        # uses its own transaction because a failed statement aborts the current
        # one (and rolling back clears the transaction-local context).
        for statement in (
            "UPDATE record_revisions SET title = 'changed' WHERE id = :id",
            "DELETE FROM record_revisions WHERE id = :id",
        ):
            async with factory() as session:
                await bind_organisation_context(session, org_a)
                with pytest.raises(DBAPIError, match="append-only and cannot be modified"):
                    await session.execute(text(statement), {"id": revision_a})
                await session.rollback()
    finally:
        await engine.dispose()

    # The foreign revision is untouched when read with the owner credential.
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            title = await connection.scalar(
                text("SELECT title FROM record_revisions WHERE id = :id"), {"id": revision_b}
            )
            assert title == "B record"
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


async def test_foreign_row_is_indistinguishable_from_missing(
    migrated_database: str,
    runtime_client: AsyncClient,
    private_key: rsa.RSAPrivateKey,
) -> None:
    """A foreign row and an absent row return the identical 404 under RLS.

    Plan P3: application errors must not disclose whether RLS hid a foreign row.
    The service predicate makes both cases a ``record_not_found`` and the policy
    never exposes a different shape.
    """
    world: IsolationWorld = await seed_isolation_world(migrated_database)
    headers = auth_headers(private_key, world.multi, org_id=world.org_a)
    foreign = await runtime_client.get(f"/api/v1/records/{world.record_b}", headers=headers)
    missing = await runtime_client.get(f"/api/v1/records/{uuid.uuid4()}", headers=headers)
    assert foreign.status_code == NOT_FOUND, foreign.text
    assert missing.status_code == NOT_FOUND, missing.text
    assert foreign.json()["code"] == "record_not_found"
    assert missing.json()["code"] == "record_not_found"
    # Only the per-request correlation id may differ: the error envelope itself
    # must not disclose whether the row exists in another organisation.
    assert foreign.json()["message"] == missing.json()["message"]
    assert foreign.json().get("details") == missing.json().get("details")


# --- Representative query plans ---------------------------------------------


async def test_representative_query_plans_use_the_tenant_index(
    migrated_database: str, runtime_database_url: str
) -> None:
    """List, detail and revision-history plans use indexes; no sequential scan."""
    org_a, sample_id = await seed_representative_records(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)
            list_plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(text("EXPLAIN " + _LIST_PAGE_SQL), {"org": org_a})
                ).all()
            )
            detail_plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(
                        text("EXPLAIN SELECT * FROM records WHERE id = :id"),
                        {"id": sample_id},
                    )
                ).all()
            )
            history_plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(
                        text("EXPLAIN " + _HISTORY_SQL), {"org": org_a, "rid": sample_id}
                    )
                ).all()
            )
            assert "Seq Scan" not in list_plan
            assert "ix_records_organisation_id_created_at" in list_plan
            assert "Seq Scan" not in detail_plan
            assert "pk_records" in detail_plan
            assert "Seq Scan" not in history_plan
            assert "ix_record_revisions_record_id_created_at" in history_plan

            loop = asyncio.get_running_loop()
            start = loop.time()
            rows = (await session.execute(text(_LIST_PAGE_SQL), {"org": org_a})).all()
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


def test_group0_migration_downgrade_restores_prototype_and_reupgrades(
    migrated_database: str,
) -> None:
    """One revision down restores the prototype policy on both tables; re-upgrade re-installs."""
    config = alembic_config()
    try:
        command.downgrade(config, PROTOTYPE_REVISION)
        for table in PRODUCTION_POLICIES:
            assert (
                asyncio.run(_policy_count(migrated_database, table, PRODUCTION_POLICIES[table]))
                == 0
            ), f"production policy still present on {table} after downgrade"
            assert (
                asyncio.run(_policy_count(migrated_database, table, PROTOTYPE_POLICIES[table])) == 1
            ), f"prototype policy missing on {table} after downgrade"
            assert asyncio.run(_rls_flags(migrated_database, table)) == (True, True)

        command.upgrade(config, "head")
        for table in PRODUCTION_POLICIES:
            assert (
                asyncio.run(_policy_count(migrated_database, table, PRODUCTION_POLICIES[table]))
                == 1
            ), f"production policy missing on {table} after re-upgrade"
            assert (
                asyncio.run(_policy_count(migrated_database, table, PROTOTYPE_POLICIES[table])) == 0
            ), f"prototype policy still present on {table} after re-upgrade"
            assert asyncio.run(_rls_flags(migrated_database, table)) == (True, True)
    finally:
        command.upgrade(alembic_config(), "head")


# --- Adoption of a deployment-provisioned runtime role ----------------------


#: Test-only intermediate role used to prove the adoption branch breaks
#: indirect ``SET ROLE`` paths, not only direct grants.
BRIDGE_ROLE = "rls_group0_adoption_bridge"

#: Privileged roles reachable from ``app_runtime`` through any membership chain
#: (matching how PostgreSQL resolves ``SET ROLE``).
_REACHABLE_PRIVILEGED_SQL = """
WITH RECURSIVE reachable(roleid) AS (
    SELECT m.roleid
    FROM pg_auth_members m
    JOIN pg_roles member ON member.oid = m.member
    WHERE member.rolname = :runtime
    UNION
    SELECT m.roleid
    FROM reachable
    JOIN pg_auth_members m ON m.member = reachable.roleid
)
SELECT DISTINCT granted.rolname
FROM reachable
JOIN pg_roles granted ON granted.oid = reachable.roleid
WHERE granted.rolsuper
   OR granted.rolbypassrls
   OR EXISTS (
        SELECT 1 FROM pg_class c
        WHERE c.relowner = granted.oid
          AND c.relname IN ('records', 'record_revisions')
   )
"""


async def _reachable_privileged_roles(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(text(_REACHABLE_PRIVILEGED_SQL), {"runtime": RUNTIME_ROLE})
            ).all()
        return {str(row[0]) for row in rows}
    finally:
        await engine.dispose()


async def _create_bridge_chain(database_url: str) -> None:
    """Wire ``app_runtime -> bridge -> protected-table owner`` (owner role)."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            owner = await connection.scalar(
                text("SELECT relowner::regrole::text FROM pg_class WHERE relname = 'records'")
            )
            assert owner is not None
            await connection.execute(text(f"DROP ROLE IF EXISTS {BRIDGE_ROLE}"))
            await connection.execute(text(f"CREATE ROLE {BRIDGE_ROLE}"))
            await connection.execute(text(f'GRANT "{owner}" TO {BRIDGE_ROLE}'))
            await connection.execute(text(f"GRANT {BRIDGE_ROLE} TO {RUNTIME_ROLE}"))
    finally:
        await engine.dispose()


async def _drop_bridge_role(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP ROLE IF EXISTS {BRIDGE_ROLE}"))
    finally:
        await engine.dispose()


def test_group0_adoption_breaks_transitive_privileged_paths(
    migrated_database: str,
) -> None:
    """A pre-existing role cannot reach a privileged role through an intermediate.

    PostgreSQL resolves ``SET ROLE`` through indirect membership chains, so the
    adoption branch must break the entry membership, not only revoke a directly
    granted privileged role. This test sits at the prototype revision, wires
    ``app_runtime -> bridge -> owner``, then applies the group 0 migration.
    """
    config = alembic_config()
    try:
        command.downgrade(config, PROTOTYPE_REVISION)
        asyncio.run(_create_bridge_chain(migrated_database))
        assert asyncio.run(_reachable_privileged_roles(migrated_database)) != set()

        command.upgrade(config, "head")
        assert asyncio.run(_reachable_privileged_roles(migrated_database)) == set()
    finally:
        command.downgrade(config, "base")
        asyncio.run(_drop_bridge_role(migrated_database))
        force_drop_runtime_role(migrated_database)
        command.upgrade(config, "head")
