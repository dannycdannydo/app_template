"""Shared real-PostgreSQL helpers for the RLS rollout suites (plan P3).

``test_rls_records_db.py`` proved the P2 prototype with module-local helpers.
The production group rollouts (``docs/rls-rollout.md`` §3) each need the same
scaffolding against the restricted ``app_runtime`` login: reachability probing,
a throwaway credential for the ``NOLOGIN`` role, a runtime-role engine, and a
two-organisation seed. Those are collected here so a group suite stays focused
on the behaviour it is proving.

The seeding helpers deliberately connect with the **owner** credential
(``DATABASE_URL``): a seed must be able to write foreign rows that the runtime
role is denied, and it must run with RLS bypassed the way a migration does.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: The restricted, non-owner, non-``BYPASSRLS`` runtime role (ADR-0022
#: decision 2). The P2 prototype creates it ``NOLOGIN``; a deployment grants
#: its own credential out of band.
RUNTIME_ROLE = "app_runtime"

#: Test-only throwaway credential attached to the ``NOLOGIN`` role. The
#: migrations deliberately embed no secret, so a real runtime login can be
#: exercised the way a deployment would.
RUNTIME_PASSWORD = "rls-group-test-password"


def alembic_config() -> Config:
    """Return an Alembic ``Config`` pointed at the backend project."""
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return config


def database_reachable(database_url: str) -> bool:
    """Probe ``database_url`` with a short async engine connect."""

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


def runtime_url(owner_url: str) -> str:
    """Return the test database URL for the restricted runtime login."""
    return (
        make_url(owner_url)
        .set(username=RUNTIME_ROLE, password=RUNTIME_PASSWORD)
        .render_as_string(hide_password=False)
    )


def provision_runtime_login(owner_url: str) -> None:
    """Grant the prototype/rollout role a login credential (idempotent)."""

    async def _run() -> None:
        engine = create_async_engine(owner_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f"ALTER ROLE {RUNTIME_ROLE} LOGIN PASSWORD '{RUNTIME_PASSWORD}'")
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def runtime_engine(url: str, *, pooled: bool = False) -> AsyncEngine:
    """Build a runtime-role engine; a single-connection pool enables reuse proof."""
    if pooled:
        return create_async_engine(
            url,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=1,
            max_overflow=0,
        )
    return create_async_engine(url, poolclass=NullPool)


async def seed_two_organisation_records(
    owner_url: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed two organisations, one record and one revision each (owner role)."""
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    record_a, record_b = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:a, :an), (:b, :bn)"),
                {"a": org_a, "an": "Group A", "b": org_b, "bn": "Group B"},
            )
            await connection.execute(
                text(
                    "INSERT INTO records (id, organisation_id, title, body, version) "
                    "VALUES (:a, :ao, 'A record', '', 1), (:b, :bo, 'B record', '', 1)"
                ),
                {"a": record_a, "ao": org_a, "b": record_b, "bo": org_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO record_revisions "
                    "(id, record_id, organisation_id, version, action, title, body) "
                    "VALUES (:ra, :a, :ao, 1, 'created', 'A record', ''), "
                    "(:rb, :b, :bo, 1, 'created', 'B record', '')"
                ),
                {
                    "ra": uuid.uuid4(),
                    "rb": uuid.uuid4(),
                    "a": record_a,
                    "b": record_b,
                    "ao": org_a,
                    "bo": org_b,
                },
            )
    finally:
        await engine.dispose()
    return org_a, org_b, record_a, record_b


async def seed_two_organisation_files(
    owner_url: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed two organisations and one file row each (owner role).

    The owner credential is used deliberately: grouping ``files`` is a direct
    tenant table, so a seed must be able to write foreign rows the restricted
    runtime role is denied. Returns ``(org_a, org_b, file_a, file_b)``.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    file_a, file_b = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:a, :an), (:b, :bn)"),
                {"a": org_a, "an": "Files Group A", "b": org_b, "bn": "Files Group B"},
            )
            await connection.execute(
                text(
                    "INSERT INTO files "
                    "(id, organisation_id, storage_provider, storage_bucket, object_key, "
                    "original_filename, content_type, size_bytes, status) "
                    "VALUES (:a, :ao, 'fake', 'bucket', :ka, 'a.pdf', "
                    "'application/pdf', 1, 'uploaded'), "
                    "(:b, :bo, 'fake', 'bucket', :kb, 'b.pdf', "
                    "'application/pdf', 1, 'uploaded')"
                ),
                {
                    "a": file_a,
                    "ao": org_a,
                    "ka": f"organisations/{org_a}/documents/{file_a}/original",
                    "b": file_b,
                    "bo": org_b,
                    "kb": f"organisations/{org_b}/documents/{file_b}/original",
                },
            )
    finally:
        await engine.dispose()
    return org_a, org_b, file_a, file_b


