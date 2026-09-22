"""Real-PostgreSQL production enablement suite for the jobs group (P3, 4b).

Plan P3 / ``docs/rls-rollout.md`` §3 group 4b. It proves the production
enablement migration (``d0e1f2a3b4c5``) that promotes the durable ``jobs`` table
and its attempt ledger ``job_attempts`` into the permanent policy chain, and the
non-bypass ``app_coordinator`` role that group 4b brings forward with it:

- the canonical ``<table>_organisation_isolation`` runtime policy is installed
  with matching ``USING``/``WITH CHECK``, the single-row ``jobs_worker_bootstrap``
  ``FOR SELECT`` policy admits exactly the broker's job id, and RLS is enabled
  **and forced** on both tables;
- ``job_attempts`` carries the denormalised non-null ``organisation_id``
  (ADR-0022 decision 6);
- default denial: no bound context returns no rows and fails closed for writes;
- the runtime role denies select, insert, update and delete of another
  organisation's rows, including through an unscoped query and a tenant-key move;
- the coordinator role reads and settles dispatch state across tenants but
  cannot read a terminal job or insert one, and it holds no ``BYPASSRLS``;
- representative, realistically sized lookups use the org index with no
  sequential scan; and
- the migration upgrades, downgrades one revision and re-upgrades cleanly.

The suite runs against real PostgreSQL with the restricted runtime and
coordinator logins (the migration creates both roles ``NOLOGIN``; the test
grants throwaway credentials). ``migrated_database`` reverts to base at teardown,
so the rollout leaves no residue.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any, cast

import pytest
from alembic import command
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.rls_helpers import (
    COORDINATOR_ROLE,
    RUNTIME_ROLE,
    alembic_config,
    coordinator_url,
    database_reachable,
    downgrade_to_base,
    provision_coordinator_login,
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_representative_jobs,
    seed_two_organisation_jobs,
    upgrade_to_head,
)

from app.db.rls import bind_job_context, bind_organisation_context

#: The group 4a revision this migration revises.
GROUP4A_REVISION = "c9d0e1f2a3b4"

#: Every table enabled by the group 4b migration.
JOBS_TABLES = ("jobs", "job_attempts")


def _runtime_policy(table: str) -> str:
    return f"{table}_organisation_isolation"


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


# --- Installed roles and policies --------------------------------------------


async def test_roles_and_policies_are_installed_and_forced(migrated_database: str) -> None:
    """Both tables are forced under the canonical policies and roles are safe."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            for table in JOBS_TABLES:
                row = (
                    await connection.execute(
                        text(
                            "SELECT qual, with_check FROM pg_policies "
                            "WHERE tablename = :table AND policyname = :policy"
                        ),
                        {"table": table, "policy": _runtime_policy(table)},
                    )
                ).one_or_none()
                assert row is not None, f"missing production policy on {table}"
                assert "app_current_tenant_id()" in row.qual
                assert "app_current_tenant_id()" in row.with_check

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

            bootstrap = (
                await connection.execute(
                    text(
                        "SELECT qual, with_check FROM pg_policies "
                        "WHERE tablename = 'jobs' AND policyname = 'jobs_worker_bootstrap'"
                    )
                )
            ).one_or_none()
            assert bootstrap is not None
            assert "app_current_job_id()" in bootstrap.qual
            # The bootstrap is SELECT-only: it must never carry a WITH CHECK,
            # because that would authorise a real UPDATE by job id.
            assert bootstrap.with_check is None

            lock_policy = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE tablename = 'jobs' AND policyname = 'jobs_worker_bootstrap_lock'"
                )
            )
            assert lock_policy == 0

            settle = (
                await connection.execute(
                    text(
                        "SELECT with_check FROM pg_policies "
                        "WHERE tablename = 'jobs' "
                        "AND policyname = 'jobs_coordinator_dispatch_settle'"
                    )
                )
            ).one_or_none()
            assert settle is not None
            assert "'queued'" in settle.with_check
            assert "'failed'" in settle.with_check

            column = (
                await connection.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'job_attempts' AND column_name = 'organisation_id'"
                    )
                )
            ).one_or_none()
            assert column is not None, "job_attempts.organisation_id missing"
            assert column.is_nullable == "NO"

            for role in (RUNTIME_ROLE, COORDINATOR_ROLE):
                attributes = (
                    await connection.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                            "FROM pg_roles WHERE rolname = :role"
                        ),
                        {"role": role},
                    )
                ).one()
                assert attributes.rolsuper is False, role
                assert attributes.rolbypassrls is False, role
                assert attributes.rolcreatedb is False, role
                assert attributes.rolcreaterole is False, role
    finally:
        await engine.dispose()


