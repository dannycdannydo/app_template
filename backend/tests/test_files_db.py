"""Real-database integration tests for the files module (Scope §6.3).

The fakes in ``test_files.py`` prove the request-flow contract but never
execute SQL, so org-scoping and the status/deleted filters could silently
regress at the query level. These tests run the real migration and the real
service against a reachable PostgreSQL (same skip pattern as
``test_records_db.py``: migrated to head up front, reverted to base
afterwards). Object storage stays on the in-memory fake (pinned by
``STORAGE_PROVIDER=fake`` in ``conftest.py``), so no MinIO is needed — the
verification seam between the service and the adapter is already proven by the
storage contract tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast as typing_cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.modules.files import service
from app.modules.files.models import File, FileStatus
from app.modules.organisations.models import Organisation
from app.storage import FakeObjectStorage, get_storage
from app.storage.types import ObjectInfo

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
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards.

    Requires a reachable PostgreSQL as configured by ``DATABASE_URL``; skipped
    otherwise. Reverting to base keeps the test database clean for the other
    migration smoke tests, whichever runs first.
    """
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


async def _create_org(session: AsyncSession, name: str) -> Organisation:
    organisation = Organisation(name=name)
    session.add(organisation)
    await session.commit()
    return organisation


def _fake_storage() -> FakeObjectStorage:
    """Return the process-wide fake adapter as its concrete type (has ``put``)."""
    return typing_cast(FakeObjectStorage, get_storage())


async def _upload_and_complete(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    original_filename: str = "report.pdf",
    content_type: str = "application/pdf",
    content: bytes = b"real database bytes",
    checksum: str | None = None,
) -> File:
    """Run intent -> direct PUT (fake storage) -> complete in one round trip."""
    file, signed_url = await service.create_upload_intent(
        session,
        organisation_id=organisation_id,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=len(content),
        actor_user_id=None,
    )
    assert signed_url.method == "PUT"
    await _fake_storage().put(file.object_key, content)
    completed, _job_id = await service.complete_upload(
        session,
        organisation_id=organisation_id,
        file_id=file.id,
        checksum=checksum,
    )
    return completed


async def test_files_crud_round_trip_within_org(migrated_database: str) -> None:
    """Acceptance §5.4: intent -> PUT -> complete -> list -> detail -> delete."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Files CRUD Ltd")
            content = b"the bytes that were uploaded"
            checksum = hashlib.sha256(content).hexdigest()

            uploaded = await _upload_and_complete(
                session,
                organisation.id,
                content=content,
                checksum=checksum,
            )
            assert uploaded.status == FileStatus.UPLOADED
            assert uploaded.checksum == checksum
            assert uploaded.object_key == (
                f"organisations/{organisation.id}/documents/{uploaded.id}/original"
            )

            # List finds exactly one file; the detail round-trips.
            files, total = await service.list_files(
                session,
                organisation_id=organisation.id,
                page=1,
                page_size=50,
            )
            assert total == 1
            assert [file.id for file in files] == [uploaded.id]

            fetched = await service.get_file(
                session,
                organisation_id=organisation.id,
                file_id=uploaded.id,
            )
            assert fetched.status == FileStatus.UPLOADED
            assert fetched.original_filename == "report.pdf"
            # Plan P6: completion pins the immutable content identity.
            assert fetched.content_identity is not None
            # Promotion moved the served key from the staging key to the final,
            # non-presigned key.
            assert fetched.object_key == (
                f"organisations/{organisation.id}/documents/{fetched.id}/original"
            )
            assert "staging" not in fetched.object_key

            # Download is gated until the file reaches ``ready`` (the worker's
            # post-verification, post-scan state).
            with pytest.raises(ConflictError):
                await service.create_download_url(
                    session,
                    organisation_id=organisation.id,
                    file_id=uploaded.id,
                )
            await service.mark_file_processing(
                session, organisation_id=organisation.id, file_id=uploaded.id
            )
            await service.mark_file_ready(
                session, organisation_id=organisation.id, file_id=uploaded.id
            )

            # A signed download URL is issued for a verified, ready file.
            download = await service.create_download_url(
                session,
                organisation_id=organisation.id,
                file_id=uploaded.id,
            )
            assert download.method == "GET"
            assert uploaded.object_key in download.url

            # Soft delete: the row stays (status deleted, deleted_at set) and
            # the object is gone from storage.
            await service.delete_file(
                session,
                organisation_id=organisation.id,
                file_id=uploaded.id,
            )
            await session.refresh(uploaded)
            assert uploaded.status == FileStatus.DELETED
            assert uploaded.deleted_at is not None
            assert await get_storage().head_object(uploaded.object_key) is None

            # Deleted files are excluded from list and detail by default.
            files_after, total_after = await service.list_files(
                session,
                organisation_id=organisation.id,
                page=1,
                page_size=50,
            )
            assert files_after == []
            assert total_after == 0
            with pytest.raises(NotFoundError):
                await service.get_file(
                    session,
                    organisation_id=organisation.id,
                    file_id=uploaded.id,
                )
    finally:
        await engine.dispose()


async def test_status_filter_and_deleted_exclusion(migrated_database: str) -> None:
    """The list status filter and deleted-file exclusion work at the SQL level."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Status Filter Ltd")
            content = b"status filter bytes"
            ready = await _upload_and_complete(session, organisation.id, content=content)
            pending, _ = await service.create_upload_intent(
                session,
                organisation_id=organisation.id,
                original_filename="pending.txt",
                content_type="text/plain",
                size_bytes=3,
            )

            ready_only, ready_total = await service.list_files(
                session,
                organisation_id=organisation.id,
                page=1,
                page_size=50,
                status=FileStatus.UPLOADED,
            )
            assert ready_total == 1
            assert [file.id for file in ready_only] == [ready.id]

            pending_only, pending_total = await service.list_files(
                session,
                organisation_id=organisation.id,
                page=1,
                page_size=50,
                status=FileStatus.PENDING,
            )
            assert pending_total == 1
            assert [file.id for file in pending_only] == [pending.id]
    finally:
        await engine.dispose()


