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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.modules.files import service
from app.modules.files.models import File, FileStatus
from app.modules.organisations.models import Organisation
from app.storage import FakeObjectStorage, get_storage

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

            # A signed download URL is issued for a verified file.
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


async def test_complete_rejects_already_completed_file(migrated_database: str) -> None:
    """A file that is no longer pending cannot be completed twice (409)."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            organisation = await _create_org(session, "Double Complete Ltd")
            content = b"double complete bytes"
            uploaded = await _upload_and_complete(session, organisation.id, content=content)

            with pytest.raises(ConflictError):
                await service.complete_upload(
                    session, organisation_id=organisation.id, file_id=uploaded.id
                )
    finally:
        await engine.dispose()
