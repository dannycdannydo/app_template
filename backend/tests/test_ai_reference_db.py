"""Real-database integration tests for the durable transfer reference (v0.8 Scope §6.3).

The unit suites prove the streaming seam and the fake-store contract but never
execute SQL, so the organisation-scoped ``ai_attachment_references`` table, the
retry-only reuse idempotency, the concurrent-duplicate adoption and the
"AI cleanup never deletes the feature source" guarantee could silently
regress. These tests run the real migration and the real
:class:`~app.ai.persistence.references.SQLTransferReferenceStore` plus the
:class:`~app.ai.transfer_orchestrator.TransferOrchestrator` against a
reachable PostgreSQL, using the same skip pattern as the other ``*_db.py``
modules: migrated to head up front, reverted to base afterwards.

Coverage maps to Scope §6.3 checkbox 5: cross-org denial, concurrent duplicate
creation, digest change, expired reference replacement, forbidden persisted
fields and rollback/error paths, plus the checkbox-4 proof that deletion of a
provider copy never touches the feature-owned source object.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.errors import TransferExecutionUnavailableError
from app.ai.persistence.models import AIAttachmentReference
from app.ai.persistence.references import SQLTransferReferenceStore
from app.ai.staging import ExternalFileReference, ExternalReferenceStatus, FakeTransferStore
from app.ai.transfer import (
    MANAGED_URL_DEFAULT_TTL_SECONDS,
    MANAGED_URL_MAX_TTL_SECONDS,
    SourceLifecycle,
    TransferMode,
    derive_idempotency_key,
)
from app.ai.transfer_orchestrator import TransferOrchestrator
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
    """Create one fresh organisation (unique per test, so rows never collide)."""
    organisation = Organisation(name=f"Transfer Org {uuid.uuid4().hex[:8]}")
    session.add(organisation)
    await session.commit()
    return organisation


def _source_key(organisation_id: UUID) -> str:
    return f"organisations/{organisation_id}/documents/lease.pdf"


def _staged_reference(
    *,
    organisation_id: UUID,
    mode: TransferMode,
    source_digest: str,
    region: str = "eu-west-1",
    logical_request_id: str = "req-1",
    external_id: str | None = None,
    expires_at: datetime | None = None,
) -> ExternalFileReference:
    """A staged (provider-side) reference shaped like a store's return value."""
    key = _source_key(organisation_id)
    return ExternalFileReference(
        mode=mode,
        provider="fake",
        external_id=external_id or f"fake-{mode.value}-{source_digest[:16]}",
        source_reference=key,
        source_digest=source_digest,
        size_bytes=1600,
        mime_type="application/pdf",
        source_lifecycle=SourceLifecycle.TRANSIENT
        if mode is TransferMode.PROVIDER_UPLOAD
        else SourceLifecycle.RETAINED,
        region=region,
        organisation_id=organisation_id,
        logical_request_id=logical_request_id,
        idempotency_key=derive_idempotency_key(
            provider="fake",
            mode=mode,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            source_digest=source_digest,
            region=region,
        ),
        created_at=datetime.now(UTC),
        expires_at=expires_at,
    )


async def _live_rows(
    session: AsyncSession, organisation_id: UUID, logical_request_id: str
) -> list[AIAttachmentReference]:
    return list(
        (
            await session.scalars(
                select(AIAttachmentReference).where(
                    AIAttachmentReference.organisation_id == organisation_id,
                    AIAttachmentReference.logical_request_id == logical_request_id,
                    AIAttachmentReference.status == "live",
                )
            )
        ).all()
    )


async def _all_rows(
    session: AsyncSession, organisation_id: UUID, logical_request_id: str
) -> list[AIAttachmentReference]:
    return list(
        (
            await session.scalars(
                select(AIAttachmentReference).where(
                    AIAttachmentReference.organisation_id == organisation_id,
                    AIAttachmentReference.logical_request_id == logical_request_id,
                )
            )
        ).all()
    )


# --- Table contract and forbidden fields -------------------------------------


