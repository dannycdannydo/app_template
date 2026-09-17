"""Real-database tests for plan P6 document source authority and immutable uploads.

The source authority is the security boundary this work unit adds: a private
storage reference must resolve to a live, tenant-matched application record in
an allowed state before the AI layer or the download endpoint reads its bytes.
These tests run the real migration and the real service against a reachable
PostgreSQL (the same skip pattern as ``test_files_db.py``) with the in-memory
storage fake; they cover unknown/pending/failed/quarantined/deleted/cross-org
documents, the pinned-identity overwrite check, scratch intents (including
expiry) and the staging-promotion immutability guarantee.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast as typing_cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai import scratch as ai_scratch
from app.ai.errors import AIInputValidationError
from app.ai.persistence.models import AIScratchUpload, AIScratchUploadStatus
from app.ai.persistence.service import create_default_settings, expire_scratch_uploads
from app.core.exceptions import ConflictError
from app.modules.files import service as files_service
from app.modules.files.authority import DocumentSourceAuthority
from app.modules.files.models import File, FileStatus
from app.modules.jobs.models import Job
from app.modules.organisations.models import Organisation
from app.storage import FakeObjectStorage, get_storage

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _database_reachable(database_url: str) -> bool:
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
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


async def _create_org(
    session: AsyncSession, name: str, *, ai_enabled: bool = False
) -> Organisation:
    organisation = Organisation(name=name)
    session.add(organisation)
    await session.flush()
    settings_row = await create_default_settings(session, organisation_id=organisation.id)
    settings_row.enabled = ai_enabled
    await session.commit()
    return organisation


def _fake_storage() -> FakeObjectStorage:
    return typing_cast(FakeObjectStorage, get_storage())


async def _seed_ready_document(
    session: AsyncSession,
    organisation: Organisation,
    *,
    content: bytes = b"%PDF-1.4 authorised document",
) -> tuple[File, str]:
    """Create a READY file with a pinned identity whose final object exists."""
    file, signed_url = await files_service.create_upload_intent(
        session,
        organisation_id=organisation.id,
        original_filename="report.pdf",
        content_type="application/pdf",
        size_bytes=len(content),
    )
    await _fake_storage().put(file.object_key, content)
    file, _job_id = await files_service.complete_upload(
        session, organisation_id=organisation.id, file_id=file.id
    )
    await files_service.mark_file_processing(
        session, organisation_id=organisation.id, file_id=file.id
    )
    await files_service.mark_file_ready(session, organisation_id=organisation.id, file_id=file.id)
    return file, signed_url.url


async def test_authority_allows_only_ready_documents_with_pinned_identity(
    migrated_database: str,
) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Authority Ltd")
            authority = DocumentSourceAuthority()
            ready, _url = await _seed_ready_document(session, organisation)

            # A READY document with a matching identity is authorised.
            await authority.authorize(
                session=session,
                organisation_id=organisation.id,
                storage_reference=ready.object_key,
            )

            # A same-size overwrite changes the provider checksum and fails
            # closed, even though the object still exists at the same size.
            original = await get_storage().read_object(ready.object_key)
            replacement = original[:-1] + b"T"  # same length, different bytes
            assert len(replacement) == len(original)
            await _fake_storage().put(ready.object_key, replacement)
            with pytest.raises(AIInputValidationError):
                await authority.authorize(
                    session=session,
                    organisation_id=organisation.id,
                    storage_reference=ready.object_key,
                )
    finally:
        await engine.dispose()


async def test_download_uses_the_same_source_authority(migrated_database: str) -> None:
    """Plan P6 must-fix: download shares the one document-authority decision.

    A ``ready`` file with a matching pinned identity is downloadable, and a
    same-size overwrite after approval makes download fail with 409 through
    the same authority the AI reads use — download never keeps a private copy
    of the lifecycle/identity logic.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Download Authority Ltd")
            ready, _url = await _seed_ready_document(session, organisation)

            download = await files_service.create_download_url(
                session, organisation_id=organisation.id, file_id=ready.id
            )
            assert download.method == "GET"

            original = await get_storage().read_object(ready.object_key)
            replacement = original[:-1] + b"T"
            assert len(replacement) == len(original)
            await _fake_storage().put(ready.object_key, replacement)
            with pytest.raises(ConflictError):
                await files_service.create_download_url(
                    session, organisation_id=organisation.id, file_id=ready.id
                )
    finally:
        await engine.dispose()


async def test_authority_denies_non_ready_and_unknown_documents(
    migrated_database: str,
) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Authority States Ltd")
            authority = DocumentSourceAuthority()
            denied_statuses = (
                FileStatus.PENDING,
                FileStatus.UPLOADED,
                FileStatus.PROCESSING,
                FileStatus.FAILED,
                FileStatus.QUARANTINED,
                FileStatus.DELETED,
            )
            for status in denied_statuses:
                key = f"organisations/{organisation.id}/documents/{uuid.uuid4()}/original"
                await _fake_storage().put(key, b"%PDF-1.4 bytes")
                file = File(
                    organisation_id=organisation.id,
                    storage_provider="fake",
                    storage_bucket="test-bucket",
                    object_key=key,
                    original_filename="report.pdf",
                    content_type="application/pdf",
                    size_bytes=12,
                    status=status,
                    content_identity="deadbeef",
                )
                session.add(file)
                await session.commit()
                with pytest.raises(AIInputValidationError):
                    await authority.authorize(
                        session=session,
                        organisation_id=organisation.id,
                        storage_reference=key,
                    )

            # An unknown key and a cross-organisation key both fail closed.
            with pytest.raises(AIInputValidationError):
                await authority.authorize(
                    session=session,
                    organisation_id=organisation.id,
                    storage_reference=(
                        f"organisations/{organisation.id}/documents/{uuid.uuid4()}/original"
                    ),
                )
            other = await _create_org(session, "Authority Other Ltd")
            with pytest.raises(AIInputValidationError):
                await authority.authorize(
                    session=session,
                    organisation_id=other.id,
                    storage_reference=(
                        f"organisations/{organisation.id}/documents/{uuid.uuid4()}/original"
                    ),
                )
    finally:
        await engine.dispose()


