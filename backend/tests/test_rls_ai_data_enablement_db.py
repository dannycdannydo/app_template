"""Real-PostgreSQL production enablement suite for the AI-data group (P3, group 3).

Plan P3 / ``docs/rls-rollout.md`` §3 group 3. It proves the production
enablement migration (``b8c9d0e1f2a3``) that promotes the four direct
organisation-owned AI tables into the permanent policy chain:

- the canonical ``<table>_organisation_isolation`` policy is installed with
  matching ``USING``/``WITH CHECK`` and RLS is enabled **and forced** on
  ``ai_requests``, ``ai_outputs``, ``ai_attachment_references`` and
  ``ai_scratch_uploads``;
- default denial: no bound context returns no rows and fails closed for writes;
- both **read and write** policies deny select, insert, update and delete of
  another organisation's rows, including through an unscoped query and a
  tenant-key move;
- the ``ai.execute`` worker binds the organisation context from the durable
  ``jobs`` row and settles under the enforced policies;
- the three formerly global cross-tenant sweeps (retention, scratch expiry and
  provider-file reconciliation) run under the enforced policies by iterating
  the unprotected ``organisations`` table and binding each tenant, without a
  bypass;
- representative plans use the tenant index; and
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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from alembic import command
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import CursorResult, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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
    AIIsolationSeed,
    alembic_config,
    database_reachable,
    downgrade_to_base,
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_representative_ai,
    seed_two_organisation_ai,
    upgrade_to_head,
)

from app.ai import execution as ai_execution
from app.ai.execution import (
    JOB_TYPE_AI_EXECUTE,
    execute_ai_task,
    request_id_for_job,
)
from app.ai.persistence import reconciliation as ai_reconciliation
from app.ai.persistence import service as ai_persistence
from app.ai.persistence.models import (
    AIOutputRecord,
    AIRequestRecord,
    AIRequestStatus,
    AIScratchUpload,
    AIScratchUploadStatus,
    OrganisationAISettings,
)
from app.ai.persistence.references import SQLTransferReferenceStore
from app.ai.persistence.service import create_default_settings
from app.ai.staging import FakeTransferStore
from app.db.rls import bind_organisation_context
from app.modules.files import service as files_service
from app.modules.jobs import execution as jobs_execution
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatus
from app.modules.organisations.models import Organisation
from app.modules.users.models import User
from app.storage import FakeObjectStorage, get_storage

#: The group 2 revision this migration revises.
GROUP2_REVISION = "f5a6b7c8d9e0"

#: Every table enabled by the group 3 migration.
AI_TABLES = (
    "ai_requests",
    "ai_outputs",
    "ai_attachment_references",
    "ai_scratch_uploads",
)

_LATENCY_BUDGET_MS = 500.0
#: A representative file size for the worker round-trip.
_UPLOAD_BYTES = b"the bytes the RLS AI worker verifies"


def _production_policy(table: str) -> str:
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


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    """One module-local RSA key for minting WorkOS-style tokens."""
    key, _ = generate_key_pair()
    return key


def _owner_factory(database_url: str) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# --- Installed production policy --------------------------------------------


async def test_production_policies_are_installed_and_forced(migrated_database: str) -> None:
    """RLS is enabled and forced on every AI table with the canonical policy."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            for table in AI_TABLES:
                row = (
                    await connection.execute(
                        text(
                            "SELECT qual, with_check FROM pg_policies "
                            "WHERE tablename = :table AND policyname = :policy"
                        ),
                        {"table": table, "policy": _production_policy(table)},
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
    """No bound context: zero AI rows and every write is rejected."""
    await seed_two_organisation_ai(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for table in AI_TABLES:
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO ai_requests "
                        "(id, organisation_id, request_id, attempt_number, task, "
                        "routing_reason, region, status, input_tokens, output_tokens, latency_ms) "
                        "VALUES (:id, :org, :request_id, 1, 'document.classify', "
                        "'x', '', 'failed', 0, 0, 0)"
                    ),
                    {"id": uuid.uuid4(), "org": uuid.uuid4(), "request_id": uuid.uuid4().hex},
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Cross-organisation read and write ---------------------------------------


async def test_select_is_organisation_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Own rows are visible on every table; a foreign row is invisible unscoped."""
    seed = await seed_two_organisation_ai(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            for table in AI_TABLES:
                orgs = (await session.execute(text(f"SELECT organisation_id FROM {table}"))).all()
                assert {row.organisation_id for row in orgs} == {seed.org_a}, table
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM ai_requests WHERE id = :id"),
                    {"id": seed.request_b},
                )
                == 0
            )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_insert_update_delete_and_tenant_key_move_are_denied(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Same-tenant writes succeed; every cross-tenant write is denied.

    Plan P3 completion evidence ("every enabled table has real
    select/insert/update/delete policy tests"): on each of the four group-3
    tables a real own-tenant insert/update/delete succeeds, a mismatched insert
    is rejected by ``WITH CHECK``, a foreign-tenant update/delete changes no
    row and a tenant-key move into another organisation is rejected.
    """
    seed = await seed_two_organisation_ai(migrated_database)
    # Fresh own-tenant rows for the insert proof; the seeded org A rows are
    # reused for the update/delete/tenant-key-move proofs.
    own_request_id, own_output_id = uuid.uuid4(), uuid.uuid4()
    own_reference_id, own_scratch_id = uuid.uuid4(), uuid.uuid4()
    own_row_ids = {
        "ai_requests": seed.request_a,
        "ai_outputs": seed.output_a,
        "ai_attachment_references": seed.reference_a,
        "ai_scratch_uploads": seed.scratch_a,
    }
    foreign_row_ids = {
        "ai_requests": seed.request_b,
        "ai_outputs": seed.output_b,
        "ai_attachment_references": seed.reference_b,
        "ai_scratch_uploads": seed.scratch_b,
    }
    update_statements = {
        "ai_requests": "UPDATE ai_requests SET task = 'own-updated' WHERE id = :id",
        "ai_outputs": (
            "UPDATE ai_outputs SET output_json = '{\"own\": true}'::jsonb WHERE id = :id"
        ),
        "ai_attachment_references": (
            "UPDATE ai_attachment_references SET external_id = 'own-updated' WHERE id = :id"
        ),
        "ai_scratch_uploads": "UPDATE ai_scratch_uploads SET status = 'ready' WHERE id = :id",
    }
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Same-tenant inserts succeed on every table (the ai_requests row is
        # inserted first so the ai_outputs composite tenant FK resolves inside
        # the transaction).
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            for _table, statement, params in _own_inserts(
                seed,
                request_id=own_request_id,
                output_id=own_output_id,
                reference_id=own_reference_id,
                scratch_id=own_scratch_id,
            ):
                await session.execute(text(statement), params)
            await session.commit()

        # A mismatched insert is rejected by WITH CHECK on every table. Each
        # failed statement aborts its transaction, so roll back and rebind the
        # tenant before the next one.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            for _table, statement, params in _foreign_inserts(seed.org_b):
                with pytest.raises(ProgrammingError, match="row-level security"):
                    await session.execute(text(statement), params)
                await session.rollback()
                await bind_organisation_context(session, seed.org_a)

        # Same-tenant update and delete succeed; the matching foreign writes
        # affect no row. Each table gets its own transaction, rolled back so the
        # seeded rows survive for the later checks.
        for table in AI_TABLES:
            own_id = own_row_ids[table]
            foreign_id = foreign_row_ids[table]
            update_sql = update_statements[table]
            async with factory() as session:
                await bind_organisation_context(session, seed.org_a)
                for target_id, expected in ((own_id, 1), (foreign_id, 0)):
                    own_update = cast(
                        "CursorResult[Any]",
                        await session.execute(text(update_sql), {"id": target_id}),
                    )
                    assert own_update.rowcount == expected, f"{table} update {target_id}"
                    delete = cast(
                        "CursorResult[Any]",
                        await session.execute(
                            text(f"DELETE FROM {table} WHERE id = :id"), {"id": target_id}
                        ),
                    )
                    assert delete.rowcount == expected, f"{table} delete {target_id}"
                await session.rollback()

        # Moving a same-tenant row into another tenant is rejected on every
        # table (WITH CHECK on the new row).
        for table in AI_TABLES:
            async with factory() as session:
                await bind_organisation_context(session, seed.org_a)
                with pytest.raises(ProgrammingError, match="row-level security"):
                    await session.execute(
                        text(f"UPDATE {table} SET organisation_id = :org WHERE id = :id"),
                        {"org": seed.org_b, "id": own_row_ids[table]},
                    )
                await session.rollback()
    finally:
        await engine.dispose()

    # The foreign rows are untouched when read by an unrestricted owner connection.
    engine, factory = _owner_factory(migrated_database)
    try:
        async with factory() as session:
            for table in AI_TABLES:
                assert (
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE id = :id"),
                        {"id": foreign_row_ids[table]},
                    )
                    == 1
                ), table
    finally:
        await engine.dispose()


def _own_inserts(
    seed: AIIsolationSeed,
    *,
    request_id: uuid.UUID,
    output_id: uuid.UUID,
    reference_id: uuid.UUID,
    scratch_id: uuid.UUID,
) -> list[tuple[str, str, dict[str, object]]]:
    """Return one same-tenant insert per group-3 table for the write proof."""
    return [
        (
            "ai_requests",
            "INSERT INTO ai_requests "
            "(id, organisation_id, request_id, attempt_number, task, provider, model, "
            "prompt_name, prompt_version, routing_reason, region, status, input_tokens, "
            "output_tokens, latency_ms) "
            "VALUES (:id, :org, :request_id, 1, 'document.classify', 'fake', "
            "'fake.document-classifier', 'document.classify', 1, 'own', '', 'failed', 0, 0, 0)",
            {"id": request_id, "org": seed.org_a, "request_id": uuid.uuid4().hex},
        ),
        (
            "ai_outputs",
            "INSERT INTO ai_outputs (id, ai_request_id, organisation_id, output_json) "
            "VALUES (:id, :request, :org, '{\"own\": true}'::jsonb)",
            {"id": output_id, "request": request_id, "org": seed.org_a},
        ),
        (
            "ai_attachment_references",
            "INSERT INTO ai_attachment_references "
            "(id, organisation_id, logical_request_id, provider, transfer_mode, external_id, "
            "source_reference, source_digest, size_bytes, mime_type, source_lifecycle, region, "
            "status, idempotency_key) "
            "VALUES (:id, :org, 'own', 'fake', 'provider_upload', 'ext-own', 'ref', "
            ":digest, 1600, 'application/pdf', 'transient', 'eu-west-1', 'live', :key)",
            {"id": reference_id, "org": seed.org_a, "digest": "e1" * 32, "key": "f1" * 32},
        ),
        (
            "ai_scratch_uploads",
            "INSERT INTO ai_scratch_uploads "
            "(id, organisation_id, upload_id, object_key, content_type, size_bytes, status, "
            "expires_at) VALUES (:id, :org, :upload_id, :key, 'application/pdf', 1024, "
            "'pending', :expires)",
            {
                "id": scratch_id,
                "org": seed.org_a,
                "upload_id": uuid.uuid4(),
                "key": f"organisations/{seed.org_a}/ai/scratch/{uuid.uuid4()}.pdf",
                "expires": datetime.now(UTC) + timedelta(hours=1),
            },
        ),
    ]


def _foreign_inserts(org_b: uuid.UUID) -> list[tuple[str, str, dict[str, object]]]:
    """Return one cross-tenant insert per group-3 table for the WITH CHECK proof."""
    return [
        (
            "ai_requests",
            "INSERT INTO ai_requests "
            "(id, organisation_id, request_id, attempt_number, task, routing_reason, region, "
            "status, input_tokens, output_tokens, latency_ms) "
            "VALUES (:id, :org, :request_id, 1, 'document.classify', 'x', '', 'failed', 0, 0, 0)",
            {"id": uuid.uuid4(), "org": org_b, "request_id": uuid.uuid4().hex},
        ),
        (
            "ai_outputs",
            "INSERT INTO ai_outputs (id, ai_request_id, organisation_id) "
            "VALUES (:id, :request, :org)",
            {"id": uuid.uuid4(), "request": uuid.uuid4(), "org": org_b},
        ),
        (
            "ai_attachment_references",
            "INSERT INTO ai_attachment_references "
            "(id, organisation_id, logical_request_id, provider, transfer_mode, external_id, "
            "source_reference, source_digest, size_bytes, mime_type, source_lifecycle, region, "
            "status, idempotency_key) "
            "VALUES (:id, :org, 'foreign', 'fake', 'provider_upload', 'ext', 'ref', "
            ":digest, 1600, 'application/pdf', 'transient', 'eu-west-1', 'live', :key)",
            {
                "id": uuid.uuid4(),
                "org": org_b,
                "digest": "e5" * 32,
                "key": "f6" * 32,
            },
        ),
        (
            "ai_scratch_uploads",
            "INSERT INTO ai_scratch_uploads "
            "(id, organisation_id, upload_id, object_key, content_type, size_bytes, status, "
            "expires_at) VALUES (:id, :org, :upload_id, :key, 'application/pdf', 1024, "
            "'pending', :expires)",
            {
                "id": uuid.uuid4(),
                "org": org_b,
                "upload_id": uuid.uuid4(),
                "key": f"organisations/{org_b}/ai/scratch/{uuid.uuid4()}.pdf",
                "expires": datetime.now(UTC) + timedelta(hours=1),
            },
        ),
    ]


# --- Worker context propagation ---------------------------------------------


async def test_ai_worker_binds_context_and_settles_under_enforced_rls(
    migrated_database: str,
    runtime_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AI worker drives a document classification to success under RLS.

    Plan P3 group 3 / ADR-0022 decisions 3 and 9: the worker binds the durable
    job's organisation before touching the protected AI tables, and the
    persistence port rebinds after each of its own commits. This test proves
    that with the restricted runtime role and every group-3 policy enforced.
    """
    storage = cast(FakeObjectStorage, get_storage())
    owner_engine, owner_factory = _owner_factory(migrated_database)
    try:
        async with owner_factory() as session:
            organisation = Organisation(name="RLS AI Data Ltd")
            session.add(organisation)
            await session.flush()
            user = User(
                workos_user_id=f"user_rls_ai_data_{uuid.uuid4().hex[:10]}",
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
                session, organisation_id=organisation_id, file_id=file.id
            )
            await files_service.mark_file_processing(
                session, organisation_id=organisation_id, file_id=file.id
            )
            ready = await files_service.mark_file_ready(
                session, organisation_id=organisation_id, file_id=file.id
            )
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

    runtime_engine_instance = runtime_engine(runtime_database_url)
    runtime_factory = async_sessionmaker(runtime_engine_instance, expire_on_commit=False)
    monkeypatch.setattr(ai_execution, "async_session_factory", runtime_factory)
    monkeypatch.setattr(jobs_execution, "async_session_factory", runtime_factory)
    try:
        await execute_ai_task(str(job_id))
    finally:
        await runtime_engine_instance.dispose()

    owner_engine, owner_factory = _owner_factory(migrated_database)
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


# --- Per-organisation sweeps under enforced RLS -----------------------------


async def test_retention_and_scratch_sweeps_run_per_organisation(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The retention and scratch sweeps process rows across tenants without bypass.

    With every group-3 policy enforced, the sweeps enumerate the unprotected
    ``organisations`` table and bind each tenant. This seeds two organisations
    with stale reservations, expired outputs and expired scratch intents and
    proves both are reconciled on the restricted runtime role.
    """
    now = datetime.now(UTC)
    owner_engine, owner_factory = _owner_factory(migrated_database)
    expected: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = {}
    try:
        async with owner_factory() as session:
            for label in ("A", "B"):
                organisation = Organisation(name=f"RLS AI Retention {label}")
                session.add(organisation)
                await session.flush()
                session.add(
                    OrganisationAISettings(
                        organisation_id=organisation.id,
                        enabled=True,
                        retention_policy_days=30,
                    )
                )
                stale = AIRequestRecord(
                    organisation_id=organisation.id,
                    request_id=uuid.uuid4().hex,
                    attempt_number=1,
                    task="document.classify",
                    provider="fake",
                    model="fake.document-classifier",
                    prompt_name="document.classify",
                    prompt_version=1,
                    routing_reason="seeded",
                    status=AIRequestStatus.RUNNING,
                    cost=Decimal("0.000010"),
                )
                session.add(stale)
                await session.flush()
                output = AIOutputRecord(
                    ai_request_id=stale.id,
                    organisation_id=organisation.id,
                    output_json={"seeded": True},
                )
                session.add(output)
                await session.flush()
                scratch = AIScratchUpload(
                    organisation_id=organisation.id,
                    upload_id=uuid.uuid4(),
                    object_key=f"organisations/{organisation.id}/ai/scratch/{uuid.uuid4()}.pdf",
                    content_type="application/pdf",
                    size_bytes=1024,
                    status=AIScratchUploadStatus.PENDING,
                    expires_at=now - timedelta(minutes=5),
                )
                session.add(scratch)
                await session.flush()
                stale.created_at = now - timedelta(days=2)
                output.created_at = now - timedelta(days=40)
                expected[organisation.id] = (stale.id, output.id, scratch.id)
            await session.commit()
    finally:
        await owner_engine.dispose()

    storage = FakeObjectStorage(bucket="test-bucket")
    runtime_engine_instance = runtime_engine(runtime_database_url)
    runtime_factory = async_sessionmaker(runtime_engine_instance, expire_on_commit=False)
    try:
        async with runtime_factory() as session:
            summary = await ai_persistence.enforce_ai_retention(session, storage, now=now)
    finally:
        await runtime_engine_instance.dispose()

    assert summary["stale_requests_reconciled"] >= 2
    assert summary["outputs_deleted"] >= 2
    assert summary["scratch_intents_expired"] >= 2

    owner_engine, owner_factory = _owner_factory(migrated_database)
    try:
        async with owner_factory() as session:
            for stale_id, output_id, scratch_id in expected.values():
                stale = await session.get(AIRequestRecord, stale_id)
                assert stale is not None
                assert stale.status == AIRequestStatus.FAILED
                assert stale.error_code == ai_persistence.ERROR_CODE_WORKER_CRASHED
                assert stale.cost == Decimal("0.000010")  # reservation never released
                assert await session.get(AIOutputRecord, output_id) is None
                scratch = await session.get(AIScratchUpload, scratch_id)
                assert scratch is not None
                assert scratch.status == AIScratchUploadStatus.EXPIRED
    finally:
        await owner_engine.dispose()


async def test_transfer_reconciliation_runs_per_organisation(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The provider-file reconciliation sweep processes both tenants without bypass."""
    seed = await seed_two_organisation_ai(migrated_database)
    store = FakeTransferStore()
    runtime_engine_instance = runtime_engine(runtime_database_url)
    runtime_factory = async_sessionmaker(runtime_engine_instance, expire_on_commit=False)
    try:
        async with runtime_factory() as session:
            summary = await ai_reconciliation.reconcile_provider_file_references(
                session,
                storage=FakeObjectStorage(bucket="feature-bucket"),
                stores={"fake": store},
                references=SQLTransferReferenceStore(session),
                batch_size=50,
                retry_after_seconds=60,
            )
    finally:
        await runtime_engine_instance.dispose()

    assert summary["deleted"] >= 2

    owner_engine, owner_factory = _owner_factory(migrated_database)
    try:
        async with owner_factory() as session:
            for reference_id in (seed.reference_a, seed.reference_b):
                status = await session.scalar(
                    text("SELECT status FROM ai_attachment_references WHERE id = :id"),
                    {"id": reference_id},
                )
                assert status == "deleted"
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


async def test_foreign_ai_request_is_indistinguishable_from_missing(
    migrated_database: str,
    runtime_client: AsyncClient,
    private_key: rsa.RSAPrivateKey,
) -> None:
    """A foreign AI request and an absent one return the identical 404 under RLS."""
    world: IsolationWorld = await seed_isolation_world(migrated_database)
    headers = auth_headers(private_key, world.multi, org_id=world.org_a)
    foreign = await runtime_client.get(
        f"/api/v1/ai/classify/requests/{world.ai_request_b}", headers=headers
    )
    missing = await runtime_client.get(
        f"/api/v1/ai/classify/requests/{uuid.uuid4().hex}", headers=headers
    )
    assert foreign.status_code == NOT_FOUND, foreign.text
    assert missing.status_code == NOT_FOUND, missing.text
    assert foreign.json()["code"] == "ai_request_not_found"
    assert missing.json()["code"] == "ai_request_not_found"


# --- Representative query plans ---------------------------------------------


async def test_representative_query_plans_use_the_tenant_index(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The AI result lookup plan uses the tenant index; no sequential scan."""
    org_a, sample_request_id = await seed_representative_ai(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    request_plan_sql = (
        "SELECT * FROM ai_requests WHERE organisation_id = :org AND request_id = :request_id "
        "ORDER BY attempt_number DESC"
    )
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)
            plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(
                        text("EXPLAIN " + request_plan_sql),
                        {"org": org_a, "request_id": sample_request_id},
                    )
                ).all()
            )
            assert "Seq Scan" not in plan
            assert "uq_ai_requests_org_request_attempt" in plan

            loop = asyncio.get_running_loop()
            start = loop.time()
            rows = (
                await session.execute(
                    text(request_plan_sql), {"org": org_a, "request_id": sample_request_id}
                )
            ).all()
            elapsed_ms = (loop.time() - start) * 1000
            assert rows
            assert elapsed_ms < _LATENCY_BUDGET_MS
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


def test_group3_migration_downgrade_and_reupgrade(migrated_database: str) -> None:
    """One revision down removes only the AI-data policies; re-upgrade re-installs."""
    config = alembic_config()
    try:
        command.downgrade(config, GROUP2_REVISION)
        for table in AI_TABLES:
            assert (
                asyncio.run(_policy_count(migrated_database, table, _production_policy(table))) == 0
            ), table
            assert asyncio.run(_rls_flags(migrated_database, table)) == (False, False), table
        # Earlier groups stay intact.
        assert (
            asyncio.run(
                _policy_count(migrated_database, "notifications", "notifications_user_isolation")
            )
            == 1
        )
        assert asyncio.run(_rls_flags(migrated_database, "notifications")) == (True, True)

        command.upgrade(config, "head")
        for table in AI_TABLES:
            assert (
                asyncio.run(_policy_count(migrated_database, table, _production_policy(table))) == 1
            ), table
            assert asyncio.run(_rls_flags(migrated_database, table)) == (True, True), table
    finally:
        command.upgrade(alembic_config(), "head")


# --- Restricted runtime credential ------------------------------------------


async def test_runtime_credential_cannot_disable_policy_or_alter_schema(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The runtime role is non-owner and cannot weaken any AI-data policy."""
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            statements = ["ALTER ROLE app_runtime BYPASSRLS"]
            for table in AI_TABLES:
                statements.extend(
                    [
                        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
                        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
                        f"DROP POLICY {_production_policy(table)} ON {table}",
                        f"ALTER TABLE {table} ADD COLUMN hacked integer",
                    ]
                )
            for statement in statements:
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
                await session.rollback()
    finally:
        await engine.dispose()