async def test_representative_query_plans_use_the_index(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The org-scoped jobs list uses its composite index, with no sequential scan."""
    org_a, _ = await seed_representative_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)
            plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "EXPLAIN SELECT * FROM jobs WHERE organisation_id = :org "
                            "ORDER BY created_at DESC LIMIT 50"
                        ),
                        {"org": org_a},
                    )
                ).all()
            )
            assert "Seq Scan" not in plan
            assert "ix_jobs_organisation_id_created_at" in plan, plan
            await session.rollback()
    finally:
        await engine.dispose()


# --- Default denial ----------------------------------------------------------


async def test_default_denial_without_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """No bound context: zero jobs/attempts rows and every write is rejected."""
    await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for table in JOBS_TABLES:
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO jobs "
                        "(id, organisation_id, job_type, status, progress, input_reference) "
                        "VALUES (:id, :org, 'file.processing', 'queued', 0, 'ref')"
                    ),
                    {"id": uuid.uuid4(), "org": uuid.uuid4()},
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Cross-organisation read and write ---------------------------------------


async def test_select_is_organisation_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Own rows are visible; a foreign row is invisible even through an unscoped query."""
    seed = await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            for table in JOBS_TABLES:
                orgs = (await session.execute(text(f"SELECT organisation_id FROM {table}"))).all()
                assert {row.organisation_id for row in orgs} == {seed.org_a}, table
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM jobs WHERE id = :id"), {"id": seed.job_b}
                )
                == 0
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM job_attempts WHERE id = :id"),
                    {"id": seed.attempt_b},
                )
                == 0
            )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_insert_update_delete_and_tenant_key_move_are_denied(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Same-tenant writes succeed; every cross-tenant write is denied."""
    seed = await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # A mismatched insert is rejected by WITH CHECK on both tables.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO jobs "
                        "(id, organisation_id, job_type, status, progress, input_reference) "
                        "VALUES (:id, :org, 'file.processing', 'queued', 0, 'ref')"
                    ),
                    {"id": uuid.uuid4(), "org": seed.org_b},
                )
            await session.rollback()
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

        # Same-tenant insert succeeds (the job id is UUID-generated client-side).
        own_delete_job = uuid.uuid4()
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await session.execute(
                text(
                    "INSERT INTO jobs "
                    "(id, organisation_id, job_type, status, progress, input_reference) "
                    "VALUES (:id, :org, 'file.processing', 'queued', 0, 'ref')"
                ),
                {"id": own_delete_job, "org": seed.org_a},
            )
            await session.commit()

        # Same-tenant update/delete succeed; foreign writes affect no row. The
        # seeded jobs have attempt rows (FK RESTRICT), so the delete proof uses
        # the attempt-free job inserted above.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            for target_id, expected in ((seed.job_a, 1), (seed.job_b, 0)):
                updated = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        text("UPDATE jobs SET progress = 50 WHERE id = :id"),
                        {"id": target_id},
                    ),
                )
                assert updated.rowcount == expected, target_id
            deleted_own = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM jobs WHERE id = :id"), {"id": own_delete_job}
                ),
            )
            assert deleted_own.rowcount == 1
            deleted_foreign = cast(
                "CursorResult[Any]",
                await session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": seed.job_b}),
            )
            assert deleted_foreign.rowcount == 0
            await session.rollback()

        # The attempt ledger enforces the same boundary directly.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            for target_id, expected in ((seed.attempt_a, 1), (seed.attempt_b, 0)):
                updated = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        text("UPDATE job_attempts SET status = 'succeeded' WHERE id = :id"),
                        {"id": target_id},
                    ),
                )
                assert updated.rowcount == expected, target_id
                deleted = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        text("DELETE FROM job_attempts WHERE id = :id"), {"id": target_id}
                    ),
                )
                assert deleted.rowcount == expected, target_id
            await session.rollback()

        # Moving a same-tenant row into another tenant is rejected.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text("UPDATE jobs SET organisation_id = :org WHERE id = :id"),
                    {"org": seed.org_b, "id": seed.job_a},
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Attempt parent/tenant consistency ---------------------------------------


async def test_attempt_tenant_key_is_tied_to_parent_job(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The composite ``(job_id, organisation_id)`` FK rejects a mismatched parent.

    A session bound to organisation A can satisfy the attempt's direct RLS
    check with an A tenant key while naming a B job id; the composite parent FK
    is the control that rejects that pair. A direct (owner-role) tenant-key
    update cannot break the pair either.
    """
    seed = await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(DBAPIError):
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
                        "org": seed.org_a,
                        "dispatch": uuid.uuid4(),
                        "owner": uuid.uuid4(),
                    },
                )
            await session.rollback()
    finally:
        await engine.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.begin() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text("UPDATE job_attempts SET organisation_id = :org WHERE id = :id"),
                    {"org": seed.org_b, "id": seed.attempt_a},
                )
    finally:
        await owner_engine.dispose()


