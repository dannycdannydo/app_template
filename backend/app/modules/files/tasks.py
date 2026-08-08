"""File-processing Dramatiq task (Scope §6.5, §6.4, blueprint §17, §18).

``process_file`` is the example job that makes files and jobs work together:
after the browser's direct PUT is verified at completion, this task re-verifies
the stored object (the second, worker-side check of BP §17's direct-upload
flow), advances the file ``uploaded`` -> ``processing`` -> ``ready`` with job
progress 0 -> 100, and closes the durable job as ``succeeded``. Failure (the
object vanished or its size drifted) marks the file ``failed`` and the job
``failed`` with an ``error_code``.

Scope §6.4 closes the loop the release exists to demonstrate: on completion
the task notifies the uploader. A file that reaches ``ready`` produces a
``file.ready`` in-app notification with its email delivery job enqueued; a
file that fails produces ``file.failed`` the same way (the notification and
delivery creation is idempotent, so a retried message cannot double-notify).

Idempotency follows the rules in ``jobs.service`` (BP §18): the task re-runs
safely on a retried or re-delivered message. A job that already reached a
terminal state is left untouched (terminal states are never re-run); the file
transition helpers each return early when the file is already in (or past)
the target state. Permanent failures mark the durable row and the file
themselves and then raise :class:`JobPermanentError`, which the retry policy
declares in ``throws`` so it is never retried; transient errors propagate and
the Retries middleware retries them up to ``MAX_ATTEMPTS``.

The handler function is deliberately separate from its actor declaration so a
test can re-declare it bound to its own broker (the same pattern as
``app.modules.jobs.tasks``). The actor runs on the ``documents`` queue
(blueprint §18 example queues) so document workloads never compete with the
``default`` infrastructure queue.
"""

from __future__ import annotations

import uuid

import dramatiq
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.logging import bind_worker_context
from app.db.session import async_session_factory
from app.modules.files import service as files_service
from app.modules.files.models import File
from app.modules.jobs import service as jobs_service
from app.modules.notifications import service as notifications_service
from app.modules.users.models import User
from app.storage import get_storage

# The durable ``job_type`` this task produces (Scope §6.5). ``create_and_enqueue``
# in the files service names it when it writes the row, so the constant lives
# with the task that owns the identity.
JOB_TYPE_FILE_PROCESSING = "file.processing"

# The queue document workloads run on (blueprint §18 example queues: default,
# documents, integrations, ai, emails). The retries-exhausted finalizer keeps
# running on the infrastructure ``default`` queue (jobs.tasks).
HANDLER_QUEUE = "documents"

# Progress milestones (acceptance §5.8: the polling client sees 0 -> 100 while
# the file advances uploaded -> processing -> ready).
_PROGRESS_VERIFYING = 10
_PROGRESS_VERIFIED = 50
_PROGRESS_PROCESSING = 80

# Permanent error codes the file-processing job records on the durable row.
ERROR_CODE_FILE_NOT_FOUND = "file_not_found"
ERROR_CODE_VERIFICATION_FAILED = "file_verification_failed"

logger = structlog.get_logger()


