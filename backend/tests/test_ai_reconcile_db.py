"""Real-database tests for the §6.7 reconciliation sweep and terminal deletion.

The unit suites prove the orchestrator seams hermetically; this module runs
the real migration, the real
:class:`~app.ai.persistence.references.SQLTransferReferenceStore`, the real
reconciliation queries and the real
:func:`~app.ai.persistence.reconciliation.reconcile_provider_file_references`
sweep against a reachable PostgreSQL (the same skip pattern as the other
``*_db.py`` modules).

Coverage maps to Scope §6.7 checkboxes 2-4:

- the sweep claims and deletes provider-hosted copies whose owning AI request
  is terminal, and a failed provider delete leaves the row stamped with the
  safe error code for the bounded backoff window (re-claimed only after the
  retry window);
- the candidate query never returns managed-signed-url rows (no provider
  copy), storage-reference rows (GCS staging objects owned by the deployer
  lifecycle) or rows of still-running/queued requests, and never touches the
  feature-owned source object;
- batching bounds one run, the backlog gauge predicate matches the candidate
  predicate, and the sweep writes ``ai.transfer_reconciled`` /
  ``ai.transfer_deleted`` audit events plus deletion-failure markers on the
  durable rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import dramatiq
import httpx
import pytest
from alembic import command
from alembic.config import Config
from dramatiq.brokers.stub import StubBroker
from dramatiq.worker import Worker
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.attachments import MAX_ATTACHMENT_BYTES
from app.ai.persistence import reconciliation
from app.ai.persistence.models import (
    AIAttachmentReference,
    AIRequestRecord,
    AIRequestStatus,
    OrganisationAISettings,
)
from app.ai.persistence.port import AIRequestReservation, OrganisationAIPolicy
from app.ai.persistence.queries import ai_attachment_reference_reconciliation_backlog_statement
from app.ai.persistence.references import SQLTransferReferenceStore
from app.ai.providers.fake import FakeLLMProvider
from app.ai.providers.openai_upload import OpenAITransferStore
from app.ai.registry import load_registry_bundle
from app.ai.schemas import AIRequest
from app.ai.service import AIService
from app.ai.staging import ExternalFileReference, FakeTransferStore, TransferStore
from app.ai.storage_resolver import StorageAttachmentResolver
from app.ai.transfer import (
    SourceLifecycle,
    TransferDeploymentPolicy,
    TransferMode,
    derive_idempotency_key,
)
from app.modules.audit.models import AuditEvent
from app.modules.organisations.models import Organisation
from app.storage.fake import FakeObjectStorage

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _database_reachable(database_url: str) -> bool:
    """Probe the configured database with a short async engine connect."""

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


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


def _session_factory(database_url: str):
    engine = create_async_engine(database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_organisation(session: AsyncSession) -> Organisation:
    organisation = Organisation(name=f"Reconcile Org {uuid.uuid4().hex[:8]}")
    session.add(organisation)
    await session.commit()
    return organisation


async def _enable_ai(session: AsyncSession, organisation_id: UUID) -> None:
    settings = OrganisationAISettings(organisation_id=organisation_id)
    session.add(settings)
    await session.commit()
    return None


async def _seed_terminal_request(
    session: AsyncSession, organisation_id: UUID, request_id: str, *, status: str = "failed"
) -> None:
    """Insert one terminal (or queued/running) AI request row with a digest."""
    session.add(
        AIRequestRecord(
            organisation_id=organisation_id,
            request_id=request_id,
            attempt_number=1,
            task="document.classify",
            provider="fake",
            model="fake-model-document.classify",
            prompt_name="document.classify_v1",
            prompt_version=1,
            routing_reason="test",
            status=AIRequestStatus(status),
            input_reference=f"organisations/{organisation_id}/documents/doc-1/original",
            input_digest="ab" * 32,
        )
    )
    await session.commit()


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def _seed_provider_reference(
    session: AsyncSession,
    organisation_id: UUID,
    logical_request_id: str,
    *,
    mode: TransferMode = TransferMode.PROVIDER_UPLOAD,
    provider: str = "fake",
    digest: str | None = None,
    external_id: str | None = None,
    status: str = "live",
    deletion_attempted_at: datetime | None = None,
    error_code: str | None = None,
    source_lifecycle: SourceLifecycle | None = None,
) -> str:
    """Insert one reference row; returns its derived idempotency key."""
    digest = digest or _digest(f"{logical_request_id}-{mode.value}")
    key = derive_idempotency_key(
        provider=provider,
        mode=mode,
        organisation_id=organisation_id,
        logical_request_id=logical_request_id,
        source_digest=digest,
        region="eu-west-1",
    )
    lifecycle = source_lifecycle or (
        SourceLifecycle.RETAINED
        if mode is not TransferMode.PROVIDER_UPLOAD
        else SourceLifecycle.TRANSIENT
    )
    session.add(
        AIAttachmentReference(
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            provider=provider,
            transfer_mode=mode.value,
            external_id=external_id or f"fake-file-{key[:12]}",
            source_reference=f"organisations/{organisation_id}/documents/doc-1/original",
            source_digest=digest,
            size_bytes=6_000_000,
            mime_type="application/pdf",
            source_lifecycle=lifecycle.value,
            region="eu-west-1",
            status=status,
            idempotency_key=key,
            error_code=error_code,
            deletion_attempted_at=deletion_attempted_at,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await session.commit()
    return key


async def _row(session: AsyncSession, key: str) -> AIAttachmentReference | None:
    return await session.scalar(
        select(AIAttachmentReference).where(AIAttachmentReference.idempotency_key == key)
    )


async def _cleanup(session: AsyncSession, organisation_id: UUID) -> None:
    """Remove every row this test's organisation owns (tests share the module DB)."""
    await session.execute(
        delete(AIAttachmentReference).where(
            AIAttachmentReference.organisation_id == organisation_id
        )
    )
    await session.execute(
        delete(AIRequestRecord).where(AIRequestRecord.organisation_id == organisation_id)
    )
    await session.execute(delete(Organisation).where(Organisation.id == organisation_id))
    await session.commit()