# --- Worker bootstrap --------------------------------------------------------


async def test_worker_bootstrap_reads_only_its_durable_job(
    migrated_database: str, runtime_database_url: str
) -> None:
    """``app.job_id`` admits exactly one job and no attempt or foreign row.

    The worker is handed one opaque id by the broker; the bootstrap policy must
    let it read that durable row and nothing else, and it must not become a
    tenant authority (the attempt ledger still needs the organisation context).
    """
    seed = await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_job_context(session, seed.job_a)
            rows = (await session.execute(text("SELECT id FROM jobs"))).all()
            assert {row.id for row in rows} == {seed.job_a}
            assert await session.scalar(text("SELECT count(*) FROM job_attempts")) == 0
            await session.rollback()
    finally:
        await engine.dispose()


async def test_worker_bootstrap_job_context_cannot_update_or_move(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Binding only ``app.job_id`` grants no update authority on the named row.

    The bootstrap has no UPDATE policy, so a caller who knows a job id can read
    that one row but cannot change its progress, and cannot move its tenant key
    into another organisation.
    """
    seed = await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_job_context(session, seed.job_a)
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM jobs WHERE id = :id"), {"id": seed.job_a}
                )
                == 1
            )
            for statement, params in (
                ("UPDATE jobs SET progress = 50 WHERE id = :id", {"id": seed.job_a}),
                (
                    "UPDATE jobs SET organisation_id = :org WHERE id = :id",
                    {"org": seed.org_b, "id": seed.job_a},
                ),
            ):
                result = cast("CursorResult[Any]", await session.execute(text(statement), params))
                assert result.rowcount == 0, statement
            await session.rollback()
    finally:
        await engine.dispose()


async def test_worker_bootstrap_clears_job_context_before_tenant_phase(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The worker helper clears ``app.job_id`` and binds the durable row's org."""
    from app.modules.jobs import service as jobs_service

    seed = await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            job = await jobs_service.get_job_for_task(session, job_id=seed.job_a)
            assert job.id == seed.job_a
            job_setting = await session.scalar(text("SELECT current_setting('app.job_id', true)"))
            org_setting = await session.scalar(
                text("SELECT current_setting('app.organisation_id', true)")
            )
            assert job_setting in (None, "")
            assert org_setting == str(seed.org_a)
            await session.rollback()
    finally:
        await engine.dispose()


async def test_worker_bootstrap_empty_and_malformed_context_fail_closed(
    migrated_database: str, runtime_database_url: str
) -> None:
    """An empty or malformed ``app.job_id`` resolves to no rows (fail closed)."""
    await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for value in ("", "not-a-uuid"):
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.job_id', :value, true)"), {"value": value}
                )
                assert await session.scalar(text("SELECT count(*) FROM jobs")) == 0
                await session.rollback()
    finally:
        await engine.dispose()


# --- Coordinator dispatch-state access ---------------------------------------


