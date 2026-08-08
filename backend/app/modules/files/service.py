"""File metadata services (Scope §6.3/§6.5, blueprint §11, §12, §17, §18, §30).

The service owns the direct-upload flow and the file lifecycle. Every function
is one atomic operation that commits itself (BP §11); every query is org-scoped
through ``queries.org_scoped_files_statement``, so a file that exists but
belongs to another organisation surfaces as a 404. Domain failures are raised
as domain exceptions for the central handlers.

Intent-time validation (BP §30 file security) happens here, not in the router,
because it needs settings: the declared size is checked against
``STORAGE_MAX_UPLOAD_SIZE`` and the declared content type and filename
extension against ``STORAGE_ALLOWED_CONTENT_TYPES`` before any signed URL is
issued. Storage SDK calls go through the :class:`ObjectStorage` interface only
(ADR-0006) — the provider adapter is selected from settings by
``app.storage.factory.get_storage``.

Scope §6.5 adds the worker-side half of the lifecycle: after the browser's PUT
is verified at completion, :func:`complete_upload` writes the durable job row
and enqueues the ``process_file`` task (BP §18 record-then-enqueue), and the
``mark_file_*`` helpers are the transitions the worker calls — each idempotent,
so a retried message re-running the job converges instead of erroring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from dramatiq import Actor
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, ErrorDetail, NotFoundError, ValidationError
from app.db.conventions import uuid7
from app.modules.audit.service import (
    ACTION_FILE_DELETED,
    ACTION_FILE_PROCESSING,
    ACTION_FILE_READY,
    ACTION_FILE_UPLOAD_FAILED,
    ACTION_FILE_UPLOAD_STARTED,
    ACTION_FILE_UPLOADED,
    record_event,
)
from app.modules.files.models import File, FileStatus
from app.modules.files.queries import (
    org_files_count_statement,
    org_scoped_files_statement,
)
from app.modules.jobs import service as jobs_service
from app.storage import SignedUrl, get_storage

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# The server-generated object key format (Scope §6.3): the file id is embedded
# so keys are unique and traceable; the client never supplies a path.
OBJECT_KEY_TEMPLATE = "organisations/{organisation_id}/documents/{file_id}/original"

# Allowed filename extensions per allowed content type (BP §30: MIME and
# extension validation). Keyed by the same MIME types as
# ``STORAGE_ALLOWED_CONTENT_TYPES``; the extension check only applies to
# content types the template knows, so adding a new allowed type to settings
# without adding its mapping here rejects every filename for it (fail closed).
_EXTENSIONS_BY_CONTENT_TYPE: dict[str, frozenset[str]] = {
    "application/pdf": frozenset({"pdf"}),
    "application/json": frozenset({"json"}),
    "text/plain": frozenset({"txt"}),
    "text/csv": frozenset({"csv"}),
    "image/png": frozenset({"png"}),
    "image/jpeg": frozenset({"jpg", "jpeg"}),
}

# The file exists in storage once the browser has PUT it; download is only
# offered once completion has verified the object.
_DOWNLOADABLE_STATUSES = frozenset({FileStatus.UPLOADED, FileStatus.PROCESSING, FileStatus.READY})


def object_key_for(organisation_id: uuid.UUID, file_id: uuid.UUID) -> str:
    """Return the server-generated object key for one file (Scope §6.3)."""
    return OBJECT_KEY_TEMPLATE.format(
        organisation_id=organisation_id,
        file_id=file_id,
    )


def _not_found() -> NotFoundError:
    return NotFoundError(
        code="file_not_found",
        message="The file could not be found.",
    )


def _validate_declared_upload(
    *,
    original_filename: str,
    content_type: str,
    size_bytes: int,
) -> None:
    """Reject oversized or disallowed uploads before any URL is issued.

    All three failures are 422 validation errors (acceptance §5.5): the
    declared contract — size, MIME type, extension — is checked against the
    configured limits before the pending record is created, so a bad
    declaration never touches storage.
    """
    settings = get_settings()
    if size_bytes > settings.storage_max_upload_size:
        raise ValidationError(
            code="file_too_large",
            message=(
                f"The declared size exceeds the maximum upload size of "
                f"{settings.storage_max_upload_size} bytes."
            ),
            details=[
                ErrorDetail(
                    field="size_bytes",
                    message="Size exceeds the configured maximum.",
                )
            ],
        )
    if content_type not in settings.storage_allowed_content_types:
        raise ValidationError(
            code="unsupported_content_type",
            message=f"The content type {content_type!r} is not allowed.",
            details=[
                ErrorDetail(
                    field="content_type",
                    message="Content type is not in the allowed list.",
                )
            ],
        )
    extensions = _EXTENSIONS_BY_CONTENT_TYPE.get(content_type)
    if extensions is None:
        raise ValidationError(
            code="unsupported_content_type",
            message=f"The content type {content_type!r} is not allowed.",
            details=[
                ErrorDetail(
                    field="content_type",
                    message="Content type has no configured extension mapping.",
                )
            ],
        )
    suffix = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else ""
    if not suffix or suffix not in extensions:
        allowed = ", ".join(sorted(extensions))
        raise ValidationError(
            code="unsupported_file_extension",
            message=(
                f"The filename extension must be one of: {allowed}, "
                f"matching the declared content type."
            ),
            details=[
                ErrorDetail(
                    field="original_filename",
                    message="Extension does not match content type.",
                )
            ],
        )


async def create_upload_intent(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    original_filename: str,
    content_type: str,
    size_bytes: int,
    actor_user_id: uuid.UUID | None = None,
) -> tuple[File, SignedUrl]:
    """Start the direct-upload flow: validate, create the pending record, sign.

    One transaction (BP §11): the pending file record is created with a
    server-generated object key and the provider plane captured from settings,
    the storage adapter mints the signed PUT URL (the S3 adapter lazily ensures
    the bucket on first use), the ``file.upload_started`` audit row is written,
    and everything commits together. The browser then PUTs the bytes directly
    to the signed URL (Scope §6.3 flow).
    """
    _validate_declared_upload(
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    settings = get_settings()
    file_id = uuid7()
    file = File(
        id=file_id,
        organisation_id=organisation_id,
        storage_provider=settings.storage_provider,
        storage_bucket=settings.storage_bucket,
        object_key=object_key_for(organisation_id, file_id),
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        created_by_user_id=actor_user_id,
        status=FileStatus.PENDING,
    )
    session.add(file)
    await session.flush()
    signed_url = await get_storage().create_upload_url(
        file_id=file_id,
        object_key=file.object_key,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_FILE_UPLOAD_STARTED,
        resource_type="file",
        resource_id=str(file_id),
        metadata={
            "object_key": file.object_key,
            "original_filename": original_filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
        },
    )
    await session.commit()
    await session.refresh(file)
    return file, signed_url


async def complete_upload(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
    checksum: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    process_task: Actor[Any, Any] | None = None,
) -> tuple[File, uuid.UUID | None]:
    """Verify the stored object, mark the file ``uploaded`` and enqueue the job.

    The browser's direct PUT is verified, never trusted (BP §17 security): the
    object must exist, its size must match the declared ``size_bytes``, and
    when the client supplied a checksum it must compare equal to the provider's
    (the checksum is opaque; equality only). Verification failure fails the
    file and raises a 422 so the client knows the upload was rejected.

    Once verified, the file is marked ``uploaded`` and the durable processing
    job is created and enqueued (BP §18 record-then-enqueue, Scope §6.5): the
    job row is written with ``job_type="file.processing"`` and
    ``input_reference`` set to the file id, then the ``process_file`` task is
    sent with that job id. The returned job id is what the client polls via
    ``GET /api/v1/jobs/{job_id}`` (the response schema carries it as
    ``processing_job_id``).

    ``process_task`` is the actor to enqueue; it defaults to the module-level
    ``process_file`` actor. Tests pass a copy re-declared on their own broker
    (the same seam ``jobs_service.create_and_enqueue`` exposes for its task).
    """
    file = await get_file(session, organisation_id=organisation_id, file_id=file_id)
    if file.status != FileStatus.PENDING:
        raise ConflictError(
            code="file_not_pending",
            message="Only a pending file can be completed.",
        )
    object_info = await get_storage().head_object(file.object_key)
    verified = object_info is not None and object_info.size_bytes == file.size_bytes
    if verified and checksum is not None:
        verified = object_info is not None and object_info.checksum == checksum
    if not verified:
        file.status = FileStatus.FAILED
        await record_event(
            session,
            organisation_id=organisation_id,
            actor_user_id=actor_user_id,
            action=ACTION_FILE_UPLOAD_FAILED,
            resource_type="file",
            resource_id=str(file.id),
            metadata={
                "object_key": file.object_key,
                "expected_size_bytes": file.size_bytes,
                "actual_size_bytes": object_info.size_bytes if object_info else None,
                "reason": (
                    "object_missing"
                    if object_info is None
                    else "size_mismatch"
                    if object_info.size_bytes != file.size_bytes
                    else "checksum_mismatch"
                ),
            },
        )
        await session.commit()
        raise ValidationError(
            code="upload_verification_failed",
            message="The uploaded object could not be verified; the file has been marked failed.",
        )
    file.status = FileStatus.UPLOADED
    if object_info is not None and object_info.checksum:
        file.checksum = object_info.checksum
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_FILE_UPLOADED,
        resource_type="file",
        resource_id=str(file.id),
        metadata={
            "object_key": file.object_key,
            "size_bytes": file.size_bytes,
            "checksum": file.checksum,
        },
    )
    await session.commit()
    await session.refresh(file)
    # Imported lazily: the task module imports this service, so a module-level
    # import would be circular. By the time the completion flow runs the module
    # is cached, so the import is a dict lookup. The task module is the single
    # source of truth for the actor and its ``job_type`` identity.
    from app.modules.files import tasks as files_tasks

    task = process_task or files_tasks.process_file_actor
    job = await jobs_service.create_and_enqueue(
        session,
        organisation_id=organisation_id,
        job_type=files_tasks.JOB_TYPE_FILE_PROCESSING,
        input_reference=str(file.id),
        actor_user_id=actor_user_id,
        task=task,
    )
    return file, job.id


async def list_files(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    page: int,
    page_size: int,
    status: FileStatus | None = None,
) -> tuple[list[File], int]:
    """Return one page of the caller's organisation's files plus the total.

    Newest first, ties broken by id so paging is stable; deleted files are
    excluded by default. ``page``/``page_size`` are validated by the router's
    query parameters; the service still clamps defensively.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total = await session.scalar(org_files_count_statement(organisation_id, status=status))
    rows = await session.scalars(
        org_scoped_files_statement(organisation_id, status=status)
        .order_by(File.created_at.desc(), File.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.all()), total or 0


async def get_file(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
) -> File:
    """Return one file; a file outside the organisation is a 404.

    The org-scoped (and not-deleted) filter is the isolation boundary: a file
    id that exists in another organisation — or was soft-deleted — simply does
    not match, so cross-organisation and deleted-file reads are
    indistinguishable from missing rows (acceptance §5.4).
    """
    file = await session.scalar(
        org_scoped_files_statement(organisation_id).where(File.id == file_id)
    )
    if file is None:
        raise _not_found()
    return file


async def create_download_url(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
) -> SignedUrl:
    """Return a short-lived signed GET URL for one stored object.

    Only files whose object has been verified are downloadable; a pending
    (never uploaded), failed or quarantined file is a 409, never a signed URL.
    """
    file = await get_file(session, organisation_id=organisation_id, file_id=file_id)
    if file.status not in _DOWNLOADABLE_STATUSES:
        raise ConflictError(
            code="file_not_downloadable",
            message="The file is not ready to download.",
        )
    return await get_storage().create_download_url(object_key=file.object_key)


async def delete_file(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Soft-delete a file: remove the object, mark the row deleted, audit.

    One transaction (BP §11): the stored object is removed from the provider
    (idempotent), the row is soft-deleted via ``deleted_at`` + ``status``
    (never physically removed — the metadata record and its audit trail stay),
    and the ``document.deleted`` audit event (blueprint §29 example) is written
    in the same commit. A file outside the organisation is a 404.
    """
    file = await get_file(session, organisation_id=organisation_id, file_id=file_id)
    await get_storage().delete_object(file.object_key)
    file.deleted_at = datetime.now(UTC)
    file.status = FileStatus.DELETED
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_FILE_DELETED,
        resource_type="file",
        resource_id=str(file.id),
        metadata={
            "object_key": file.object_key,
            "original_filename": file.original_filename,
        },
    )
    await session.commit()


async def mark_file_processing(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
) -> File:
    """Transition a file ``uploaded`` -> ``processing`` (worker-side, §6.5).

    Called by the ``process_file`` task. Idempotent across retries: a file
    already ``processing`` (or already ``ready``, when a retried message
    re-runs after the file finished) is returned untouched, so the task can be
    safely re-run on a re-delivered message. Any other state is a 409 — a
    pending, failed or deleted file never enters processing.
    """
    file = await get_file(session, organisation_id=organisation_id, file_id=file_id)
    if file.status in (FileStatus.PROCESSING, FileStatus.READY):
        return file
    if file.status != FileStatus.UPLOADED:
        raise ConflictError(
            code="file_not_processing",
            message="Only an uploaded file can enter processing.",
        )
    file.status = FileStatus.PROCESSING
    await record_event(
        session,
        organisation_id=organisation_id,
        action=ACTION_FILE_PROCESSING,
        resource_type="file",
        resource_id=str(file.id),
        metadata={
            "object_key": file.object_key,
            "original_filename": file.original_filename,
        },
    )
    await session.commit()
    await session.refresh(file)
    return file


async def mark_file_ready(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
) -> File:
    """Transition a file ``processing`` -> ``ready`` (worker-side, §6.5).

    Called by the ``process_file`` task once the stored object is verified. A
    file that is already ``ready`` is returned untouched (idempotent retry); a
    file not in ``processing`` is a 409, so a ready file is never moved again.
    """
    file = await get_file(session, organisation_id=organisation_id, file_id=file_id)
    if file.status == FileStatus.READY:
        return file
    if file.status != FileStatus.PROCESSING:
        raise ConflictError(
            code="file_not_ready",
            message="Only a processing file can become ready.",
        )
    file.status = FileStatus.READY
    await record_event(
        session,
        organisation_id=organisation_id,
        action=ACTION_FILE_READY,
        resource_type="file",
        resource_id=str(file.id),
        metadata={
            "object_key": file.object_key,
            "original_filename": file.original_filename,
        },
    )
    await session.commit()
    await session.refresh(file)
    return file


async def mark_file_failed(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
    reason: str,
) -> File:
    """Mark a file ``failed`` after a worker-side verification failure (§6.5).

    Called by the ``process_file`` task when the stored object cannot be
    verified while processing (missing, or a size that drifted from the
    declaration). Idempotent: an already-``failed`` or ``deleted`` file is
    returned untouched, so a retried message cannot double-audit. The audit
    row reuses ``file.upload_failed`` with the reason in the metadata, exactly
    like the completion-time failure path.
    """
    file = await get_file(session, organisation_id=organisation_id, file_id=file_id)
    if file.status in (FileStatus.FAILED, FileStatus.DELETED):
        return file
    file.status = FileStatus.FAILED
    await record_event(
        session,
        organisation_id=organisation_id,
        action=ACTION_FILE_UPLOAD_FAILED,
        resource_type="file",
        resource_id=str(file.id),
        metadata={
            "object_key": file.object_key,
            "reason": reason,
        },
    )
    await session.commit()
    await session.refresh(file)
    return file