async def test_cross_org_access_is_not_found(migrated_database: str) -> None:
    """Acceptance §5.4: another org's file resolves to 404 on every operation."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org_a = await _create_org(session, "Org A Files Ltd")
            org_b = await _create_org(session, "Org B Files Ltd")
            content = b"cross org bytes"
            file_a = await _upload_and_complete(session, org_a.id, content=content)

            with pytest.raises(NotFoundError):
                await service.get_file(session, organisation_id=org_b.id, file_id=file_a.id)
            with pytest.raises(NotFoundError):
                await service.complete_upload(session, organisation_id=org_b.id, file_id=file_a.id)
            with pytest.raises(NotFoundError):
                await service.create_download_url(
                    session, organisation_id=org_b.id, file_id=file_a.id
                )
            with pytest.raises(NotFoundError):
                await service.delete_file(session, organisation_id=org_b.id, file_id=file_a.id)

            # The file is untouched and the other org's list stays empty.
            pristine = await service.get_file(session, organisation_id=org_a.id, file_id=file_a.id)
            assert pristine.status == FileStatus.UPLOADED
            files_b, total_b = await service.list_files(
                session, organisation_id=org_b.id, page=1, page_size=50
            )
            assert files_b == []
            assert total_b == 0
    finally:
        await engine.dispose()


async def test_complete_failure_paths_persist_failed_status(migrated_database: str) -> None:
    """Acceptance §5.5: verification failure persists ``failed`` and raises 422."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Failure Paths Ltd")

            # Missing object: the browser never PUT anything.
            missing, _ = await service.create_upload_intent(
                session,
                organisation_id=organisation.id,
                original_filename="missing.pdf",
                content_type="application/pdf",
                size_bytes=4,
            )
            with pytest.raises(ValidationError):
                await service.complete_upload(
                    session, organisation_id=organisation.id, file_id=missing.id
                )
            await session.refresh(missing)
            assert missing.status == FileStatus.FAILED

            # Size mismatch: store a different size than the declaration.
            mismatched, _ = await service.create_upload_intent(
                session,
                organisation_id=organisation.id,
                original_filename="mismatch.pdf",
                content_type="application/pdf",
                size_bytes=10,
            )
            # The fake enforces the declared size at put time; re-declare the
            # key with the smaller size to simulate a browser storing the wrong
            # object, then complete — the head result (6 bytes) must not match.
            await _fake_storage().create_upload_url(
                file_id=mismatched.id,
                object_key=mismatched.object_key,
                content_type="application/pdf",
                size_bytes=6,
            )
            await _fake_storage().put(mismatched.object_key, b"six...")
            with pytest.raises(ValidationError):
                await service.complete_upload(
                    session, organisation_id=organisation.id, file_id=mismatched.id
                )
            await session.refresh(mismatched)
            assert mismatched.status == FileStatus.FAILED
    finally:
        await engine.dispose()