async def test_coordinator_reads_and_settles_dispatch_state(
    migrated_database: str, coordinator_database_url: str
) -> None:
    """The coordinator's read scope is dispatch identity, not tenant or status.

    It may read any row that retains a dispatch identity — including a
    **terminal** job that still carries one, the deliberate boundary that lets
    it resolve a late dispatch event instead of declaring it dead — but not a
    terminal row with no dispatch identity. Its update is confined to the
    settlement columns of an in-flight row.
    """
    seed = await seed_two_organisation_jobs(migrated_database)
    terminal_with_dispatch = uuid.uuid4()
    terminal_without_dispatch = uuid.uuid4()
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO jobs "
                    "(id, organisation_id, job_type, status, progress, input_reference, "
                    "dispatch_id, owner_token) "
                    "VALUES (:id, :org, 'file.processing', 'succeeded', 100, 'ref', "
                    ":dispatch, :owner)"
                ),
                {
                    "id": terminal_with_dispatch,
                    "org": seed.org_a,
                    "dispatch": uuid.uuid4(),
                    "owner": uuid.uuid4(),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO jobs "
                    "(id, organisation_id, job_type, status, progress, input_reference) "
                    "VALUES (:id, :org, 'file.processing', 'succeeded', 100, 'ref')"
                ),
                {"id": terminal_without_dispatch, "org": seed.org_a},
            )
    finally:
        await owner_engine.dispose()
    engine = runtime_engine(coordinator_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            visible = (await session.execute(text("SELECT id, status FROM jobs"))).all()
            visible_ids = {row.id for row in visible}
            assert seed.job_a in visible_ids
            assert seed.job_b in visible_ids
            # A terminal row that retains a dispatch identity stays visible by
            # design; one with no dispatch identity does not.
            assert terminal_with_dispatch in visible_ids
            assert terminal_without_dispatch not in visible_ids

            # A legitimate settlement-column update on an in-flight row works.
            updated = cast(
                "CursorResult[Any]",
                await session.execute(
                    text(
                        "UPDATE jobs SET status = 'failed', error_code = 'coordinator', "
                        "completed_at = now(), execution_lease_expires_at = NULL "
                        "WHERE id = :id"
                    ),
                    {"id": seed.job_a},
                ),
            )
            assert updated.rowcount == 1
            await session.commit()

        # A terminal row is outside the settlement policy: the update touches no
        # row even though the row is visible.
        async with factory() as session:
            updated = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE jobs SET status = 'failed' WHERE id = :id"),
                    {"id": terminal_with_dispatch},
                ),
            )
            assert updated.rowcount == 0
            await session.rollback()

        # The coordinator has no INSERT privilege/policy on jobs.
        async with factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        "INSERT INTO jobs "
                        "(id, organisation_id, job_type, status, progress, input_reference) "
                        "VALUES (:id, :org, 'file.processing', 'queued', 0, 'ref')"
                    ),
                    {"id": uuid.uuid4(), "org": seed.org_a},
                )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_coordinator_cannot_update_tenant_payload_or_ownership_columns(
    migrated_database: str, coordinator_database_url: str
) -> None:
    """Column-level grants deny every non-settlement column on jobs/attempts."""
    seed = await seed_two_organisation_jobs(migrated_database)
    engine = runtime_engine(coordinator_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements = [
        ("UPDATE jobs SET organisation_id = :value WHERE id = :id", seed.org_b, seed.job_a),
        ("UPDATE jobs SET input_reference = :value WHERE id = :id", "x", seed.job_a),
        ("UPDATE jobs SET result_reference = :value WHERE id = :id", "x", seed.job_a),
        ("UPDATE jobs SET job_type = :value WHERE id = :id", "x", seed.job_a),
        ("UPDATE jobs SET progress = :value WHERE id = :id", 10, seed.job_a),
        ("UPDATE jobs SET created_by_user_id = :value WHERE id = :id", seed.org_a, seed.job_a),
        ("UPDATE jobs SET attempt_count = :value WHERE id = :id", 9, seed.job_a),
        (
            "UPDATE job_attempts SET organisation_id = :value WHERE id = :id",
            seed.org_b,
            seed.attempt_a,
        ),
        (
            "UPDATE job_attempts SET job_id = :value WHERE id = :id",
            seed.job_b,
            seed.attempt_a,
        ),
        (
            "UPDATE job_attempts SET owner_token = :value WHERE id = :id",
            uuid.uuid4(),
            seed.attempt_a,
        ),
        (
            "UPDATE job_attempts SET dispatch_id = :value WHERE id = :id",
            uuid.uuid4(),
            seed.attempt_a,
        ),
        (
            "UPDATE job_attempts SET attempt_number = :value WHERE id = :id",
            9,
            seed.attempt_a,
        ),
    ]
    try:
        for statement, value, target in statements:
            async with factory() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement), {"value": value, "id": target})
                await session.rollback()
    finally:
        await engine.dispose()


# --- Coordinator settlement path ---------------------------------------------


async def test_coordinator_settles_a_ceiling_job_and_writes_audit(
    migrated_database: str, coordinator_database_url: str
) -> None:
    """The coordinator's bounded recovery fails an exhausted job under RLS.

    The settlement closes the running attempt, flips the job to ``failed`` and
    writes its audit event in one coordinator transaction, so this proves the
    dispatch-state UPDATE policies and the auditor/attempt grants are load
    bearing without any ``BYPASSRLS``.
    """
    from sqlalchemy import select

    from app.modules.jobs import service as jobs_service
    from app.modules.jobs.models import Job, JobStatus

    seed = await seed_two_organisation_jobs(migrated_database)
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE jobs SET attempt_count = :count WHERE id = :id"),
                {"count": jobs_service.MAX_ATTEMPTS, "id": seed.job_a},
            )
    finally:
        await owner_engine.dispose()

    engine = runtime_engine(coordinator_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            job = await session.scalar(select(Job).where(Job.id == seed.job_a))
            assert job is not None
            settled = await jobs_service.enforce_attempt_ceiling_locked(session, job)
            assert settled is True
            await session.commit()
    finally:
        await engine.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    try:
        async with owner_factory() as session:
            persisted = await session.get(Job, seed.job_a)
            assert persisted is not None
            assert persisted.status is JobStatus.FAILED
            audit = await session.scalar(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE resource_type = 'job' AND resource_id = :id"
                ),
                {"id": str(seed.job_a)},
            )
            assert audit == 1
    finally:
        await owner_engine.dispose()


