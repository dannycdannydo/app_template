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