async def test_promotion_requires_stable_identity(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan P6 should-fix: a promoted object without a stable identity fails.

    A provider that exposes no checksum for the final object cannot back a
    pinned content identity, so completion must fail the file rather than
    create a ``ready``-capable row the authority would always reject.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = _fake_storage()
    original_head = storage.head_object
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Promotion Identity Ltd")
            content = b"promoted identity bytes"
            file, _url = await service.create_upload_intent(
                session,
                organisation_id=organisation.id,
                original_filename="report.pdf",
                content_type="application/pdf",
                size_bytes=len(content),
            )
            await storage.put(file.object_key, content)
            final_key = service.object_key_for(organisation.id, file.id)

            async def _head_without_final_checksum(object_key: str) -> ObjectInfo | None:
                info = await original_head(object_key)
                if info is not None and object_key == final_key:
                    return ObjectInfo(
                        object_key=info.object_key,
                        size_bytes=info.size_bytes,
                        content_type=info.content_type,
                        checksum=None,
                    )
                return info

            monkeypatch.setattr(storage, "head_object", _head_without_final_checksum)
            with pytest.raises(ValidationError):
                await service.complete_upload(
                    session, organisation_id=organisation.id, file_id=file.id
                )
            await session.refresh(file)
            assert file.status == FileStatus.FAILED
    finally:
        await engine.dispose()


async def test_promotion_detects_same_size_overwrite_during_copy(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan P6 should-fix: a staging overwrite between head and copy fails.

    The completed object's pinned identity must equal the bytes that passed the
    pre-copy verification; a same-size overwrite of the staging key landing in
    that window must fail completion instead of promoting unverified bytes.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = _fake_storage()
    original_copy = storage.copy_object
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Promotion Race Ltd")
            content = b"original staging bytes"
            file, _url = await service.create_upload_intent(
                session,
                organisation_id=organisation.id,
                original_filename="report.pdf",
                content_type="application/pdf",
                size_bytes=len(content),
            )
            staging_key = file.object_key
            await storage.put(staging_key, content)

            async def _copy_after_tamper(*, source_key: str, destination_key: str) -> None:
                # A replay of the still-live staging PUT lands between the
                # pre-copy head and the provider copy.
                await storage.put(source_key, b"tampered staging bytes")
                await original_copy(source_key=source_key, destination_key=destination_key)

            monkeypatch.setattr(storage, "copy_object", _copy_after_tamper)
            with pytest.raises(ValidationError):
                await service.complete_upload(
                    session,
                    organisation_id=organisation.id,
                    file_id=file.id,
                    checksum=hashlib.sha256(content).hexdigest(),
                )
            await session.refresh(file)
            assert file.status == FileStatus.FAILED
    finally:
        await engine.dispose()


async def test_complete_replay_is_idempotent(migrated_database: str) -> None:
    """A replayed completion returns the same job and schedules no duplicate.

    Plan P6: completion is idempotent under retry/parallel calls — the first
    call leaves one ``uploaded`` file with exactly one processing job, and the
    replay returns that same job id.
    """
    from app.modules.jobs.models import Job

    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Double Complete Ltd")
            content = b"double complete bytes"
            file, _signed_url = await service.create_upload_intent(
                session,
                organisation_id=organisation.id,
                original_filename="report.pdf",
                content_type="application/pdf",
                size_bytes=len(content),
            )
            await _fake_storage().put(file.object_key, content)
            first, first_job_id = await service.complete_upload(
                session, organisation_id=organisation.id, file_id=file.id
            )
            assert first.status == FileStatus.UPLOADED
            assert first_job_id is not None

            replay, replay_job_id = await service.complete_upload(
                session, organisation_id=organisation.id, file_id=file.id
            )
            assert replay.id == first.id
            assert replay.status == FileStatus.UPLOADED
            assert replay_job_id == first_job_id

            # A retry after the worker advanced the row to ``processing`` (and
            # then ``ready``) must still resolve to that same job rather than
            # erroring or scheduling a duplicate (plan P6: replay is idempotent
            # across every valid post-completion lifecycle state).
            await service.mark_file_processing(
                session, organisation_id=organisation.id, file_id=file.id
            )
            processing_replay, processing_job_id = await service.complete_upload(
                session, organisation_id=organisation.id, file_id=file.id
            )
            assert processing_replay.status == FileStatus.PROCESSING
            assert processing_job_id == first_job_id

            await service.mark_file_ready(session, organisation_id=organisation.id, file_id=file.id)
            ready_replay, ready_job_id = await service.complete_upload(
                session, organisation_id=organisation.id, file_id=file.id
            )
            assert ready_replay.status == FileStatus.READY
            assert ready_job_id == first_job_id

            job_count = await session.scalar(
                select(func.count()).select_from(Job).where(Job.input_reference == str(file.id))
            )
            assert job_count == 1
    finally:
        await engine.dispose()


async def test_concurrent_completion_produces_one_job(migrated_database: str) -> None:
    """Plan P6: two parallel completions leave one uploaded file and one job.

    The completion path locks the file row ``FOR UPDATE``, so the second
    transaction blocks, observes the first's ``uploaded`` status and returns the
    same processing job instead of scheduling a duplicate.
    """
    from app.modules.jobs.models import Job

    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Concurrent Complete Ltd")
            content = b"concurrent completion bytes"
            file, _signed_url = await service.create_upload_intent(
                session,
                organisation_id=organisation.id,
                original_filename="report.pdf",
                content_type="application/pdf",
                size_bytes=len(content),
            )
            file_id = file.id
            await _fake_storage().put(file.object_key, content)

        async def _complete() -> tuple[File, uuid.UUID | None]:
            async with session_factory() as session:
                return await service.complete_upload(
                    session, organisation_id=organisation.id, file_id=file_id
                )

        first, second = await asyncio.gather(_complete(), _complete())
        assert first[0].status == FileStatus.UPLOADED
        assert second[0].status == FileStatus.UPLOADED
        assert first[1] is not None
        assert first[1] == second[1]

        async with session_factory() as session:
            job_count = await session.scalar(
                select(func.count()).select_from(Job).where(Job.input_reference == str(file_id))
            )
        assert job_count == 1
    finally:
        await engine.dispose()