async def test_reference_table_has_no_managed_url_columns(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            columns = set(
                (
                    await session.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'ai_attachment_references'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "url" not in columns
            assert "query_string" not in columns
            assert "bytes" not in columns
            assert "external_id" in columns
            assert "idempotency_key" in columns
    finally:
        await engine.dispose()


async def test_external_file_reference_forbids_extra_fields() -> None:
    reference = _staged_reference(
        organisation_id=uuid.uuid4(), mode=TransferMode.PROVIDER_UPLOAD, source_digest="b" * 64
    ).model_dump()
    reference["managed_url"] = "https://storage.example.invalid/signed?X-Amz-Signature=abc"
    with pytest.raises(ValidationError):
        ExternalFileReference(**reference)


# --- SQLTransferReferenceStore lifecycle ------------------------------------


async def test_create_persists_safe_durable_fields(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            reference = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="c" * 64,
            )
            stored = await SQLTransferReferenceStore(session).create_or_adopt(reference)
            assert stored.idempotency_key == reference.idempotency_key
            assert stored.external_id == reference.external_id
            rows = await _all_rows(session, org.id, "req-1")
            assert len(rows) == 1
            row = rows[0]
            assert row.source_digest == "c" * 64
            assert row.source_reference == reference.source_reference
            assert row.transfer_mode == "provider_upload"
            assert row.source_lifecycle == "transient"
            assert row.region == "eu-west-1"
            assert row.size_bytes == 1600
            assert row.mime_type == "application/pdf"
    finally:
        await engine.dispose()


async def test_retry_reuses_one_live_row_and_adopts(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            store = SQLTransferReferenceStore(session)
            reference = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="d" * 64,
            )
            first = await store.create_or_adopt(reference)
            second = await store.create_or_adopt(reference)
            # The same durable reference is adopted, never duplicated.
            assert second.idempotency_key == first.idempotency_key
            rows = await _live_rows(session, org.id, "req-1")
            assert len(rows) == 1
            assert rows[0].last_used_at is not None
    finally:
        await engine.dispose()


async def test_digest_change_creates_a_new_idempotent_transfer(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            store = SQLTransferReferenceStore(session)
            old = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="e1" * 32,
            )
            await store.create_or_adopt(old)
            new = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="e2" * 32,
            )
            changed = await store.create_or_adopt(new)
            assert changed.idempotency_key != old.idempotency_key
            # A retry carrying the OLD digest still reuses its own live record
            # (retry-only reuse), while the NEW digest maps to the new transfer.
            old_hit = await store.find_live(
                organisation_id=org.id,
                logical_request_id="req-1",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="e1" * 32,
                region="eu-west-1",
            )
            assert old_hit is not None and old_hit.idempotency_key == old.idempotency_key
            new_hit = await store.find_live(
                organisation_id=org.id,
                logical_request_id="req-1",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="e2" * 32,
                region="eu-west-1",
            )
            assert new_hit is not None and new_hit.idempotency_key == new.idempotency_key
            rows = await _live_rows(session, org.id, "req-1")
            assert {row.source_digest for row in rows} == {"e1" * 32, "e2" * 32}
    finally:
        await engine.dispose()


async def test_find_live_returns_only_the_matching_live_reference(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            store = SQLTransferReferenceStore(session)
            reference = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="e" * 64,
            )
            await store.create_or_adopt(reference)
            hit = await store.find_live(
                organisation_id=org.id,
                logical_request_id="req-1",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="e" * 64,
                region="eu-west-1",
            )
            assert hit is not None and hit.idempotency_key == reference.idempotency_key
            # A changed digest is a different transfer: no reusable record.
            miss = await store.find_live(
                organisation_id=org.id,
                logical_request_id="req-1",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="f" * 64,
                region="eu-west-1",
            )
            assert miss is None
    finally:
        await engine.dispose()


async def test_cross_organisation_denial(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org_a = await _seed_organisation(session)
            org_b = await _seed_organisation(session)
        async with session_factory() as session:
            await SQLTransferReferenceStore(session).create_or_adopt(
                _staged_reference(
                    organisation_id=org_a.id,
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_digest="a1" * 32,
                )
            )
        # The same idempotency key in another organisation is invisible: reuse
        # reports no row, and a new transfer is allowed under the same key.
        async with session_factory() as session:
            store = SQLTransferReferenceStore(session)
            other = await store.find_live(
                organisation_id=org_b.id,
                logical_request_id="req-1",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="a1" * 32,
                region="eu-west-1",
            )
            assert other is None
            await store.create_or_adopt(
                _staged_reference(
                    organisation_id=org_b.id,
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_digest="a1" * 32,
                )
            )
            rows_b = await _live_rows(session, org_b.id, "req-1")
            assert len(rows_b) == 1
    finally:
        await engine.dispose()


async def test_expired_reference_is_replaced_by_a_new_live_row(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            store = SQLTransferReferenceStore(session)
            past = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="cafe" * 16,
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            await store.create_or_adopt(past)
            # Time-expired: reuse reports missing and marks the row expired.
            hit = await store.find_live(
                organisation_id=org.id,
                logical_request_id="req-1",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="cafe" * 16,
                region="eu-west-1",
            )
            assert hit is None
            expired_rows = await _all_rows(session, org.id, "req-1")
            assert len(expired_rows) == 1 and expired_rows[0].status == "expired"
            # The replacement inserts a new live row: the expired row no
            # longer blocks the idempotency (partial unique index).
            future = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="cafe" * 16,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            replacement = await store.create_or_adopt(future)
            assert replacement.idempotency_key == past.idempotency_key
            all_rows = await _all_rows(session, org.id, "req-1")
            assert len(all_rows) == 2  # the expired row plus its live replacement
            live = await _live_rows(session, org.id, "req-1")
            assert len(live) == 1
            assert live[0].expires_at is not None and live[0].expires_at > datetime.now(UTC)
    finally:
        await engine.dispose()


async def test_mark_expired_mark_deleted_and_expire_all(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            store = SQLTransferReferenceStore(session)
            one = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="aa11" * 16,
            )
            await store.create_or_adopt(one)
            assert await store.mark_deleted(
                organisation_id=org.id, idempotency_key=one.idempotency_key
            )
            rows = await _all_rows(session, org.id, "req-1")
            assert rows[0].status == "deleted" and rows[0].deleted_at is not None
            # A second delete of the same (now terminal) row is a no-op.
            assert not await store.mark_deleted(
                organisation_id=org.id, idempotency_key=one.idempotency_key
            )
            two = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.MANAGED_SIGNED_URL,
                source_digest="bb22" * 16,
            )
            await store.create_or_adopt(two)
            assert (
                await store.expire_all_for_request(
                    organisation_id=org.id, logical_request_id="req-1"
                )
                == 1
            )
            live = await _live_rows(session, org.id, "req-1")
            assert live == []
    finally:
        await engine.dispose()


async def test_list_for_request_is_org_scoped(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org_a = await _seed_organisation(session)
            org_b = await _seed_organisation(session)
        async with session_factory() as session:
            await SQLTransferReferenceStore(session).create_or_adopt(
                _staged_reference(
                    organisation_id=org_a.id,
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_digest="1111" * 16,
                )
            )
            await SQLTransferReferenceStore(session).create_or_adopt(
                _staged_reference(
                    organisation_id=org_b.id,
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_digest="2222" * 16,
                )
            )
        async with session_factory() as session:
            store = SQLTransferReferenceStore(session)
            assert (
                len(
                    await store.list_for_request(
                        organisation_id=org_a.id, logical_request_id="req-1"
                    )
                )
                == 1
            )
            assert (
                len(
                    await store.list_for_request(
                        organisation_id=org_b.id, logical_request_id="req-1"
                    )
                )
                == 1
            )
    finally:
        await engine.dispose()


# --- Orchestrator: create/adopt/reuse/expire/delete --------------------------


async def _orchestrator(
    session: AsyncSession,
    *,
    storage: FakeObjectStorage | None = None,
    store: FakeTransferStore | None = None,
    references: SQLTransferReferenceStore | None = None,
) -> tuple[TransferOrchestrator, FakeObjectStorage, FakeTransferStore]:
    storage = storage or FakeObjectStorage(bucket="test-bucket")
    store = store or FakeTransferStore()
    orchestrator = TransferOrchestrator(
        storage=storage,
        store=store,
        references=references or SQLTransferReferenceStore(session),
    )
    return orchestrator, storage, store


async def test_orchestrator_create_reuse_and_store_recreation(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            orchestrator, _, store = await _orchestrator(session)
            args: dict[str, Any] = {
                "organisation_id": org.id,
                "logical_request_id": "req-orch",
                "provider_id": "fake",
                "mode": TransferMode.PROVIDER_UPLOAD,
                "source_reference": _source_key(org.id),
                "source_digest": "abcd" * 16,
                "size_bytes": 1600,
                "mime_type": "application/pdf",
                "source_lifecycle": SourceLifecycle.TRANSIENT,
                "region": "eu-west-1",
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
            first = await orchestrator.create_or_reuse_reference(**args)
            # A retry of the same logical transfer reuses the same reference.
            retry = await orchestrator.create_or_reuse_reference(**args)
            assert retry.idempotency_key == first.idempotency_key
            rows = await _live_rows(session, org.id, "req-orch")
            assert len(rows) == 1
            # The provider-side copy expired: the next stage recreates it and
            # the durable row is refreshed in place (still exactly one live row).
            store.expire_due(now=datetime.now(UTC) + timedelta(hours=2))
            recreated = await orchestrator.create_or_reuse_reference(**args)
            assert recreated.external_id != first.external_id
            rows = await _live_rows(session, org.id, "req-orch")
            assert len(rows) == 1 and rows[0].external_id == recreated.external_id
    finally:
        await engine.dispose()


async def test_orchestrator_managed_url_mode_builds_reference_without_store_copy(
    migrated_database: str,
) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            orchestrator, storage, store = await _orchestrator(session)
            key = _source_key(org.id)
            content = b"%PDF-1.7" * 100
            await storage.create_upload_url(
                file_id=uuid.uuid4(),
                object_key=key,
                content_type="application/pdf",
                size_bytes=len(content),
            )
            await storage.put(key, content, content_type="application/pdf")
            reference = await orchestrator.create_or_reuse_reference(
                organisation_id=org.id,
                logical_request_id="req-managed",
                provider_id="fake",
                mode=TransferMode.MANAGED_SIGNED_URL,
                source_reference=key,
                source_digest=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                mime_type="application/pdf",
                source_lifecycle=SourceLifecycle.RETAINED,
                region="eu-west-1",
                expires_at=None,
            )
            assert reference.external_id == key
            assert store.records == []  # no provider copy was staged
            signed = await orchestrator.mint_managed_url(reference=reference)
            assert signed.method == "GET" and signed.url.startswith("https://")
            remaining = (signed.expires_at - datetime.now(UTC)).total_seconds()
            assert MANAGED_URL_DEFAULT_TTL_SECONDS - 5 <= remaining <= MANAGED_URL_MAX_TTL_SECONDS
    finally:
        await engine.dispose()


async def test_orchestrator_refuses_inline_reference(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            orchestrator, _, _ = await _orchestrator(session)
            with pytest.raises(TransferExecutionUnavailableError, match="no durable reference"):
                await orchestrator.create_or_reuse_reference(
                    organisation_id=org.id,
                    logical_request_id="req-inline",
                    provider_id="fake",
                    mode=TransferMode.INLINE,
                    source_reference=_source_key(org.id),
                    source_digest="dead" * 16,
                    size_bytes=1600,
                    mime_type="application/pdf",
                    source_lifecycle=SourceLifecycle.TRANSIENT,
                    region="eu-west-1",
                    expires_at=None,
                )
    finally:
        await engine.dispose()


async def test_orchestrator_delete_never_touches_the_feature_source(
    migrated_database: str,
) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            orchestrator, storage, store = await _orchestrator(session)
            key = _source_key(org.id)
            content = b"%PDF-1.7 delete-proof" * 100
            await storage.create_upload_url(
                file_id=uuid.uuid4(),
                object_key=key,
                content_type="application/pdf",
                size_bytes=len(content),
            )
            await storage.put(key, content, content_type="application/pdf")
            reference = await orchestrator.create_or_reuse_reference(
                organisation_id=org.id,
                logical_request_id="req-delete",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_reference=key,
                source_digest="beef" * 16,
                size_bytes=len(content),
                mime_type="application/pdf",
                source_lifecycle=SourceLifecycle.TRANSIENT,
                region="eu-west-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await orchestrator.delete_reference(reference=reference)
            # The AI-owned provider copy was deleted through the store…
            assert len(store.deleted) == 1
            # …but the feature-owned source object is untouched.
            assert await storage.head_object(key) is not None
            rows = await _all_rows(session, org.id, "req-delete")
            assert rows[0].status == "deleted"
    finally:
        await engine.dispose()


async def test_orchestrator_request_scoped_expire_and_delete(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            orchestrator, storage, store = await _orchestrator(session)
            key = _source_key(org.id)
            content = b"%PDF-1.7 sweep" * 100
            await storage.create_upload_url(
                file_id=uuid.uuid4(),
                object_key=key,
                content_type="application/pdf",
                size_bytes=len(content),
            )
            await storage.put(key, content, content_type="application/pdf")
            for digest in ("aaaa" * 16, "bbbb" * 16):
                await orchestrator.create_or_reuse_reference(
                    organisation_id=org.id,
                    logical_request_id="req-sweep",
                    provider_id="fake",
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_reference=key,
                    source_digest=digest,
                    size_bytes=len(content),
                    mime_type="application/pdf",
                    source_lifecycle=SourceLifecycle.TRANSIENT,
                    region="eu-west-1",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            assert (
                await orchestrator.expire_references_for_request(
                    organisation_id=org.id, logical_request_id="req-sweep"
                )
                == 2
            )
            assert await _live_rows(session, org.id, "req-sweep") == []
            # Terminal cleanup composes safely after expiry: the sweep resolves
            # the authoritative (now expired) rows and still deletes the
            # provider copies, so an expire-then-delete sequence cannot strand
            # copies for the §6.7 reconciliation job.
            assert (
                await orchestrator.delete_references_for_request(
                    organisation_id=org.id, logical_request_id="req-sweep"
                )
                == 2
            )
            assert len(store.deleted) == 2
            assert await storage.head_object(key) is not None  # source untouched
            rows = await _all_rows(session, org.id, "req-sweep")
            assert {row.status for row in rows} == {"deleted"}
    finally:
        await engine.dispose()


async def test_cleanup_continues_after_one_delete_failure(migrated_database: str) -> None:
    """A failed provider deletion must not block the immediate best-effort
    attempt of the remaining references of the same logical request (Scope §6.7
    checkbox 1): the failed row keeps its safe failure marker, the later copy
    is still deleted, and the feature-owned source object is untouched."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)

        class _FlakyStore(FakeTransferStore):
            """Fails the first provider delete this process sees, then succeeds."""

            def __init__(self) -> None:
                super().__init__()
                self._delete_failures_left = 1
                self.deleted_external_ids: list[str] = []

            async def delete(self, reference: ExternalFileReference) -> None:
                self.deleted_external_ids.append(reference.external_id)
                if self._delete_failures_left > 0:
                    self._delete_failures_left -= 1
                    raise TransferExecutionUnavailableError("provider delete unavailable")
                await super().delete(reference)

        async with session_factory() as session:
            flaky_store = _FlakyStore()
            orchestrator, storage, store = await _orchestrator(session, store=flaky_store)
            key = _source_key(org.id)
            content = b"%PDF-1.7 continue" * 100
            await storage.create_upload_url(
                file_id=uuid.uuid4(),
                object_key=key,
                content_type="application/pdf",
                size_bytes=len(content),
            )
            await storage.put(key, content, content_type="application/pdf")
            # Two provider copies of one logical request (different digests).
            first_key: str | None = None
            for digest in ("1111" * 16, "2222" * 16):
                reference = await orchestrator.create_or_reuse_reference(
                    organisation_id=org.id,
                    logical_request_id="req-continue",
                    provider_id="fake",
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_reference=key,
                    source_digest=digest,
                    size_bytes=len(content),
                    mime_type="application/pdf",
                    source_lifecycle=SourceLifecycle.TRANSIENT,
                    region="eu-west-1",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
                if first_key is None:
                    first_key = reference.idempotency_key
            # The flaky store fails the FIRST delete attempt (rows are iterated
            # in creation order); the sweep must continue to the second copy.
            assert first_key is not None
            assert (
                await orchestrator.delete_references_for_request(
                    organisation_id=org.id, logical_request_id="req-continue"
                )
                == 1
            )
            # Both provider copies were attempted; the second was deleted.
            assert len(flaky_store.deleted_external_ids) == 2
            assert len(store.deleted) == 1
            rows = await _all_rows(session, org.id, "req-continue")
            by_key = {row.idempotency_key: row for row in rows}
            failed_row = by_key[first_key]
            assert failed_row.status == "live"
            assert failed_row.error_code == "provider_reference_deletion_failed"
            assert failed_row.deletion_attempted_at is not None
            assert {row.status for row in rows if row is not failed_row} == {"deleted"}
            # The feature-owned source object is untouched.
            assert await storage.head_object(key) is not None
    finally:
        await engine.dispose()


async def test_restart_after_crash_cleans_reference_from_durable_row(
    migrated_database: str,
) -> None:
    """A worker crash between staging and terminal cleanup leaves a live
    durable row; a restarted orchestrator with a fresh store resolves it from
    the durable row alone and completes the terminal cleanup (Scope §2.5/§6.7
    checkbox 1/4 crash recovery)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
            orchestrator, storage, store = await _orchestrator(session)
            key = _source_key(org.id)
            content = b"%PDF-1.7 crash" * 100
            await storage.create_upload_url(
                file_id=uuid.uuid4(),
                object_key=key,
                content_type="application/pdf",
                size_bytes=len(content),
            )
            await storage.put(key, content, content_type="application/pdf")
            await orchestrator.create_or_reuse_reference(
                organisation_id=org.id,
                logical_request_id="req-crash",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_reference=key,
                source_digest="cafe" * 16,
                size_bytes=len(content),
                mime_type="application/pdf",
                source_lifecycle=SourceLifecycle.TRANSIENT,
                region="eu-west-1",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            assert len(store.records) == 1
        # Crash: no finalize ran; the durable row is still live after the
        # session boundary (a fresh process would see exactly this).
        async with session_factory() as session:
            rows = await _all_rows(session, org.id, "req-crash")
            assert len(rows) == 1
            assert rows[0].status == "live"
        # Restart: a fresh orchestrator with a fresh store (empty in-process
        # cache) resolves the authoritative row from the durable record and
        # completes the terminal cleanup.
        async with session_factory() as session:
            restarted, _, fresh_store = await _orchestrator(session)
            result = await restarted.finalize_request_references(
                organisation_id=org.id, logical_request_id="req-crash"
            )
            assert result.expired == 1
            assert result.deleted == 1
            # The fresh store deleted the copy purely from the durable row
            # (FakeTransferStore.delete no-ops without a staged record, but
            # the durable row — the proof of cleanup — is terminal; the real
            # adapters issue the provider DELETE from the same durable row,
            # covered by the reconcile fresh-adapter test).
            rows = await _all_rows(session, org.id, "req-crash")
            assert rows[0].status == "deleted"
            assert rows[0].deletion_attempted_at is None
            assert await storage.head_object(key) is not None  # source untouched
            _ = fresh_store
    finally:
        await engine.dispose()


async def test_concurrent_duplicate_creation_yields_one_live_row(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        shared_store = FakeTransferStore()

        async def _create() -> None:
            async with session_factory() as session:
                orchestrator, _, _ = await _orchestrator(session, store=shared_store)
                await orchestrator.create_or_reuse_reference(
                    organisation_id=org.id,
                    logical_request_id="req-race",
                    provider_id="fake",
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_reference=_source_key(org.id),
                    source_digest="c0de" * 16,
                    size_bytes=1600,
                    mime_type="application/pdf",
                    source_lifecycle=SourceLifecycle.TRANSIENT,
                    region="eu-west-1",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )

        await asyncio.gather(_create(), _create())
        async with session_factory() as session:
            rows = await _live_rows(session, org.id, "req-race")
            assert len(rows) == 1
    finally:
        await engine.dispose()


async def test_stage_failure_rolls_back_without_a_durable_row(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)

        class _BrokenStore(FakeTransferStore):
            async def stage(self, **kwargs: Any) -> ExternalFileReference:
                raise TransferExecutionUnavailableError("provider upload failed")

        async with session_factory() as session:
            orchestrator, _, _ = await _orchestrator(session, store=_BrokenStore())
            with pytest.raises(TransferExecutionUnavailableError, match="upload failed"):
                await orchestrator.create_or_reuse_reference(
                    organisation_id=org.id,
                    logical_request_id="req-rollback",
                    provider_id="fake",
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_reference=_source_key(org.id),
                    source_digest="c0de" * 16,
                    size_bytes=1600,
                    mime_type="application/pdf",
                    source_lifecycle=SourceLifecycle.TRANSIENT,
                    region="eu-west-1",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
        async with session_factory() as session:
            assert await _all_rows(session, org.id, "req-rollback") == []
    finally:
        await engine.dispose()


# --- Review fixes: compensation, authoritative deletion, live-row adoption ---


async def test_persistence_failure_after_successful_stage_compensates(
    migrated_database: str,
) -> None:
    """Must-fix: a staged provider copy is deleted when the durable row fails.

    The provider copy is staged first; if persistence then fails, the AI-owned
    copy must not be left untracked (no row for the §6.7 reconciliation job to
    find) — the orchestrator compensates with a best-effort delete through the
    provider-neutral store.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)

        class _BrokenReferenceStore(SQLTransferReferenceStore):
            async def create_or_adopt(
                self, reference: ExternalFileReference
            ) -> ExternalFileReference:
                raise TransferExecutionUnavailableError("durable persistence failed")

        async with session_factory() as session:
            orchestrator, _, store = await _orchestrator(
                session, references=_BrokenReferenceStore(session)
            )
            with pytest.raises(TransferExecutionUnavailableError, match="persistence failed"):
                await orchestrator.create_or_reuse_reference(
                    organisation_id=org.id,
                    logical_request_id="req-compensate",
                    provider_id="fake",
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_reference=_source_key(org.id),
                    source_digest="c0de" * 16,
                    size_bytes=1600,
                    mime_type="application/pdf",
                    source_lifecycle=SourceLifecycle.TRANSIENT,
                    region="eu-west-1",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            # The staged copy was compensated: deleted through the store.
            assert len(store.deleted) == 1
            assert len(store.records) == 1
            assert store.records[0].status.value == "deleted"
        async with session_factory() as session:
            assert await _all_rows(session, org.id, "req-compensate") == []
    finally:
        await engine.dispose()


async def test_persistence_failure_with_cleanup_failure_propagates(
    migrated_database: str,
) -> None:
    """Must-fix: even when compensation fails, the original error wins.

    The staged copy remains (bounded by provider expiry and §6.7 reconciliation
    coverage of cleanup failures); the caller sees the persistence failure.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)

        class _BrokenReferenceStore(SQLTransferReferenceStore):
            async def create_or_adopt(
                self, reference: ExternalFileReference
            ) -> ExternalFileReference:
                raise TransferExecutionUnavailableError("durable persistence failed")

        class _BrokenDeleteStore(FakeTransferStore):
            async def delete(self, reference: ExternalFileReference) -> None:
                raise TransferExecutionUnavailableError("provider delete failed")

        async with session_factory() as session:
            store = _BrokenDeleteStore()
            orchestrator, _, _ = await _orchestrator(
                session, store=store, references=_BrokenReferenceStore(session)
            )
            with pytest.raises(TransferExecutionUnavailableError, match="persistence failed"):
                await orchestrator.create_or_reuse_reference(
                    organisation_id=org.id,
                    logical_request_id="req-compensate-fail",
                    provider_id="fake",
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_reference=_source_key(org.id),
                    source_digest="c0de" * 16,
                    size_bytes=1600,
                    mime_type="application/pdf",
                    source_lifecycle=SourceLifecycle.TRANSIENT,
                    region="eu-west-1",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            # Compensation itself failed: the copy is still staged and live.
            assert store.deleted == []
            assert len(store.records) == 1
            assert store.records[0].status.value == "live"
    finally:
        await engine.dispose()


async def test_stale_reference_deletion_uses_the_authoritative_live_row(
    migrated_database: str,
) -> None:
    """Must-fix: a delayed cleanup carrying an old reference cannot orphan the
    recreated provider copy.

    The provider copy was recreated (the durable row was refreshed to the new
    external id); the old reference names the dead copy. Deletion must resolve
    the authoritative live row and delete the CURRENT copy, then mark the row
    deleted — otherwise the new copy is orphaned with no row for §6.7 to find.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)

        class _ExternalIdAwareStore(FakeTransferStore):
            """Deletes the copy the reference names (like real provider
            adapters deleting by file id), instead of by idempotency key."""

            async def delete(self, reference: ExternalFileReference) -> None:
                record = self._records.get(reference.idempotency_key)
                if (
                    record is None
                    or record.external_id != reference.external_id
                    or record.status is ExternalReferenceStatus.DELETED
                ):
                    return
                record.status = ExternalReferenceStatus.DELETED
                record.deleted_at = datetime.now(UTC)
                self.deleted.append(record)

        async with session_factory() as session:
            store = _ExternalIdAwareStore()
            orchestrator, _, _ = await _orchestrator(session, store=store)
            args: dict[str, Any] = {
                "organisation_id": org.id,
                "logical_request_id": "req-orch-stale",
                "provider_id": "fake",
                "mode": TransferMode.PROVIDER_UPLOAD,
                "source_reference": _source_key(org.id),
                "source_digest": "abcd" * 16,
                "size_bytes": 1600,
                "mime_type": "application/pdf",
                "source_lifecycle": SourceLifecycle.TRANSIENT,
                "region": "eu-west-1",
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
            first = await orchestrator.create_or_reuse_reference(**args)
            # The provider-side copy expired; the retry recreates it and the
            # live row is refreshed to the new external id.
            store.expire_due(now=datetime.now(UTC) + timedelta(hours=2))
            recreated = await orchestrator.create_or_reuse_reference(**args)
            assert recreated.external_id != first.external_id
            # A delayed cleanup carrying the OLD reference (external_id of the
            # dead copy) resolves the current live row and deletes the CURRENT
            # copy — never deleting the stale name and orphaning the live one.
            assert await orchestrator.delete_reference(reference=first) is True
            assert len(store.deleted) == 1
            assert store.deleted[0].external_id == recreated.external_id
            rows = await _all_rows(session, org.id, "req-orch-stale")
            assert len(rows) == 1
            assert rows[0].status == "deleted"
            assert rows[0].external_id == recreated.external_id
    finally:
        await engine.dispose()


async def test_delete_reference_with_mismatched_transfer_fails_closed(
    migrated_database: str,
) -> None:
    """Must-fix: deletion validates the resolved row against the caller's
    provider/mode before any provider deletion."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            reference_store = SQLTransferReferenceStore(session)
            reference = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="b00b" * 16,
            )
            await reference_store.create_or_adopt(reference)
            orchestrator, _, store = await _orchestrator(session, references=reference_store)
            wrong_provider = reference.model_copy(update={"provider": "openai"})
            with pytest.raises(TransferExecutionUnavailableError, match="does not match"):
                await orchestrator.delete_reference(reference=wrong_provider)
            assert store.deleted == []  # nothing was deleted
    finally:
        await engine.dispose()


async def test_adopt_touches_the_live_row_after_expired_replacement(
    migrated_database: str,
) -> None:
    """Must-fix: adoption lands on the live row, never on a historical row.

    After an expired row is replaced by a new live row sharing the idempotency
    key, adoption must touch the live row's last-used marker while the terminal
    row stays untouched.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            store = SQLTransferReferenceStore(session)
            past = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="cafe" * 16,
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            await store.create_or_adopt(past)
            # The expired row is replaced by a new live row with the same key.
            future = _staged_reference(
                organisation_id=org.id,
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="cafe" * 16,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await store.create_or_adopt(future)
            rows = await _all_rows(session, org.id, "req-1")
            assert len(rows) == 2
            assert {row.status for row in rows} == {"live", "expired"}
            assert await store.adopt(organisation_id=org.id, idempotency_key=future.idempotency_key)
            rows = await _all_rows(session, org.id, "req-1")
            live = next(row for row in rows if row.status == "live")
            expired = next(row for row in rows if row.status == "expired")
            assert live.last_used_at is not None
            assert expired.last_used_at is None
            # find_live adopts and returns the live row.
            hit = await store.find_live(
                organisation_id=org.id,
                logical_request_id="req-1",
                provider_id="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                source_digest="cafe" * 16,
                region="eu-west-1",
            )
            assert hit is not None and hit.last_used_at is not None
    finally:
        await engine.dispose()


async def test_orchestrator_provider_mismatch_fails_closed(migrated_database: str) -> None:
    """Should-fix: the staging store's provider must match the selected provider."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)

        class _OtherProviderStore(FakeTransferStore):
            provider_id = "other-provider"

        async with session_factory() as session:
            store = _OtherProviderStore()
            orchestrator, _, _ = await _orchestrator(session, store=store)
            with pytest.raises(TransferExecutionUnavailableError, match="does not match"):
                await orchestrator.create_or_reuse_reference(
                    organisation_id=org.id,
                    logical_request_id="req-provider",
                    provider_id="fake",
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_reference=_source_key(org.id),
                    source_digest="c0de" * 16,
                    size_bytes=1600,
                    mime_type="application/pdf",
                    source_lifecycle=SourceLifecycle.TRANSIENT,
                    region="eu-west-1",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            assert store.records == []  # nothing was staged
    finally:
        await engine.dispose()


async def test_reference_table_rejects_inline_transfer_mode(migrated_database: str) -> None:
    """Should-fix: the table stores only the three non-inline transfer modes.

    The table is the durable record of non-inline transfers (Scope §2.3), so
    the database itself must reject ``inline`` — not just the application.
    """
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            org = await _seed_organisation(session)
        async with session_factory() as session:
            row = AIAttachmentReference(
                organisation_id=org.id,
                logical_request_id="req-inline-db",
                provider="fake",
                transfer_mode="inline",
                external_id="fake-inline",
                source_reference=_source_key(org.id),
                source_digest="dead" * 16,
                size_bytes=1600,
                mime_type="application/pdf",
                source_lifecycle="transient",
                region="eu-west-1",
                idempotency_key="key-inline-db",
            )
            session.add(row)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()