async def _audit_actions(session: AsyncSession, organisation_id: UUID) -> set[str]:
    rows = (
        await session.scalars(
            select(AuditEvent.action).where(AuditEvent.organisation_id == organisation_id)
        )
    ).all()
    return set(rows)


async def _sweep(
    session: AsyncSession,
    store: TransferStore,
    *,
    stores: dict[str, TransferStore] | None = None,
    batch_size: int = 50,
    retry_after_seconds: int = 60,
) -> dict[str, int]:
    return await reconciliation.reconcile_provider_file_references(
        session,
        storage=FakeObjectStorage(bucket="feature-bucket"),
        stores=stores if stores is not None else {"fake": store},
        references=SQLTransferReferenceStore(session),
        batch_size=batch_size,
        retry_after_seconds=retry_after_seconds,
    )


async def test_reconcile_claim_deletes_orphan_and_audits(migrated_database: str) -> None:
    """A provider-upload row whose owning AI request row is missing entirely
    (a genuine orphan, v0.8 Scope §5.5/§6.7 checkbox 2) is claimed, deleted and
    audited; the durable row is the proof of cleanup."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            await _enable_ai(session, org.id)
            # No ai_requests row at all: the reference is a true orphan.
            key = await _seed_provider_reference(session, org.id, "req-orphan")
        store = FakeTransferStore()
        async with session_factory() as session:
            summary = await _sweep(session, store)
            assert summary["candidates"] == 1
            assert summary["deleted"] == 1
            assert summary["failed"] == 0
            row = await _row(session, key)
            assert row is not None
            assert row.status == "deleted"
            assert row.deleted_at is not None
            assert row.deletion_attempted_at is None  # cleared on success
            assert row.error_code is None
            actions = await _audit_actions(session, org.id)
            assert "ai.transfer_deleted" in actions
            assert "ai.transfer_reconciled" in actions
            # The backlog gauge predicate matches: nothing eligible remains.
            remaining = await session.scalar(
                ai_attachment_reference_reconciliation_backlog_statement(
                    retry_after=datetime.now(UTC) - timedelta(seconds=60)
                )
            )
            assert remaining == 0
        await _cleanup(session, org.id)
    finally:
        await engine.dispose()


async def test_reconcile_skips_running_request_and_other_modes(migrated_database: str) -> None:
    """Still-running requests, managed-URL rows and GCS staging rows never match."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            await _seed_terminal_request(session, org.id, "req-terminal")
            await _seed_terminal_request(session, org.id, "req-running", status="running")
            provider_key = await _seed_provider_reference(
                session, org.id, "req-terminal", external_id="fake-file-live"
            )
            # A live provider-upload reference of a still-running request: the
            # sweep must never claim a copy an in-flight execution may reuse.
            await _seed_provider_reference(
                session, org.id, "req-running", external_id="fake-file-busy"
            )
            # A managed signed URL has no provider copy and must never match.
            await _seed_provider_reference(
                session,
                org.id,
                "req-terminal",
                mode=TransferMode.MANAGED_SIGNED_URL,
                external_id=f"organisations/{org.id}/documents/doc-1/original",
                source_lifecycle=SourceLifecycle.RETAINED,
            )
            # A Vertex GCS staging object is owned by the deployer lifecycle
            # and must never match the provider-file sweep.
            await _seed_provider_reference(
                session,
                org.id,
                "req-terminal",
                mode=TransferMode.STORAGE_REFERENCE,
                external_id=f"gs://staging-bucket/obj-{uuid.uuid4().hex}",
                source_lifecycle=SourceLifecycle.RETAINED,
            )
        store = FakeTransferStore()
        async with session_factory() as session:
            summary = await _sweep(session, store, batch_size=10)
            assert summary["candidates"] == 1
            assert summary["deleted"] == 1
            row = await _row(session, provider_key)
            assert row is not None
            assert row.status == "deleted"
            # The running-request row is untouched (still live, no attempt).
            busy = (
                await session.scalars(
                    select(AIAttachmentReference).where(
                        AIAttachmentReference.external_id == "fake-file-busy"
                    )
                )
            ).one()
            assert busy.status == "live"
            assert busy.deletion_attempted_at is None
            # The managed-url and GCS rows remain live too.
            others = (
                await session.scalars(
                    select(AIAttachmentReference).where(
                        AIAttachmentReference.idempotency_key != provider_key
                    )
                )
            ).all()
            assert {other.status for other in others} == {"live"}
            assert all(other.deletion_attempted_at is None for other in others)
        await _cleanup(session, org.id)
    finally:
        await engine.dispose()