async def test_authority_requires_live_scratch_intent(migrated_database: str) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Scratch Authority Ltd", ai_enabled=True)
            authority = DocumentSourceAuthority()
            content = b"%PDF-1.4 scratch"
            intent = await ai_scratch.create_scratch_intent(
                session,
                organisation_id=organisation.id,
                content_type="application/pdf",
                size_bytes=len(content),
            )
            await _fake_storage().put(intent.object_key, content, content_type="application/pdf")

            # A pending scratch intent is not authorised until completed.
            with pytest.raises(AIInputValidationError):
                await authority.authorize(
                    session=session,
                    organisation_id=organisation.id,
                    storage_reference=intent.object_key,
                )
            await ai_scratch.complete_scratch_intent(
                session, organisation_id=organisation.id, upload_id=intent.upload_id
            )
            await session.commit()
            await authority.authorize(
                session=session,
                organisation_id=organisation.id,
                storage_reference=intent.object_key,
            )

            # An expired intent fails closed even though the row is ``ready``.
            row = await session.scalar(
                select(AIScratchUpload).where(AIScratchUpload.object_key == intent.object_key)
            )
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            with pytest.raises(AIInputValidationError):
                await authority.authorize(
                    session=session,
                    organisation_id=organisation.id,
                    storage_reference=intent.object_key,
                )
    finally:
        await engine.dispose()


async def test_expired_scratch_intents_are_swept_without_retention_policy(
    migrated_database: str,
) -> None:
    """Plan P6: the global scratch ceiling applies even with no per-org policy."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Scratch Expiry Ltd", ai_enabled=True)
            content = b"%PDF-1.4 scratch"
            intent = await ai_scratch.create_scratch_intent(
                session,
                organisation_id=organisation.id,
                content_type="application/pdf",
                size_bytes=len(content),
            )
            await _fake_storage().put(intent.object_key, content, content_type="application/pdf")
            await ai_scratch.complete_scratch_intent(
                session, organisation_id=organisation.id, upload_id=intent.upload_id
            )
            await session.commit()
            row = await session.scalar(
                select(AIScratchUpload).where(AIScratchUpload.object_key == intent.object_key)
            )
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(seconds=5)
            await session.commit()

            expired = await expire_scratch_uploads(session, _fake_storage())
        # The sweep is global; earlier tests may have left other expired
        # intents in the shared migrated database, so assert at least ours.
        assert expired >= 1
        async with session_factory() as session:
            row = await session.scalar(
                select(AIScratchUpload).where(AIScratchUpload.object_key == intent.object_key)
            )
            assert row is not None
            assert row.status == AIScratchUploadStatus.EXPIRED
        assert await get_storage().head_object(intent.object_key) is None
    finally:
        await engine.dispose()


async def test_staging_replay_cannot_mutate_promoted_bytes(migrated_database: str) -> None:
    """The signed PUT targets staging; replaying it never changes final bytes."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Immutable Uploads Ltd")
            content = b"%PDF-1.4 original bytes"
            file, _signed_url = await files_service.create_upload_intent(
                session,
                organisation_id=organisation.id,
                original_filename="report.pdf",
                content_type="application/pdf",
                size_bytes=len(content),
            )
            staging_key = file.object_key
            await _fake_storage().put(staging_key, content)
            file, _job_id = await files_service.complete_upload(
                session, organisation_id=organisation.id, file_id=file.id
            )
            final_key = file.object_key
            pinned = file.content_identity
            assert pinned is not None

            # Replay the (now deleted) staging capability with different bytes
            # of the same size; the final object must be untouched.
            await _fake_storage().put(staging_key, b"%PDF-1.4 tampered bytes")
            final_info = await get_storage().head_object(final_key)
            assert final_info is not None
            assert final_info.checksum == pinned
            assert final_info.checksum != hashlib.sha256(b"%PDF-1.4 tampered bytes").hexdigest()
    finally:
        await engine.dispose()


async def test_completion_failure_rolls_back_to_pending(migrated_database: str) -> None:
    """Plan P6 atomic completion: a failed schedule leaves no uploaded file."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Atomic Completion Ltd")
            content = b"%PDF-1.4 atomic"
            file, _signed_url = await files_service.create_upload_intent(
                session,
                organisation_id=organisation.id,
                original_filename="report.pdf",
                content_type="application/pdf",
                size_bytes=len(content),
            )
            file_id = file.id
            await _fake_storage().put(file.object_key, content)

            import app.modules.jobs.service as jobs_service

            original = jobs_service.schedule_job

            async def _boom(*args: object, **kwargs: object) -> object:
                raise RuntimeError("scheduling unavailable")

            jobs_service.schedule_job = _boom  # type: ignore[assignment]
            try:
                with pytest.raises(RuntimeError):
                    await files_service.complete_upload(
                        session, organisation_id=organisation.id, file_id=file_id
                    )
            finally:
                jobs_service.schedule_job = original  # type: ignore[assignment]
            await session.rollback()

        async with session_factory() as session:
            persisted = await session.get(File, file_id)
            assert persisted is not None
            assert persisted.status == FileStatus.PENDING
            job_count = await session.scalar(
                select(func.count()).select_from(Job).where(Job.input_reference == str(file_id))
            )
            assert job_count == 0
    finally:
        await engine.dispose()
