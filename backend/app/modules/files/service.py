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
is verified at completion, :func:`complete_upload` durably schedules the
processing job (plan P3: job row + outbox dispatch event in one transaction;
the coordinator publishes the reference-only message), and the
``mark_file_*`` helpers are the transitions the worker calls — each idempotent,
so a retried message re-running the job converges instead of erroring.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.errors import AIInputValidationError
from app.core.config import get_settings
from app.core.exceptions import ConflictError, ErrorDetail, NotFoundError, ValidationError
from app.db.conventions import uuid7
from app.modules.audit.service import (
    ACTION_FILE_DELETED,
    ACTION_FILE_PROCESSING,
    ACTION_FILE_QUARANTINED,
    ACTION_FILE_READY,
    ACTION_FILE_UPLOAD_FAILED,
    ACTION_FILE_UPLOAD_STARTED,
    ACTION_FILE_UPLOADED,
    record_event,
)
from app.modules.files.authority import DocumentSourceAuthority
from app.modules.files.models import File, FileStatus
from app.modules.files.queries import (
    org_files_count_statement,
    org_scoped_files_statement,
)
from app.modules.jobs import service as jobs_service
from app.scanning import ScanVerdict, get_scanner
from app.storage import SignedUrl, get_storage

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# The server-generated final object key format (Scope §6.3): the file id is
# embedded so keys are unique and traceable; the client never supplies a path.
OBJECT_KEY_TEMPLATE = "organisations/{organisation_id}/documents/{file_id}/original"

# The unique staging key the browser's signed PUT targets (plan P6 immutable
# uploads). The final key above is never signed for a PUT: the API promotes the
# verified staging object onto it with a server-side copy, so replaying an old
# upload capability can only recreate a staging object nothing references and
# can never mutate an approved file's bytes.
STAGING_KEY_TEMPLATE = "organisations/{organisation_id}/documents/{file_id}/staging/{token}"

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

# Downloads require the scan/verification verdict: only a ``ready`` file (the
# state the worker establishes after verification and the scanning gate) is
# downloadable. The lifecycle/identity decision is owned by the one source
# authority (``DocumentSourceAuthority``) so download, inline AI, streamed AI
# and job retries can never drift; this service only translates its denial
# into the download-appropriate 409.
_DOCUMENT_AUTHORITY = DocumentSourceAuthority()

# Valid post-completion lifecycle states for a replayed completion: the first
# call moved the row out of ``pending`` and scheduled exactly one processing
# job, so any of these resolves to that existing job rather than erroring or
# scheduling a duplicate. Failure/terminal states are deliberately excluded.
_POST_COMPLETION_STATUSES = frozenset(
    {FileStatus.UPLOADED, FileStatus.PROCESSING, FileStatus.READY}
)


def object_key_for(organisation_id: uuid.UUID, file_id: uuid.UUID) -> str:
    """Return the final, non-presigned object key for one file (Scope §6.3)."""
    return OBJECT_KEY_TEMPLATE.format(
        organisation_id=organisation_id,
        file_id=file_id,
    )


def _staging_key_for(
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
    token: uuid.UUID,
) -> str:
    """Return the unique staging key the browser PUT capability targets."""
    return STAGING_KEY_TEMPLATE.format(
        organisation_id=organisation_id,
        file_id=file_id,
        token=token,
    )


def _not_found() -> NotFoundError:
    return NotFoundError(
        code="file_not_found",
        message="The file could not be found.",
    )


async def _fail_completion(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    file: File,
    object_key: str,
    reason: str,
    actual_size_bytes: int | None,
) -> None:
    """Persist the ``failed`` upload outcome and its audit row in one commit."""
    file.status = FileStatus.FAILED
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_FILE_UPLOAD_FAILED,
        resource_type="file",
        resource_id=str(file.id),
        metadata={
            "object_key": object_key,
            "expected_size_bytes": file.size_bytes,
            "actual_size_bytes": actual_size_bytes,
            "reason": reason,
        },
    )
    await session.commit()


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
    final_key = object_key_for(organisation_id, file_id)
    # Plan P6: the browser's capability targets a unique staging key, never the
    # final key the rest of the system reads. The final key is deterministic
    # from the file id (the router returns it as the storage reference), and
    # ``object_key`` starts at staging and is switched to the final key on
    # successful promotion.
    staging_key = _staging_key_for(organisation_id, file_id, uuid7())
    file = File(
        id=file_id,
        organisation_id=organisation_id,
        storage_provider=settings.storage_provider,
        storage_bucket=settings.storage_bucket,
        object_key=staging_key,
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
        # Bound the capability: an old signed PUT can never outlive the
        # reviewed window and can only target staging.
        expires_in=timedelta(seconds=settings.storage_upload_url_ttl_seconds),
    )
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_FILE_UPLOAD_STARTED,
        resource_type="file",
        resource_id=str(file_id),
        metadata={
            "staging_object_key": file.object_key,
            "final_object_key": final_key,
            "original_filename": original_filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
        },
    )
    await session.commit()
    await session.refresh(file)
    return file, signed_url