async def test_reconcile_marks_skipped_modes_failed_and_waits_for_backoff(
    migrated_database: str,
) -> None:
    """A provider with no deployed store fails closed (stamped); a failed delete waits."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            await _seed_terminal_request(session, org.id, "req-a")
            await _seed_terminal_request(session, org.id, "req-b")
            key_a = await _seed_provider_reference(session, org.id, "req-a", external_id="file-a")
            key_b = await _seed_provider_reference(session, org.id, "req-b", external_id="file-b")
        # No store owns provider "fake": every candidate is claimed (stamped)
        # and fails closed — the bounded backoff protects a missing provider
        # from being re-scanned on every scheduled sweep (v0.8 Scope §2.5).
        async with session_factory() as session:
            summary = await _sweep(session, FakeTransferStore(), stores={})
            assert summary["candidates"] == 2
            assert summary["deleted"] == 0
            assert summary["failed"] == 2
            row_a = await _row(session, key_a)
            assert row_a is not None
            assert row_a.deletion_attempted_at is not None  # claimed despite no store
        # A store that fails its provider delete stamps the safe error code
        # and the attempt time; the backoff keeps the rows unclaimed.
        failing = _FailingStore(provider_id="fake", fail_times=2)
        async with session_factory() as session:
            summary = await _sweep(session, failing, retry_after_seconds=0)
            assert summary["candidates"] == 2
            assert summary["deleted"] == 0
            assert summary["failed"] == 2
            row_a = await _row(session, key_a)
            assert row_a is not None
            assert row_a.status == "live"
            assert row_a.error_code == "provider_reference_deletion_failed"
            assert row_a.deletion_attempted_at is not None
            first_attempt = row_a.deletion_attempted_at
        # Still inside the backoff window: nothing is re-claimed.
        async with session_factory() as session:
            summary = await _sweep(session, failing, retry_after_seconds=3_600)
            assert summary["candidates"] == 0
            assert summary["deleted"] == 0
        # Past the window the rows are re-claimed and, with the provider
        # healthy again, deleted through the owning store (the stamp is
        # cleared on success).
        async with session_factory() as session:
            summary = await _sweep(session, failing, retry_after_seconds=0)
            assert summary["candidates"] == 2
            assert summary["deleted"] == 2
            row_a = await _row(session, key_a)
            assert row_a is not None
            assert row_a.status == "deleted"
            assert row_a.error_code is None
            assert row_a.deletion_attempted_at is None
            assert row_a.deleted_at is not None
            assert row_a.deleted_at >= first_attempt if first_attempt is not None else True
            row_b = await _row(session, key_b)
            assert row_b is not None
            assert row_b.status == "deleted"
        await _cleanup(session, org.id)
    finally:
        await engine.dispose()


async def test_reconcile_is_bounded_by_batch_size(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            for index in range(5):
                request_id = f"req-batch-{index}"
                await _seed_terminal_request(session, org.id, request_id)
                await _seed_provider_reference(session, org.id, request_id)
        store = FakeTransferStore()
        async with session_factory() as session:
            summary = await _sweep(session, store, batch_size=2)
            assert summary["candidates"] == 2
            assert summary["deleted"] == 2
        async with session_factory() as session:
            summary = await _sweep(session, store, batch_size=2)
            assert summary["candidates"] == 2
            assert summary["deleted"] == 2
        async with session_factory() as session:
            summary = await _sweep(session, store, batch_size=2)
            assert summary["candidates"] == 1
            assert summary["deleted"] == 1
        async with session_factory() as session:
            remaining = await session.scalar(
                ai_attachment_reference_reconciliation_backlog_statement(
                    retry_after=datetime.now(UTC) - timedelta(seconds=60)
                )
            )
            assert remaining == 0
        await _cleanup(session, org.id)
    finally:
        await engine.dispose()


async def test_reconcile_never_touches_the_feature_source(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            await _seed_terminal_request(session, org.id, "req-src")
            await _seed_provider_reference(session, org.id, "req-src")
        source = FakeObjectStorage(bucket="feature-bucket")
        key = f"organisations/{org.id}/documents/doc-1/original"
        await source.put(key, b"%PDF-1.7 keep" * 10, content_type="application/pdf")
        store = FakeTransferStore()
        async with session_factory() as session:
            summary = await _sweep(session, store, batch_size=10)
            assert summary["deleted"] == 1
            assert await source.head_object(key) is not None  # feature source untouched
        await _cleanup(session, org.id)
    finally:
        await engine.dispose()


async def test_reconcile_fresh_adapter_issues_provider_delete_from_durable_row(
    migrated_database: str,
) -> None:
    """A freshly constructed provider adapter (empty in-process cache) deletes
    the provider file named by the durable row *before* the row is marked
    deleted — the restart-safe guarantee of v0.8 Scope §2.5/§6.7 checkbox 2."""
    deleted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted_paths.append(request.url.path)
        return httpx.Response(200, json={"deleted": True})

    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            await _enable_ai(session, org.id)
            # A genuine orphan (no owning request row) carrying the provider
            # file id the OpenAI adapter will DELETE.
            key = await _seed_provider_reference(
                session,
                org.id,
                "req-orphan-openai",
                provider="openai",
                external_id="file-orphan-abc123",
            )
        fresh = OpenAITransferStore(
            api_key="sk-test",
            upload_expiry_seconds=3_600,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            async with session_factory() as session:
                summary = await _sweep(session, fresh, stores={"openai": fresh})
                assert summary["candidates"] == 1
                assert summary["deleted"] == 1
                assert summary["failed"] == 0
                row = await _row(session, key)
                assert row is not None
                assert row.status == "deleted"
        finally:
            await fresh.aclose()
        # The provider DELETE was issued from the durable row alone — a fresh
        # adapter with no process-local stage history still deletes the copy —
        # and the row was only then marked deleted.
        assert deleted_paths == ["/v1/files/file-orphan-abc123"]
        await _cleanup(session, org.id)
    finally:
        await engine.dispose()


async def test_reconcile_concurrent_workers_claim_disjoint_batches(
    migrated_database: str,
) -> None:
    """Two concurrent sweep workers claim disjoint batches: every provider file
    is deleted exactly once (atomic ``FOR UPDATE SKIP LOCKED`` claim)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            for index in range(6):
                await _seed_terminal_request(session, org.id, f"req-conc-{index}")
                await _seed_provider_reference(
                    session, org.id, f"req-conc-{index}", external_id=f"file-{index}"
                )
        store = _RecordingDeleteStore()
        async with session_factory() as session_a, session_factory() as session_b:
            results = await asyncio.gather(
                _sweep(session_a, store, batch_size=3),
                _sweep(session_b, store, batch_size=3),
            )
        assert sum(result["deleted"] for result in results) == 6
        assert sum(result["failed"] for result in results) == 0
        # No provider file was deleted twice — the atomic claim made the two
        # workers' candidate sets disjoint.
        assert len(store.deleted_external_ids) == 6
        assert len(set(store.deleted_external_ids)) == 6
        async with session_factory() as session:
            rows = (
                await session.scalars(
                    select(AIAttachmentReference).where(
                        AIAttachmentReference.organisation_id == org.id
                    )
                )
            ).all()
            assert all(row.status == "deleted" for row in rows)
        await _cleanup(session, org.id)
    finally:
        await engine.dispose()