async def process_file(job_id: str) -> None:
    """Verify the stored object and move the file to ``ready``, with progress.

    One attempt of the file-processing job: load the durable row, mark it
    ``running``, then re-verify the stored object against the file record's
    declaration (existence and size, mirroring the completion-time check). On
    success the file advances to ``ready`` and the job to ``succeeded`` with
    progress 100; on a permanent failure the file is marked ``failed`` and the
    job ``failed`` with the matching ``error_code`` before
    :class:`JobPermanentError` is raised so the message is never retried.
    """
    job_uuid = uuid.UUID(job_id)
    bind_worker_context(job_id=str(job_uuid))
    logger.info("file.processing.started")
    async with async_session_factory() as session:
        job = await jobs_service.get_job_for_task(session, job_id=job_uuid)
        bind_worker_context(job_id=str(job_uuid), resource_id=job.input_reference)
        if jobs_service.is_terminal(job.status):
            # A re-delivered message for a finished job: terminal states are
            # never re-run (acceptance §5.7), so this attempt is a no-op.
            logger.info("file.processing.skipped", reason="terminal_state")
            return

        await jobs_service.mark_running(session, job_id=job_uuid)
        file_id = uuid.UUID(job.input_reference)
        try:
            file = await files_service.get_file(
                session,
                organisation_id=job.organisation_id,
                file_id=file_id,
            )
        except NotFoundError as exc:
            # The file row is gone or soft-deleted while the job was queued.
            # No file row to fail, so the durable job alone records it; a
            # retry cannot fix a missing row, so this is a permanent failure.
            await jobs_service.fail(
                session,
                job_id=job_uuid,
                error_code=ERROR_CODE_FILE_NOT_FOUND,
                error_message="The file this job processes no longer exists.",
            )
            logger.warning("file.processing.failed", error_code=ERROR_CODE_FILE_NOT_FOUND)
            raise jobs_service.JobPermanentError(
                "the file referenced by the job does not exist"
            ) from exc

        await jobs_service.update_progress(session, job_id=job_uuid, progress=_PROGRESS_VERIFYING)
        object_info = await get_storage().head_object(file.object_key)
        verified = object_info is not None and object_info.size_bytes == file.size_bytes
        if not verified:
            reason = "object_missing" if object_info is None else "size_mismatch"
            await files_service.mark_file_failed(
                session,
                organisation_id=job.organisation_id,
                file_id=file.id,
                reason=reason,
            )
            await _notify_uploader(
                session,
                organisation_id=job.organisation_id,
                file=file,
                notification_type=notifications_service.NOTIFICATION_TYPE_FILE_FAILED,
                title=notifications_service.FILE_FAILED_TITLE,
                body=notifications_service.FILE_FAILED_BODY.format(filename=file.original_filename),
            )
            await jobs_service.fail(
                session,
                job_id=job_uuid,
                error_code=ERROR_CODE_VERIFICATION_FAILED,
                error_message=("The stored object could not be verified while processing."),
            )
            logger.warning(
                "file.processing.failed",
                error_code=ERROR_CODE_VERIFICATION_FAILED,
                reason=reason,
            )
            raise jobs_service.JobPermanentError(
                f"the stored object could not be verified ({reason})"
            )

        await jobs_service.update_progress(session, job_id=job_uuid, progress=_PROGRESS_VERIFIED)
        await files_service.mark_file_processing(
            session,
            organisation_id=job.organisation_id,
            file_id=file.id,
        )
        await jobs_service.update_progress(session, job_id=job_uuid, progress=_PROGRESS_PROCESSING)
        await files_service.mark_file_ready(
            session,
            organisation_id=job.organisation_id,
            file_id=file.id,
        )
        await _notify_uploader(
            session,
            organisation_id=job.organisation_id,
            file=file,
            notification_type=notifications_service.NOTIFICATION_TYPE_FILE_READY,
            title=notifications_service.FILE_READY_TITLE,
            body=notifications_service.FILE_READY_BODY.format(filename=file.original_filename),
        )
        await jobs_service.succeed(
            session,
            job_id=job_uuid,
            result_reference=str(file.id),
        )
        logger.info("file.processing.succeeded", file_id=str(file.id))


async def _notify_uploader(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    file: File,
    notification_type: str,
    title: str,
    body: str,
) -> None:
    """Create the in-app notification and enqueue its email for the uploader.

    Scope §6.4: a file's terminal outcome (``ready`` or ``failed``) is
    delivered to the uploader as an in-app notification plus an email delivery
    job. The notification service deduplicates on (org, user, type, resource),
    so a retried or re-delivered message cannot double-notify or double-send.

    A file with no recorded uploader, or whose uploader row is gone, has
    nobody to notify: the task logs and continues — the file and job outcomes
    are already audited, and a notification needs a recipient.
    """
    if file.created_by_user_id is None:
        logger.info("file.processing.notification_skipped", reason="no_uploader")
        return
    uploader = await session.get(User, file.created_by_user_id)
    if uploader is None:
        logger.info("file.processing.notification_skipped", reason="uploader_missing")
        return
    await notifications_service.create_file_notification(
        session,
        organisation_id=organisation_id,
        user_id=file.created_by_user_id,
        notification_type=notification_type,
        title=title,
        body=body,
        resource_id=str(file.id),
        recipient_email=uploader.email,
        actor_user_id=file.created_by_user_id,
    )
    logger.info(
        "file.processing.notified",
        notification_type=notification_type,
        file_id=str(file.id),
    )


process_file_actor = dramatiq.actor(
    queue_name=HANDLER_QUEUE,
    **jobs_service.retry_policy(),
)(process_file)
