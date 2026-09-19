"""Real-PostgreSQL production enablement suite for the files group (P3, group 1).

Plan P3 / ``docs/rls-rollout.md`` §3 group 1. It proves the production
enablement migration (``e3f4a5b6c7d8``) that promotes the direct tenant
``files`` table into the permanent policy chain:

- the canonical ``files_organisation_isolation`` policy is installed with
  matching ``USING``/``WITH CHECK`` and RLS is enabled **and forced** on
  ``files``;
- default denial: no bound context returns no rows and fails closed for writes;
- both **read and write** policies deny select, insert, update and delete of
  another organisation's rows, including through an unscoped query and a
  tenant-key move;
- a foreign row is indistinguishable from a missing one on the real
  runtime-role app path;
- the file worker binds the organisation context from the durable ``jobs`` row
  and drives the file to ``ready`` under the enforced policy across its
  multiple self-committing service steps;
- representative list/detail plans use the tenant index; and
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
from sqlalchemy import CursorResult, select, text
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
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_representative_files,
    seed_two_organisation_files,
    upgrade_to_head,
)

from app.ai import execution as ai_execution
from app.ai.execution import (
    JOB_TYPE_AI_EXECUTE,
    execute_ai_task,
    request_id_for_job,
)
from app.ai.persistence.models import AIRequestRecord, AIRequestStatus
from app.ai.persistence.service import create_default_settings
from app.db.rls import bind_organisation_context
from app.modules.files import service as files_service
from app.modules.files import tasks as files_tasks
from app.modules.files.models import FileStatus
from app.modules.jobs import execution as jobs_execution
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatus
from app.modules.organisations.models import Organisation
from app.modules.users.models import User
from app.storage import FakeObjectStorage, get_storage

#: The group 0 revision this migration revises.
GROUP0_REVISION = "d2e3f4a5b6c7"

#: Production policy installed by the group 1 migration.
PRODUCTION_POLICY = "files_organisation_isolation"

_LATENCY_BUDGET_MS = 500.0
_LIST_PAGE_SQL = (
    "SELECT id, original_filename FROM files WHERE organisation_id = :org "
    "AND deleted_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 50"
)
#: A representative file size for the worker round-trip.
_UPLOAD_BYTES = b"the bytes the RLS files worker verifies"


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
    """RLS is enabled and forced on ``files`` with the canonical policy."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT qual, with_check FROM pg_policies "
                        "WHERE tablename = 'files' AND policyname = :policy"
                    ),
                    {"policy": PRODUCTION_POLICY},
                )
            ).one_or_none()
            assert row is not None, "missing production policy on files"
            assert "app_current_tenant_id()" in row.qual
            assert "app_current_tenant_id()" in row.with_check

            flags = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'files'"
                    )
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
    """No bound context: zero file rows and every write is rejected."""
    await seed_two_organisation_files(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM files")) == 0
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO files "
                        "(id, organisation_id, storage_provider, storage_bucket, object_key, "
                        "original_filename, content_type, size_bytes, status) "
                        "VALUES (:id, :org, 'fake', 'bucket', :key, 'x.pdf', "
                        "'application/pdf', 1, 'uploaded')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org": uuid.uuid4(),
                        "key": f"organisations/{uuid.uuid4()}/documents/{uuid.uuid4()}/original",
                    },
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Cross-organisation read and write --------------------------------------