# --- Worker leases, retries and reconciliation under enforced RLS -------------
#
# Plan P3 aggregate checklist: "Prove retries, leases, reconciliation and outbox
# dispatch work without a bypass role." The job lifecycle suites prove those
# paths on the owner credential; these tests prove the same paths on the two
# restricted logins the rollout provisions — the non-owner ``app_runtime``
# worker and the non-bypass ``app_coordinator`` recovery role — under the
# enforced ``jobs``/``job_attempts``/``outbox_events`` policies.


async def _create_runtime_organisation(owner_url: str, name: str) -> uuid.UUID:
    """Create one organisation with the owner credential (RLS-bypassing seed)."""
    organisation_id = uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:id, :name)"),
                {"id": organisation_id, "name": name},
            )
    finally:
        await engine.dispose()
    return organisation_id


async def _schedule_and_claim_under_runtime(
    runtime_database_url: str, organisation_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Schedule and claim a durable job through the restricted runtime role."""
    from app.modules.jobs import service as jobs_service

    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, organisation_id)
            job = await jobs_service.schedule_job(
                session,
                organisation_id=organisation_id,
                job_type="file.processing",
                input_reference=f"lease-{uuid.uuid4().hex[:8]}",
            )
            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert claim.outcome == jobs_service.ClaimOutcome.CLAIMED
            assert claim.owner_token is not None
            assert claim.dispatch_id is not None
            return job.id, claim.owner_token, claim.dispatch_id
    finally:
        await engine.dispose()


async def _expire_execution_lease(owner_url: str, job_id: uuid.UUID) -> None:
    """Simulate a dead worker by expiring the job's execution lease (owner seed)."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE jobs SET execution_lease_expires_at = now() - interval '1 second' "
                    "WHERE id = :id"
                ),
                {"id": job_id},
            )
    finally:
        await engine.dispose()


async def _make_dispatch_due(owner_url: str, dispatch_id: uuid.UUID) -> None:
    """Move a dispatch's due time into the past (owner seed; models elapsed time)."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE outbox_events SET available_at = now() - interval '1 second' "
                    "WHERE id = :id"
                ),
                {"id": dispatch_id},
            )
    finally:
        await engine.dispose()


async def _dispatch_status(owner_url: str, dispatch_id: uuid.UUID) -> str:
    """Return one dispatch's status with the owner credential."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text("SELECT status FROM outbox_events WHERE id = :id"), {"id": dispatch_id}
            )
        return str(value)
    finally:
        await engine.dispose()


async def _job_state(owner_url: str, job_id: uuid.UUID) -> tuple[str, uuid.UUID | None, int, bool]:
    """Return ``(status, dispatch_id, attempt_count, lease_set)`` (owner read)."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status, dispatch_id, attempt_count, "
                        "execution_lease_expires_at FROM jobs WHERE id = :id"
                    ),
                    {"id": job_id},
                )
            ).one()
        return (
            str(row.status),
            row.dispatch_id,
            int(row.attempt_count),
            row.execution_lease_expires_at is not None,
        )
    finally:
        await engine.dispose()


async def _job_owner_token(owner_url: str, job_id: uuid.UUID) -> uuid.UUID | None:
    """Return one job's owner token with the owner credential (fencing read)."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text("SELECT owner_token FROM jobs WHERE id = :id"), {"id": job_id}
            )
        return value
    finally:
        await engine.dispose()


async def _database_now(owner_url: str) -> datetime:
    """Return the database clock the coordinator compares against.

    ``run_cycle`` derives ``now`` from ``SELECT now()`` and the seeds stamp
    their rows against the same clock, so the test must not mix in the host
    clock (the review's consistency finding).
    """
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(text("SELECT now()"))
        assert isinstance(value, datetime)
        return value
    finally:
        await engine.dispose()