async def _get_file_locked(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
) -> File:
    """Return one file row locked ``FOR UPDATE`` for a completion mutation.

    ``FOR UPDATE`` serialises concurrent completion/replay calls on the same
    file: the second transaction blocks, then observes the first's ``uploaded``
    status and returns its existing processing job instead of scheduling a
    duplicate. The org-scoped (and not-deleted) filter is the same isolation
    boundary as :func:`get_file`.
    """
    file = await session.scalar(
        org_scoped_files_statement(organisation_id).where(File.id == file_id).with_for_update()
    )
    if file is None:
        raise _not_found()
    return file


async def complete_upload(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
    checksum: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> tuple[File, uuid.UUID | None]:
    """Verify the stored object, mark the file ``uploaded`` and schedule the job.

    The browser's direct PUT is verified, never trusted (BP §17 security): the
    object must exist, its size must match the declared ``size_bytes``, and
    when the client supplied a checksum it must compare equal to the provider's
    (the checksum is opaque; equality only). Verification failure fails the
    file and raises a 422 so the client knows the upload was rejected.

    Once verified, the file is marked ``uploaded`` and the durable processing
    job is durably scheduled (plan P3, blueprint §19): the job row is written
    with ``job_type="file.processing"``, ``input_reference`` set to the file
    id, and its ``job.dispatch_requested`` outbox event in the same
    transaction; the coordinator publishes the reference-only broker message.
    The returned job id is what the client polls via ``GET /api/v1/jobs/
    {job_id}`` (the response schema carries it as ``processing_job_id``).
    """
    # Imported lazily: the task module imports this service, so a module-level
    # import would be circular. The task module is the single source of truth
    # for the ``job_type`` identity.
    from app.modules.files import tasks as files_tasks

    file = await _get_file_locked(session, organisation_id=organisation_id, file_id=file_id)
    if file.status in _POST_COMPLETION_STATUSES:
        # Idempotent replay (plan P6): a retried completion returns the same
        # processing job for every valid post-completion lifecycle state —
        # ``uploaded`` (the worker has not started), ``processing`` and
        # ``ready`` (the worker advanced the row after the first completion)
        # all resolve to the job the first call scheduled. A missing job is
        # only possible for files created before this contract; return the
        # file without one. Terminal failure states (failed/quarantined/
        # deleted) are handled below and are never resurrected.
        existing = await jobs_service.find_job_by_input_reference(
            session,
            organisation_id=organisation_id,
            job_type=files_tasks.JOB_TYPE_FILE_PROCESSING,
            input_reference=str(file.id),
        )
        return file, existing.id if existing is not None else None
    if file.status != FileStatus.PENDING:
        raise ConflictError(
            code="file_not_pending",
            message="Only a pending file can be completed.",
        )
    staging_key = file.object_key
    object_info = await get_storage().head_object(staging_key)
    verified = object_info is not None and object_info.size_bytes == file.size_bytes
    if verified and checksum is not None:
        verified = object_info is not None and object_info.checksum == checksum
    if not verified:
        await _fail_completion(
            session,
            organisation_id=organisation_id,
            actor_user_id=actor_user_id,
            file=file,
            object_key=staging_key,
            reason=(
                "object_missing"
                if object_info is None
                else "size_mismatch"
                if object_info.size_bytes != file.size_bytes
                else "checksum_mismatch"
            ),
            actual_size_bytes=object_info.size_bytes if object_info else None,
        )
        raise ValidationError(
            code="upload_verification_failed",
            message="The uploaded object could not be verified; the file has been marked failed.",
        )
    # Plan P6 promotion: copy the verified staging object onto the
    # deterministic final key the rest of the system reads. The final key is
    # never signed for a PUT, so an old capability cannot mutate approved bytes.
    final_key = object_key_for(organisation_id, file.id)
    try:
        await get_storage().copy_object(source_key=staging_key, destination_key=final_key)
    except KeyError:
        # Staging vanished between the head and the copy (a racing cleanup):
        # fail the file rather than promote a missing object.
        await _fail_completion(
            session,
            organisation_id=organisation_id,
            actor_user_id=actor_user_id,
            file=file,
            object_key=staging_key,
            reason="object_missing",
            actual_size_bytes=None,
        )
        raise ValidationError(
            code="upload_verification_failed",
            message="The uploaded object could not be verified; the file has been marked failed.",
        ) from None
    promoted = await get_storage().head_object(final_key)
    if promoted is None or promoted.size_bytes != file.size_bytes or not promoted.checksum:
        await _fail_completion(
            session,
            organisation_id=organisation_id,
            actor_user_id=actor_user_id,
            file=file,
            object_key=final_key,
            reason=(
                "promotion_failed"
                if promoted is None or promoted.size_bytes != file.size_bytes
                else "promotion_identity_missing"
            ),
            actual_size_bytes=promoted.size_bytes if promoted else None,
        )
        raise ValidationError(
            code="upload_verification_failed",
            message="The uploaded object could not be promoted; the file has been marked failed.",
        )
    # Plan P6 immutable uploads (AC14): the promoted bytes must be exactly the
    # bytes that passed the pre-copy check. A same-size overwrite of the
    # staging key landing between the head and the copy would otherwise
    # promote different bytes than the ones verified (and than the caller's
    # checksum); re-verify the promoted identity against the verified staging
    # identity, and against the caller's checksum when one was supplied.
    verified_identity = object_info.checksum if object_info is not None else None
    if (verified_identity is not None and promoted.checksum != verified_identity) or (
        checksum is not None and promoted.checksum != checksum
    ):
        await _fail_completion(
            session,
            organisation_id=organisation_id,
            actor_user_id=actor_user_id,
            file=file,
            object_key=final_key,
            reason="promotion_identity_mismatch",
            actual_size_bytes=promoted.size_bytes,
        )
        raise ValidationError(
            code="upload_verification_failed",
            message="The uploaded object could not be promoted; the file has been marked failed.",
        )
    file.object_key = final_key
    file.status = FileStatus.UPLOADED
    # The immutable content identity is pinned from the promoted object (whose
    # stable identity was just verified), so a later same-key overwrite is
    # detected by the source authority/download.
    file.checksum = promoted.checksum
    file.content_identity = promoted.checksum
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_FILE_UPLOADED,
        resource_type="file",
        resource_id=str(file.id),
        metadata={
            "object_key": final_key,
            "size_bytes": file.size_bytes,
            "checksum": file.checksum,
            "content_identity": file.content_identity,
        },
    )
    # Plan P6 atomic completion: the file transition, audit row, durable
    # processing job and its dispatch outbox event commit in one transaction
    # (``commit=False``), so a failed schedule leaves no ``uploaded`` file
    # without a processable job.
    job = await jobs_service.schedule_job(
        session,
        organisation_id=organisation_id,
        job_type=files_tasks.JOB_TYPE_FILE_PROCESSING,
        input_reference=str(file.id),
        actor_user_id=actor_user_id,
        commit=False,
    )
    await session.commit()
    # The composed transaction is durable: record the enqueue metric now (the
    # ``commit=False`` schedule deliberately does not, so a rollback cannot
    # report a job that was never committed).
    jobs_service.record_job_enqueued(job)
    await session.refresh(file)
    # Best-effort staging cleanup after the durable commit. A failure leaves an
    # orphaned staging object (the retention/lifecycle backstop owns it); the
    # final bytes are unaffected.
    with contextlib.suppress(Exception):
        await get_storage().delete_object(staging_key)
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
    """Return a short-lived signed GET URL for one verified, trusted object.

    Only a file whose object has reached ``ready`` — verification plus the
    scanning verdict — with a pinned identity still matching the stored object
    is downloadable. The document lifecycle/identity decision is delegated to
    the one source authority (:class:`DocumentSourceAuthority`) that inline AI,
    streamed AI and job retries also use, so the download boundary can never
    drift; its denial is translated here into the download-appropriate 409. A
    pending, uploaded, processing, failed, quarantined or deleted file, or a
    file whose bytes changed after approval, is a 409, never a signed URL.
    """
    file = await get_file(session, organisation_id=organisation_id, file_id=file_id)
    try:
        await _DOCUMENT_AUTHORITY.authorize_file(
            organisation_id=organisation_id,
            file=file,
        )
    except AIInputValidationError as exc:
        raise ConflictError(
            code="file_not_downloadable",
            message="The file is not ready to download.",
        ) from exc
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
    ownership: jobs_service.JobOwnership | None = None,
) -> File:
    """Transition a file ``uploaded`` -> ``processing`` (worker-side, §6.5).

    Called by the ``process_file`` task. Idempotent across retries: a file
    already ``processing`` (or already ``ready``, when a retried message
    re-runs after the file finished) is returned untouched, so the task can be
    safely re-run on a re-delivered message. Any other state is a 409 — a
    pending, failed or deleted file never enters processing.

    When ``ownership`` is supplied (the worker path), the owning job is locked
    and re-verified in this transaction before the transition commits, so a
    superseded attempt cannot mutate the file (plan P2, AC5).
    """
    if ownership is not None:
        await jobs_service.verify_ownership(session, ownership)
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
    ownership: jobs_service.JobOwnership | None = None,
) -> File:
    """Transition a file ``processing`` -> ``ready`` (worker-side, §6.5).

    Called by the ``process_file`` task once the stored object is verified. A
    file that is already ``ready`` is returned untouched (idempotent retry); a
    file not in ``processing`` is a 409, so a ready file is never moved again.
    When ``ownership`` is supplied the owning job is locked and re-verified in
    this transaction before the transition commits (plan P2, AC5).
    """
    if ownership is not None:
        await jobs_service.verify_ownership(session, ownership)
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
    ownership: jobs_service.JobOwnership | None = None,
) -> File:
    """Mark a file ``failed`` after a worker-side verification failure (§6.5).

    Called by the ``process_file`` task when the stored object cannot be
    verified while processing (missing, or a size that drifted from the
    declaration). Idempotent: an already-``failed`` or ``deleted`` file is
    returned untouched, so a retried message cannot double-audit. The audit
    row reuses ``file.upload_failed`` with the reason in the metadata, exactly
    like the completion-time failure path. When ``ownership`` is supplied the
    owning job is locked and re-verified in this transaction before the
    transition commits (plan P2, AC5).
    """
    if ownership is not None:
        await jobs_service.verify_ownership(session, ownership)
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