async def test_reconcile_actor_drives_fresh_runtime_store(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registered §6.7 reconciliation handler, consumed by a real worker
    from the ``ai`` queue, claims orphaned provider files and deletes them
    through a freshly constructed runtime transfer store — the actor/
    database integration evidence v0.8 Scope §6.7 checkbox 4 requires."""
    from app.ai.persistence.tasks import reconcile_provider_file_references
    from app.broker import worker_middleware

    deleted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted_paths.append(request.url.path)
        return httpx.Response(200, json={"deleted": True})

    fresh = OpenAITransferStore(
        api_key="sk-test",
        upload_expiry_seconds=3_600,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr("app.ai.runtime.get_transfer_stores", lambda: {"openai": fresh})

    broker = StubBroker(middleware=worker_middleware())
    dramatiq.set_broker(broker)
    actor = dramatiq.actor(queue_name="ai", max_retries=3)(reconcile_provider_file_references)
    worker = Worker(broker, worker_timeout=100, worker_threads=2)
    worker.start()
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            await _enable_ai(session, org.id)
            key = await _seed_provider_reference(
                session,
                org.id,
                "req-orphan-actor",
                provider="openai",
                external_id="file-actor-xyz",
            )
        actor.send()
        deadline = asyncio.get_running_loop().time() + 20
        row_status: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            async with session_factory() as session:
                row = await _row(session, key)
                if row is not None and row.status == "deleted":
                    row_status = row.status
                    break
            await asyncio.sleep(0.2)
        assert row_status == "deleted"
        # The worker built a fresh adapter (empty cache) and still issued the
        # provider DELETE from the durable row before marking it deleted.
        assert deleted_paths == ["/v1/files/file-actor-xyz"]
        await _cleanup(session, org.id)
    finally:
        worker.stop()
        broker.flush_all()
        await fresh.aclose()
        await engine.dispose()
        from app.db.session import engine as app_engine

        await app_engine.dispose()


class _ProviderUploadPort:
    """Minimal :class:`AIPersistencePort` permitting the provider-upload mode."""

    def __init__(self) -> None:
        self.policy = OrganisationAIPolicy(
            enabled=True,
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
        )

    async def load_policy(self, *, organisation_id: object) -> OrganisationAIPolicy:
        return self.policy

    async def reserve(self, **kwargs: object) -> AIRequestReservation:
        return AIRequestReservation(row_id=uuid.uuid4(), created=True)

    async def record_attempt(self, **kwargs: object) -> UUID:
        return uuid.uuid4()

    async def settle(self, **kwargs: object) -> None:
        return None


async def test_transfer_lifecycle_audit_at_the_database(migrated_database: str) -> None:
    """A real-DB execution records the full transfer-lifecycle audit trail —
    ``ai.transfer_selected`` / ``ai.transfer_staged`` / ``ai.transfer_expired``
    / ``ai.transfer_deleted`` — and leaves exactly one terminal reference row
    (v0.8 Scope §6.7 checkboxes 3-4)."""
    bundle = load_registry_bundle()
    storage = FakeObjectStorage(bucket="audit-test")
    fake_provider = FakeLLMProvider()
    store = FakeTransferStore()
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            await _enable_ai(session, org.id)
            service = AIService(
                task_registry=bundle.tasks,
                prompt_registry=bundle.prompts,
                model_registry=bundle.models,
                providers={"fake": fake_provider},
                attachment_resolver=StorageAttachmentResolver(storage),
                transfer_deployment=TransferDeploymentPolicy(
                    inline_aggregate_threshold_bytes=5_000_000,
                    max_large_attachment_bytes=50_000_000,
                    enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD}),
                ),
                storage=storage,
                transfer_stores={"fake": store},
            )
            key = f"organisations/{org.id}/ai/scratch/doc-{uuid.uuid4().hex[:8]}.pdf"
            payload = b"%PDF-1.4\n%%EOF\n" + b"x" * (MAX_ATTACHMENT_BYTES + 1024)
            await storage.put(key, payload, content_type="application/pdf")
            result = await service.execute(
                AIRequest(
                    task="document.ask",
                    storage_reference=key,
                    organisation_id=org.id,
                    user_id=uuid.uuid4(),
                    metadata={"question": "What is in this document?"},
                ),
                recorder=_ProviderUploadPort(),
                transfer_references=SQLTransferReferenceStore(session),
                execution_session=session,
                request_id="req-audit-lifecycle",
            )
            assert isinstance(result.output, str) and result.output
            # Exactly one provider copy was staged and deleted after terminal
            # success (Scope §2.5).
            assert len(store.records) == 1
            assert len(store.deleted) == 1
            actions = await _audit_actions(session, org.id)
            for action in (
                "ai.transfer_selected",
                "ai.transfer_staged",
                "ai.transfer_expired",
                "ai.transfer_deleted",
            ):
                assert action in actions, f"missing audit event {action!r}: {sorted(actions)}"
            # The durable reference is terminal: expired then deleted.
            rows = (
                await session.scalars(
                    select(AIAttachmentReference).where(
                        AIAttachmentReference.organisation_id == org.id
                    )
                )
            ).all()
            assert len(rows) == 1
            assert rows[0].status == "deleted"
        await _cleanup(session, org.id)
    finally:
        await engine.dispose()


async def test_transfer_reuse_is_audited_at_the_database(migrated_database: str) -> None:
    """A retry that finds the live matching durable reference reuses it and
    records ``ai.transfer_reused`` instead of staging a second provider copy
    (v0.8 Scope §2.1 retry-only reuse, §6.7 checkbox 3)."""
    from app.modules.audit.service import record_event

    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            await _enable_ai(session, org.id)
            store = FakeTransferStore()

            async def _record(
                action: str,
                resource_type: str,
                resource_id: str,
                organisation_id: UUID,
                metadata: dict[str, object] | None = None,
            ) -> None:
                await record_event(
                    session,
                    action=action,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    organisation_id=organisation_id,
                    metadata=metadata,
                )

            from app.ai.transfer_orchestrator import TransferOrchestrator

            orchestrator = TransferOrchestrator(
                storage=FakeObjectStorage(bucket="reuse-test"),
                store=store,
                references=SQLTransferReferenceStore(session),
                audit_recorder=_record,
            )
            staged = await orchestrator.create_or_reuse_reference(
                organisation_id=org.id,
                logical_request_id="req-reuse",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_reference=f"organisations/{org.id}/documents/doc-1/original",
                source_digest=_digest("reuse-digest"),
                size_bytes=6_000_000,
                mime_type="application/pdf",
                source_lifecycle=SourceLifecycle.TRANSIENT,
                region="eu-west-1",
                expires_at=None,
            )
            hit = await orchestrator.find_reusable_reference(
                organisation_id=org.id,
                logical_request_id="req-reuse",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest=_digest("reuse-digest"),
                region="eu-west-1",
            )
            assert hit is not None and hit.idempotency_key == staged.idempotency_key
            # Only one provider copy was ever staged (retry-only reuse).
            assert len(store.records) == 1
            # A changed digest never reuses (Scope §5.4): a new transfer.
            miss = await orchestrator.find_reusable_reference(
                organisation_id=org.id,
                logical_request_id="req-reuse",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest=_digest("changed-digest"),
                region="eu-west-1",
            )
            assert miss is None
            actions = await _audit_actions(session, org.id)
            assert "ai.transfer_staged" in actions
            assert "ai.transfer_reused" in actions
        await _cleanup(session, org.id)
    finally:
        await engine.dispose()


class _FailingStore(FakeTransferStore):
    """A store whose provider delete raises the next ``fail_times`` calls."""

    def __init__(self, *, provider_id: str, fail_times: int) -> None:
        super().__init__()
        self.provider_id = provider_id
        self._failures_left = fail_times

    async def delete(self, reference: ExternalFileReference) -> None:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise RuntimeError("provider unavailable")
        await super().delete(reference)


class _RecordingDeleteStore(FakeTransferStore):
    """A store that records every provider delete request it receives.

    Mirrors the real adapters' fresh-adapter contract: the delete is driven
    purely from the durable row, never from process-local stage history.
    """

    def __init__(self) -> None:
        super().__init__()
        self.deleted_external_ids: list[str] = []

    async def delete(self, reference: ExternalFileReference) -> None:
        self.deleted_external_ids.append(reference.external_id)
        await super().delete(reference)