async def test_select_is_organisation_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Own rows are visible; a foreign row is invisible, even unscoped."""
    org_a, _org_b, file_a, file_b = await seed_two_organisation_files(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)
            unscoped = (await session.execute(text("SELECT organisation_id FROM files"))).all()
            assert {row.organisation_id for row in unscoped} == {org_a}
            own = await session.scalar(text("SELECT id FROM files WHERE id = :id"), {"id": file_a})
            foreign = await session.scalar(
                text("SELECT id FROM files WHERE id = :id"), {"id": file_b}
            )
            assert own == file_a
            assert foreign is None
            await session.rollback()
    finally:
        await engine.dispose()


async def test_insert_update_delete_and_tenant_key_move_are_denied(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Same-tenant writes succeed; every cross-tenant write is denied."""
    org_a, org_b, file_a, file_b = await seed_two_organisation_files(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)
            # A same-tenant insert is allowed.
            await session.execute(
                text(
                    "INSERT INTO files "
                    "(id, organisation_id, storage_provider, storage_bucket, object_key, "
                    "original_filename, content_type, size_bytes, status) "
                    "VALUES (:id, :org, 'fake', 'bucket', :key, 'own.pdf', "
                    "'application/pdf', 1, 'uploaded')"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": org_a,
                    "key": f"organisations/{org_a}/documents/{uuid.uuid4()}/original",
                },
            )
            # A mismatched insert is rejected by WITH CHECK.
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO files "
                        "(id, organisation_id, storage_provider, storage_bucket, object_key, "
                        "original_filename, content_type, size_bytes, status) "
                        "VALUES (:id, :org, 'fake', 'bucket', :key, 'foreign.pdf', "
                        "'application/pdf', 1, 'uploaded')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org": org_b,
                        "key": f"organisations/{org_b}/documents/{uuid.uuid4()}/original",
                    },
                )
            await session.rollback()

        async with factory() as session:
            await bind_organisation_context(session, org_a)
            foreign_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE files SET original_filename = 'hijacked' WHERE id = :id"),
                    {"id": file_b},
                ),
            )
            assert foreign_update.rowcount == 0
            foreign_delete = cast(
                "CursorResult[Any]",
                await session.execute(text("DELETE FROM files WHERE id = :id"), {"id": file_b}),
            )
            assert foreign_delete.rowcount == 0
            own_update = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE files SET original_filename = 'updated.pdf' WHERE id = :id"),
                    {"id": file_a},
                ),
            )
            assert own_update.rowcount == 1
            await session.rollback()

        async with factory() as session:
            await bind_organisation_context(session, org_a)
            # Moving a same-tenant row into another tenant is rejected too.
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text("UPDATE files SET organisation_id = :org WHERE id = :id"),
                    {"org": org_b, "id": file_a},
                )
            await session.rollback()
    finally:
        await engine.dispose()

    # The foreign row is untouched when read by an unrestricted owner connection.
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            name = await connection.scalar(
                text("SELECT original_filename FROM files WHERE id = :id"), {"id": file_b}
            )
            assert name == "b.pdf"
    finally:
        await owner_engine.dispose()


# --- Worker context propagation ---------------------------------------------