async def _attempt_history(owner_url: str, job_id: uuid.UUID) -> list[tuple[int, str, bool]]:
    """Return ``(attempt_number, status, taken_over)`` for one job (owner read)."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT attempt_number, status, taken_over FROM job_attempts "
                        "WHERE job_id = :id ORDER BY attempt_number"
                    ),
                    {"id": job_id},
                )
            ).all()
        return [(int(row.attempt_number), str(row.status), bool(row.taken_over)) for row in rows]
    finally:
        await engine.dispose()


async def _seed_stranded_reconciliation_jobs(
    owner_url: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed one stranded queued job and one lease-expired running job (owner).

    Both jobs' current dispatch is already published and older than the
    reconciliation cutoff, so the non-bypass coordinator's recovery sweeps
    select them. Returns ``(queued_job, queued_event, running_job,
    running_event, running_token)``; the token lets the caller prove lease
    recovery rotated the dead worker's credential (the fencing property).
    """
    org = uuid.uuid4()
    queued_job, running_job = uuid.uuid4(), uuid.uuid4()
    queued_event, running_event = uuid.uuid4(), uuid.uuid4()
    running_attempt = uuid.uuid4()
    running_token = uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:id, 'Stranded jobs')"),
                {"id": org},
            )
            await connection.execute(
                text(
                    "INSERT INTO jobs "
                    "(id, organisation_id, job_type, status, progress, input_reference, "
                    "dispatch_id, owner_token, attempt_count, execution_lease_expires_at) "
                    "VALUES (:id, :org, 'file.processing', 'queued', 0, 'stranded', "
                    ":dispatch, NULL, 0, NULL), "
                    "(:rid, :org, 'file.processing', 'running', 0, 'expired', "
                    ":rdispatch, :rtoken, 1, now() - interval '1 second')"
                ),
                {
                    "id": queued_job,
                    "rid": running_job,
                    "org": org,
                    "dispatch": queued_event,
                    "rdispatch": running_event,
                    "rtoken": running_token,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO outbox_events "
                    "(id, organisation_id, event_type, event_version, aggregate_type, "
                    "aggregate_id, payload, deduplication_key, status, available_at, "
                    "processed_at) "
                    "VALUES (:id, :org, 'job.dispatch_requested', 1, 'job', :agg, "
                    "CAST(:payload AS jsonb), :dedup, 'published', "
                    "now() - interval '1000 seconds', now() - interval '1000 seconds')"
                ),
                [
                    {
                        "id": queued_event,
                        "org": org,
                        "agg": queued_job,
                        "payload": f'{{"job_id": "{queued_job}"}}',
                        "dedup": f"stranded:{queued_event}",
                    },
                    {
                        "id": running_event,
                        "org": org,
                        "agg": running_job,
                        "payload": f'{{"job_id": "{running_job}"}}',
                        "dedup": f"stranded:{running_event}",
                    },
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO job_attempts "
                    "(id, job_id, organisation_id, dispatch_id, owner_token, "
                    "attempt_number, status, lease_expires_at, started_at, taken_over) "
                    "VALUES (:id, :job, :org, :dispatch, :token, 1, 'running', "
                    "now() - interval '1 second', now() - interval '1 hour', false)"
                ),
                {
                    "id": running_attempt,
                    "job": running_job,
                    "org": org,
                    "dispatch": running_event,
                    "token": running_token,
                },
            )
    finally:
        await engine.dispose()
    return queued_job, queued_event, running_job, running_event, running_token