async def scan_file_object(
    *,
    object_key: str,
    content_type: str | None,
) -> ScanVerdict:
    """Run the configured upload scanner against one verified stored object.

    Plan P6: the worker calls this before a file may become ``ready``. The
    scanner reads through the provider-neutral storage interface, bounded by
    the template upload ceiling. A scanner outage raises
    :class:`~app.scanning.ScannerUnavailableError`, which the worker lets
    propagate as a transient failure so the file never becomes trusted while
    the verdict is unknown.
    """
    return await get_scanner().scan(
        storage=get_storage(),
        object_key=object_key,
        content_type=content_type,
        max_bytes=get_settings().storage_max_upload_size,
    )


async def mark_file_quarantined(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file_id: uuid.UUID,
    reason: str,
    ownership: jobs_service.JobOwnership | None = None,
) -> File:
    """Mark a file ``quarantined`` after the scanner rejects it (plan P6).

    Idempotent: an already-quarantined/failed/deleted file is returned
    untouched, so a retried message cannot double-audit. When ``ownership`` is
    supplied the owning job is locked and re-verified in this transaction
    before the transition commits (plan P2, AC5).
    """
    if ownership is not None:
        await jobs_service.verify_ownership(session, ownership)
    file = await get_file(session, organisation_id=organisation_id, file_id=file_id)
    if file.status in (FileStatus.QUARANTINED, FileStatus.FAILED, FileStatus.DELETED):
        return file
    file.status = FileStatus.QUARANTINED
    await record_event(
        session,
        organisation_id=organisation_id,
        action=ACTION_FILE_QUARANTINED,
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