async def test_worker_binds_organisation_context_from_durable_job(
    migrated_database: str,
    runtime_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file worker drives the file to ``ready`` under the enforced policy.

    Plan P3 / ADR-0022 decision 3: the worker reads its durable ``jobs`` row,
    then binds ``app.organisation_id`` from the row's own ``organisation_id``.
    The ``process_file`` handler crosses several self-committing service
    transactions, so the files service must bind the context for each protected
    transaction; this test proves that with the restricted runtime role and the
    ``files`` policy enforced.
    """
    storage = cast(FakeObjectStorage, get_storage())
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    try:
        async with owner_factory() as session:
            organisation = Organisation(name="RLS Files Worker Ltd")
            session.add(organisation)
            await session.flush()
            user = User(
                workos_user_id=f"user_rls_files_{uuid.uuid4().hex[:10]}",
                email="uploader@example.com",
                name="Uploader",
            )
            session.add(user)
            await session.flush()
            organisation_id, user_id = organisation.id, user.id

            file, signed_url = await files_service.create_upload_intent(
                session,
                organisation_id=organisation_id,
                original_filename="report.pdf",
                content_type="application/pdf",
                size_bytes=len(_UPLOAD_BYTES),
                actor_user_id=user_id,
            )
            assert signed_url.method == "PUT"
            await storage.put(file.object_key, _UPLOAD_BYTES)
            completed, job_id = await files_service.complete_upload(
                session,
                organisation_id=organisation_id,
                file_id=file.id,
            )
            assert completed.status == FileStatus.UPLOADED
            assert job_id is not None
            file_id = file.id
    finally:
        await owner_engine.dispose()

    # Patch the worker's session factory to the restricted runtime role.
    runtime_engine_instance = runtime_engine(runtime_database_url)
    runtime_factory = async_sessionmaker(runtime_engine_instance, expire_on_commit=False)
    monkeypatch.setattr(files_tasks, "async_session_factory", runtime_factory)
    monkeypatch.setattr(jobs_execution, "async_session_factory", runtime_factory)
    try:
        await files_tasks.process_file(str(job_id))
    finally:
        await runtime_engine_instance.dispose()

    # Read back with the owner credential: the worker left no residue.
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    try:
        async with owner_factory() as session:
            file_row = await files_service.get_file(
                session, organisation_id=organisation_id, file_id=file_id
            )
            assert file_row.status == FileStatus.READY
            job = await session.get(Job, job_id)
            assert job is not None
            assert job.status == JobStatus.SUCCEEDED
            assert job.progress == 100
    finally:
        await owner_engine.dispose()


async def test_ai_worker_binds_context_for_document_authority_read(
    migrated_database: str,
    runtime_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queued AI worker authorises a ``documents/`` file under enforced RLS.

    Plan P3 / ADR-0022 decision 9: the AI execution handler claims and commits
    the durable job, then runs the attempt on a fresh session with no bound
    organisation context. ``DocumentSourceAuthority.authorize`` must bind the
    durable job's organisation before reading the policy-protected ``files``
    row, or an otherwise valid same-organisation ready document is
    default-denied and the job fails permanently. This drives the real
    ``execute_ai_task`` path with the ``files`` policy forced on the restricted
    ``app_runtime`` login, which the file-processing worker test above does not
    exercise.
    """
    storage = cast(FakeObjectStorage, get_storage())
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    try:
        async with owner_factory() as session:
            organisation = Organisation(name="RLS AI Authority Ltd")
            session.add(organisation)
            await session.flush()
            user = User(
                workos_user_id=f"user_rls_ai_{uuid.uuid4().hex[:10]}",
                email="documents@example.com",
                name="Documents Uploader",
            )
            session.add(user)
            await session.flush()
            organisation_id, user_id = organisation.id, user.id

            settings_row = await create_default_settings(session, organisation_id=organisation_id)
            settings_row.enabled = True
            await session.flush()

            file, signed_url = await files_service.create_upload_intent(
                session,
                organisation_id=organisation_id,
                original_filename="notes.txt",
                content_type="text/plain",
                size_bytes=len(_UPLOAD_BYTES),
                actor_user_id=user_id,
            )
            assert signed_url.method == "PUT"
            await storage.put(file.object_key, _UPLOAD_BYTES, content_type="text/plain")
            await files_service.complete_upload(
                session,
                organisation_id=organisation_id,
                file_id=file.id,
            )
            await files_service.mark_file_processing(
                session, organisation_id=organisation_id, file_id=file.id
            )
            ready = await files_service.mark_file_ready(
                session, organisation_id=organisation_id, file_id=file.id
            )
            assert ready.status == FileStatus.READY
            document_reference = ready.object_key

            job = await jobs_service.schedule_job(
                session,
                organisation_id=organisation_id,
                job_type=JOB_TYPE_AI_EXECUTE,
                input_reference=document_reference,
                actor_user_id=user_id,
            )
            job_id = job.id
    finally:
        await owner_engine.dispose()

    # The AI worker runs entirely on the restricted runtime role, including the
    # fresh attempt session opened after the job claim commit.
    runtime_engine_instance = runtime_engine(runtime_database_url)
    runtime_factory = async_sessionmaker(runtime_engine_instance, expire_on_commit=False)
    monkeypatch.setattr(ai_execution, "async_session_factory", runtime_factory)
    monkeypatch.setattr(jobs_execution, "async_session_factory", runtime_factory)
    try:
        await execute_ai_task(str(job_id))
    finally:
        await runtime_engine_instance.dispose()

    # Read back with the owner credential: the authority read was not denied.
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    try:
        async with owner_factory() as session:
            job_row = await session.get(Job, job_id)
            assert job_row is not None
            assert job_row.status == JobStatus.SUCCEEDED, job_row.error_message
            record = await session.scalar(
                select(AIRequestRecord).where(
                    AIRequestRecord.organisation_id == organisation_id,
                    AIRequestRecord.request_id == request_id_for_job(job_id),
                )
            )
            assert record is not None
            assert record.status == AIRequestStatus.SUCCEEDED
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


async def test_foreign_file_is_indistinguishable_from_missing(
    migrated_database: str,
    runtime_client: AsyncClient,
    private_key: rsa.RSAPrivateKey,
) -> None:
    """A foreign file and an absent file return the identical 404 under RLS."""
    world: IsolationWorld = await seed_isolation_world(migrated_database)
    headers = auth_headers(private_key, world.multi, org_id=world.org_a)
    foreign = await runtime_client.get(f"/api/v1/files/{world.file_b}", headers=headers)
    missing = await runtime_client.get(f"/api/v1/files/{uuid.uuid4()}", headers=headers)
    assert foreign.status_code == NOT_FOUND, foreign.text
    assert missing.status_code == NOT_FOUND, missing.text
    assert foreign.json()["code"] == "file_not_found"
    assert missing.json()["code"] == "file_not_found"
    assert foreign.json()["message"] == missing.json()["message"]
    assert foreign.json().get("details") == missing.json().get("details")


# --- Representative query plans ---------------------------------------------


async def test_representative_query_plans_use_the_tenant_index(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The files list plan uses the tenant index; no sequential scan."""
    org_a, sample_id = await seed_representative_files(migrated_database)
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
                        text("EXPLAIN SELECT * FROM files WHERE id = :id"),
                        {"id": sample_id},
                    )
                ).all()
            )
            assert "Seq Scan" not in list_plan
            assert "ix_files_organisation_id_created_at" in list_plan
            assert "Seq Scan" not in detail_plan
            assert "pk_files" in detail_plan

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


def test_group1_migration_downgrade_and_reupgrade(migrated_database: str) -> None:
    """One revision down removes only the files policy; re-upgrade re-installs it."""
    config = alembic_config()
    try:
        command.downgrade(config, GROUP0_REVISION)
        assert asyncio.run(_policy_count(migrated_database, "files", PRODUCTION_POLICY)) == 0
        assert asyncio.run(_rls_flags(migrated_database, "files")) == (False, False)
        # Earlier groups stay intact.
        assert (
            asyncio.run(
                _policy_count(migrated_database, "records", "records_organisation_isolation")
            )
            == 1
        )
        assert asyncio.run(_rls_flags(migrated_database, "records")) == (True, True)

        command.upgrade(config, "head")
        assert asyncio.run(_policy_count(migrated_database, "files", PRODUCTION_POLICY)) == 1
        assert asyncio.run(_rls_flags(migrated_database, "files")) == (True, True)
    finally:
        command.upgrade(alembic_config(), "head")


# --- Restricted runtime credential ------------------------------------------


async def test_runtime_credential_cannot_disable_policy_or_alter_schema(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The runtime role is non-owner and cannot weaken the files policy."""
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for statement in (
                "ALTER TABLE files DISABLE ROW LEVEL SECURITY",
                "ALTER TABLE files NO FORCE ROW LEVEL SECURITY",
                "DROP POLICY files_organisation_isolation ON files",
                "DROP TABLE files",
                "ALTER TABLE files ADD COLUMN hacked integer",
                "ALTER ROLE app_runtime BYPASSRLS",
            ):
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
                await session.rollback()
    finally:
        await engine.dispose()