async def test_worker_takes_over_an_expired_lease_under_enforced_rls(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A restricted-role duplicate takes a dead attempt over without a bypass.

    Plan P3 aggregate. The worker path is a self-committing service on the
    non-owner ``app_runtime`` login: it schedules and claims a durable job, the
    lease expires while the row stays ``running``, and a duplicate message takes
    the dead attempt over — rotating the owner token, retaining the dispatch
    identity, abandoning the dead attempt and opening a fresh one — all under
    the enforced ``jobs``/``job_attempts`` policies.
    """
    from app.modules.jobs import service as jobs_service

    org = await _create_runtime_organisation(migrated_database, "Lease takeover")
    job_id, first_token, first_dispatch = await _schedule_and_claim_under_runtime(
        runtime_database_url, org
    )
    assert await _attempt_history(migrated_database, job_id) == [(1, "running", False)]

    await _expire_execution_lease(migrated_database, job_id)

    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            takeover = await jobs_service.claim_dispatch(session, job_id=job_id)
            assert takeover.outcome == jobs_service.ClaimOutcome.CLAIMED
            assert takeover.taken_over is True
            assert takeover.dispatch_id == first_dispatch
            assert takeover.owner_token is not None
            assert takeover.owner_token != first_token
            assert takeover.job is not None
            assert takeover.job.attempt_count == 2
    finally:
        await engine.dispose()

    status, dispatch_id, attempt_count, lease_set = await _job_state(migrated_database, job_id)
    assert (status, dispatch_id, attempt_count, lease_set) == (
        "running",
        first_dispatch,
        2,
        True,
    )
    assert await _attempt_history(migrated_database, job_id) == [
        (1, "abandoned", False),
        (2, "running", True),
    ]


async def test_transient_failure_retry_is_durable_under_enforced_rls(
    migrated_database: str, runtime_database_url: str, coordinator_database_url: str
) -> None:
    """A restricted-role retry is durably queued, published and re-claimed.

    Plan P3 aggregate. The runtime worker settles a transient failure into a
    replacement dispatch without a bypass; the non-bypass coordinator's real
    ``run_cycle`` claims the due dispatch (``FOR UPDATE SKIP LOCKED``), reads the
    job aggregate under its own policy, publishes it through the allow-listed
    registry and settles it owner-checked; and the runtime re-claims the
    published retry as a new attempt. The whole retry loop therefore runs on the
    two restricted credentials the rollout provisions.
    """
    from app.job_coordinator.loop import run_cycle
    from app.job_coordinator.registry import DispatchRegistry
    from app.modules.jobs import service as jobs_service
    from app.modules.jobs.models import JobStatus

    org = await _create_runtime_organisation(migrated_database, "Retry dispatch")
    job_id, first_token, _ = await _schedule_and_claim_under_runtime(runtime_database_url, org)

    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            decision = await jobs_service.settle_retryable_failure(
                session, job_id=job_id, owner_token=first_token
            )
            assert decision is JobStatus.QUEUED
    finally:
        await engine.dispose()

    status, retry_dispatch, attempt_count, lease_set = await _job_state(migrated_database, job_id)
    assert status == "queued"
    assert retry_dispatch is not None
    assert (attempt_count, lease_set) == (1, False)
    assert await _attempt_history(migrated_database, job_id) == [(1, "retry_scheduled", False)]
    assert await _dispatch_status(migrated_database, retry_dispatch) == "pending"

    # Time passes and the coordinator owns the dispatch lifecycle: the real
    # publish cycle runs on the non-bypass ``app_coordinator`` login, exercising
    # the claim, the job-aggregate read under the coordinator policy and the
    # owner-checked settle, not the lifecycle columns directly. Earlier tests in
    # the module may leave other due pending rows, so assert on this dispatch's
    # row and on membership in the recorded sends rather than exact counts.
    await _make_dispatch_due(migrated_database, retry_dispatch)

    recorded_sends: list[dict[str, Any]] = []

    class _RecordingTarget:
        def send(self, **kwargs: Any) -> None:
            recorded_sends.append(kwargs)

    registry = DispatchRegistry(
        job_actors={"file.processing": _RecordingTarget()}, maintenance_actors={}
    )
    coordinator_engine = create_async_engine(coordinator_database_url, poolclass=NullPool)
    coordinator_factory = async_sessionmaker(coordinator_engine, expire_on_commit=False)
    try:
        stats = await run_cycle(
            coordinator_factory,
            registry=registry,
            batch_size=50,
            publication_lease_seconds=60,
        )
    finally:
        await coordinator_engine.dispose()
    assert stats.published >= 1
    assert {"job_id": str(job_id)} in recorded_sends
    assert await _dispatch_status(migrated_database, retry_dispatch) == "published"

    # The worker re-claims the published retry as its second attempt.
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            retried = await jobs_service.claim_dispatch(session, job_id=job_id)
            assert retried.outcome == jobs_service.ClaimOutcome.CLAIMED
            assert retried.taken_over is False
            assert retried.job is not None
            assert retried.job.attempt_count == 2
    finally:
        await engine.dispose()

    assert await _attempt_history(migrated_database, job_id) == [
        (1, "retry_scheduled", False),
        (2, "running", False),
    ]


async def test_coordinator_reconciles_stranded_jobs_under_enforced_rls(
    migrated_database: str, coordinator_database_url: str
) -> None:
    """The non-bypass coordinator recovers stranded queued and expired-running jobs.

    Plan P3 aggregate. Each stranded job gets exactly one replacement pending
    dispatch; the expired running job's dead attempt is abandoned, its owner
    token is rotated and it returns to ``queued`` — all under the enforced
    policies on the restricted ``app_coordinator`` login.
    """
    from app.job_coordinator.reconciliation import (
        reconcile_queued_jobs,
        reconcile_running_jobs,
    )

    (
        queued_job,
        queued_event,
        running_job,
        running_event,
        running_token,
    ) = await _seed_stranded_reconciliation_jobs(migrated_database)
    # Reconciliation compares its cutoff against the database clock (and the
    # seeds stamp their rows against it), so take ``now`` from the database
    # rather than the host clock.
    now = await _database_now(migrated_database)
    # The candidate statements deliberately carry no ORDER BY, so the limit only
    # bounds the pass: the membership assertions below hold while the module
    # database holds fewer than 50 stranded candidates (it migrates from base).
    engine = runtime_engine(coordinator_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            reconciled = await reconcile_queued_jobs(
                session,
                now=now,
                threshold_seconds=900,
                cooldown_seconds=900,
                limit=50,
            )
            assert queued_job in reconciled

        async with factory() as session:
            recovered = await reconcile_running_jobs(
                session,
                now=now,
                threshold_seconds=900,
                cooldown_seconds=900,
                limit=50,
            )
            assert running_job in recovered
    finally:
        await engine.dispose()

    queued_status, queued_replacement, _, _ = await _job_state(migrated_database, queued_job)
    assert queued_status == "queued"
    assert queued_replacement not in (None, queued_event)
    assert await _dispatch_status(migrated_database, queued_replacement) == "pending"

    running_status, running_replacement, running_attempts, running_lease = await _job_state(
        migrated_database, running_job
    )
    assert running_status == "queued"
    assert running_replacement not in (None, running_event)
    assert (running_attempts, running_lease) == (1, False)
    assert await _dispatch_status(migrated_database, running_replacement) == "pending"
    assert await _attempt_history(migrated_database, running_job) == [(1, "abandoned", False)]
    # Recovery rotates the owner token, so the dead worker's captured credential
    # can never mutate the recovered job (the fencing property of lease recovery).
    recovered_token = await _job_owner_token(migrated_database, running_job)
    assert recovered_token is not None
    assert recovered_token != running_token


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


async def _column_exists(database_url: str, table: str, column: str) -> bool:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :column"
                ),
                {"table": table, "column": column},
            )
        return bool(value)
    finally:
        await engine.dispose()


def test_group4b_migration_downgrade_and_reupgrade(migrated_database: str) -> None:
    """One revision down removes only the jobs group; re-upgrade re-installs."""
    config = alembic_config()
    try:
        command.downgrade(config, GROUP4A_REVISION)
        for table in JOBS_TABLES:
            assert (
                asyncio.run(_policy_count(migrated_database, table, _runtime_policy(table))) == 0
            ), table
            assert asyncio.run(_rls_flags(migrated_database, table)) == (False, False), table
        assert not asyncio.run(_column_exists(migrated_database, "job_attempts", "organisation_id"))
        # The earlier group-4a settings policies stay intact.
        assert (
            asyncio.run(
                _policy_count(
                    migrated_database,
                    "organisation_ai_settings",
                    "organisation_ai_settings_organisation_isolation",
                )
            )
            == 1
        )
        assert asyncio.run(_rls_flags(migrated_database, "organisation_ai_settings")) == (
            True,
            True,
        )

        command.upgrade(config, "head")
        for table in JOBS_TABLES:
            assert (
                asyncio.run(_policy_count(migrated_database, table, _runtime_policy(table))) == 1
            ), table
            assert asyncio.run(_rls_flags(migrated_database, table)) == (True, True), table
        assert asyncio.run(_column_exists(migrated_database, "job_attempts", "organisation_id"))
    finally:
        command.upgrade(alembic_config(), "head")


# --- Restricted credentials --------------------------------------------------


async def test_runtime_and_coordinator_cannot_disable_policy_or_alter_schema(
    migrated_database: str, runtime_database_url: str, coordinator_database_url: str
) -> None:
    """Neither restricted role can weaken the jobs policies or alter the schema."""
    for database_url in (runtime_database_url, coordinator_database_url):
        engine = runtime_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                statements = ["ALTER ROLE app_runtime BYPASSRLS"]
                for table in JOBS_TABLES:
                    statements.extend(
                        [
                            f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
                            f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
                            f"DROP POLICY {_runtime_policy(table)} ON {table}",
                            f"ALTER TABLE {table} ADD COLUMN hacked integer",
                        ]
                    )
                for statement in statements:
                    with pytest.raises(DBAPIError):
                        await session.execute(text(statement))
                    await session.rollback()
        finally:
            await engine.dispose()