async def seed_representative_files(
    owner_url: str,
    *,
    organisations: int = 40,
    rows_per_organisation: int = 30,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a multi-tenant ``files`` table for the representative plan review.

    A two-row table cannot give a representative plan; with many organisations
    the indexed ``organisation_id``/``created_at`` path is the planner's real
    choice. Returns organisation A and one of its file ids.
    """
    org_a = uuid.uuid4()
    other_orgs = [uuid.uuid4() for _ in range(organisations - 1)]
    sample_id = uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:id, :name)"),
                [
                    {"id": org, "name": f"Files plan {i}"}
                    for i, org in enumerate([org_a, *other_orgs])
                ],
            )
            rows = [
                {"id": sample_id, "org": org_a, "name": "sample.pdf"},
                *(
                    {"id": uuid.uuid4(), "org": org, "name": f"file {index}.pdf"}
                    for org in [org_a, *other_orgs]
                    for index in range(rows_per_organisation)
                ),
            ]
            for row in rows:
                row["key"] = f"organisations/{row['org']}/documents/{row['id']}/original"
            await connection.execute(
                text(
                    "INSERT INTO files "
                    "(id, organisation_id, storage_provider, storage_bucket, object_key, "
                    "original_filename, content_type, size_bytes, status) "
                    "VALUES (:id, :org, 'fake', 'bucket', :key, "
                    ":name, 'application/pdf', 1, 'uploaded')"
                ),
                rows,
            )
            await connection.execute(text("ANALYZE files"))
    finally:
        await engine.dispose()
    return org_a, sample_id


async def seed_representative_records(
    owner_url: str,
    *,
    organisations: int = 40,
    rows_per_organisation: int = 30,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a multi-tenant ``records`` table and its revision ledger.

    A two-row table cannot give a representative plan; with many organisations
    the indexed ``organisation_id``/``created_at`` path is the planner's real
    choice. Each record also gets a small revision history so the ledger's
    ``record_id``/``created_at`` index can be reviewed. Returns organisation A
    and one of its record ids.
    """
    org_a = uuid.uuid4()
    other_orgs = [uuid.uuid4() for _ in range(organisations - 1)]
    sample_id = uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:id, :name)"),
                [
                    {"id": org, "name": f"Group plan {i}"}
                    for i, org in enumerate([org_a, *other_orgs])
                ],
            )
            rows = [{"id": sample_id, "org": org_a, "title": "sample"}]
            for org in [org_a, *other_orgs]:
                rows.extend(
                    {"id": uuid.uuid4(), "org": org, "title": f"row {index}"}
                    for index in range(rows_per_organisation)
                )
            await connection.execute(
                text(
                    "INSERT INTO records (id, organisation_id, title, body, version) "
                    "VALUES (:id, :org, :title, '', 1)"
                ),
                rows,
            )
            revisions = [
                {
                    "id": uuid.uuid4(),
                    "rid": row["id"],
                    "org": row["org"],
                    "version": version,
                }
                for row in rows
                for version in range(1, 4)
            ]
            await connection.execute(
                text(
                    "INSERT INTO record_revisions "
                    "(id, record_id, organisation_id, version, action, title, body) "
                    "VALUES (:id, :rid, :org, :version, 'updated', 'revision', '')"
                ),
                revisions,
            )
            await connection.execute(text("ANALYZE records"))
            await connection.execute(text("ANALYZE record_revisions"))
    finally:
        await engine.dispose()
    return org_a, sample_id


def force_drop_runtime_role(database_url: str) -> None:
    """Revoke grants and drop ``app_runtime`` if it exists (test cleanup)."""

    async def _run() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        f"""
                        DO $$
                        BEGIN
                            IF EXISTS (
                                SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}'
                            ) THEN
                                EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public
                                         FROM {RUNTIME_ROLE}';
                                EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
                                         FROM {RUNTIME_ROLE}';
                                EXECUTE 'REVOKE ALL ON SCHEMA public FROM {RUNTIME_ROLE}';
                                EXECUTE 'DROP ROLE {RUNTIME_ROLE}';
                            END IF;
                        END
                        $$;
                        """
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def upgrade_to_head() -> None:
    """Apply every migration to head."""
    command.upgrade(alembic_config(), "head")


def downgrade_to_base() -> None:
    """Revert every migration to base."""
    command.downgrade(alembic_config(), "base")


__all__ = [
    "BACKEND_ROOT",
    "RUNTIME_PASSWORD",
    "RUNTIME_ROLE",
    "alembic_config",
    "database_reachable",
    "downgrade_to_base",
    "force_drop_runtime_role",
    "provision_runtime_login",
    "runtime_engine",
    "runtime_url",
    "seed_representative_files",
    "seed_representative_records",
    "seed_two_organisation_files",
    "seed_two_organisation_records",
    "upgrade_to_head",
]
