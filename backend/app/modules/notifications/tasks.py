"""Notification email Dramatiq task (Scope §6.3/§6.4, blueprint §18, §20).

``send_notification_email`` delivers one notification's email through the
provider-neutral adapter (ADR-0015): it loads the durable ``notification.email``
job (whose ``input_reference`` is the delivery id), advances the delivery row
``queued -> running -> succeeded/failed``, records the provider's message id,
and closes the durable job exactly like ``process_file`` does for file
processing.

Email is only ever sent from this worker task — never inside an HTTP handler
(blueprint §20), a rule the test suite enforces structurally.

Idempotency and execution ownership follow the durable delivery plan (P2):
the task runs its domain work through ``app.modules.jobs.execution``'s shared
wrapper, which claims the dispatch atomically, defers a duplicate with a live
lease, releases ownership before a transient error propagates and treats a
stale attempt as a no-op. A re-delivered message for a job or delivery that
already reached a terminal state is a no-op, so a retried or re-delivered
message can never double-send. The delivery row is marked ``failed`` (with its
``notification.delivery_failed`` audit row) and the durable job ``failed``
with an ``error_code`` before :class:`JobPermanentError` is raised, so the
message is never retried (a failed delivery is terminal by the same rule a
succeeded one is). The transient/permanent split of SMTP failures is
introduced in plan P4; until then every ``EmailSendError`` is permanent.

The handler function is deliberately separate from its actor declaration so a
test can re-declare it bound to its own broker (the same pattern as
``app.modules.files.tasks``). The actor runs on the ``emails`` queue
(blueprint §18 example queues) so email workloads never compete with the
``default`` infrastructure queue.
"""

from __future__ import annotations

import uuid
from typing import NoReturn

import dramatiq
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import bind_worker_context
from app.db.session import async_session_factory
from app.email import get_email_provider
from app.email.base import EmailSendError
from app.modules.jobs import service as jobs_service
from app.modules.jobs.execution import DurableJobContext, run_claimed
from app.modules.notifications import service as notifications_service

# The durable ``job_type`` this task produces (Scope §6.3). ``send_test_notification``
# names it when it writes the row, so the constant lives with the task that
# owns the identity (the same convention as ``files.tasks.JOB_TYPE_FILE_PROCESSING``).
JOB_TYPE_NOTIFICATION_EMAIL = "notification.email"

# The queue email workloads run on (blueprint §18 example queues: default,
# documents, integrations, ai, emails). The retries-exhausted finalizer keeps
# running on the infrastructure ``default`` queue (jobs.tasks).
HANDLER_QUEUE = "emails"

# Permanent error code the email-delivery job records on the durable row.
ERROR_CODE_EMAIL_DELIVERY_FAILED = "email_delivery_failed"
ERROR_CODE_INVALID_JOB_CONTEXT = "invalid_notification_job_context"

logger = structlog.get_logger()


async def send_notification_email(job_id: str) -> None:
    """Send one notification's email and track it on the delivery row.

    One attempt of the email-delivery job: load the durable row, skip a
    terminal job (terminal states are never re-run, acceptance §5.7), then run
    the attempt through the shared execution wrapper (plan P2), which claims
    the dispatch before the delivery and provider work starts; a foreign job
    type is rejected under that claimed owner. On success the delivery advances
    to ``succeeded`` with the provider's message id and ``sent_at`` stamped,
    and the job to ``succeeded`` with the provider message id as its result
    reference. On a provider failure the delivery is marked ``failed`` (with
    its audit row) and the job ``failed`` with the matching ``error_code``
    before :class:`JobPermanentError` is raised so the message is never
    retried.
    """
    job_uuid = uuid.UUID(job_id)
    bind_worker_context(job_id=str(job_uuid))
    logger.info("notification.email.started")
    async with async_session_factory() as session:
        job = await jobs_service.get_job_for_task(session, job_id=job_uuid)
        bind_worker_context(job_id=str(job_uuid), resource_id=job.input_reference)
        if jobs_service.is_terminal(job.status):
            # A re-delivered message for a finished job: terminal states are
            # never re-run (acceptance §5.7), so this attempt is a no-op.
            logger.info("notification.email.skipped", reason="terminal_state")
            return

    await run_claimed(job_id=job_uuid, handler=_send_notification_email_attempt)


async def _send_notification_email_attempt(
    context: DurableJobContext, session: AsyncSession
) -> None:
    """One owned attempt of the email-delivery job (plan P2 ownership)."""
    job = context.job
    if job.job_type != JOB_TYPE_NOTIFICATION_EMAIL:
        # The wrong-type settlement runs under the claimed owner (plan P2), so
        # it is accepted even once every job row carries a dispatch id (P3):
        # the handler fails the durable row with the invalid-context error
        # before raising the never-retried permanent error.
        await _fail_invalid_context(
            session,
            job_id=context.job_id,
            reason="wrong_job_type",
            owner_token=context.owner_token,
        )
    delivery = await notifications_service.get_delivery_for_task(
        session, delivery_id=uuid.UUID(job.input_reference)
    )
    notification = await notifications_service.get_notification_for_task(
        session, notification_id=delivery.notification_id
    )
    if notification.organisation_id != job.organisation_id:
        await _fail_invalid_context(
            session,
            job_id=context.job_id,
            reason="organisation_mismatch",
            owner_token=context.owner_token,
        )

    if notifications_service.is_delivery_terminal(delivery.status):
        # A re-delivered message for an already-finished delivery: terminal
        # deliveries are never re-sent (Scope §6.4 idempotency rule), so
        # close the durable job without sending a second email.
        await jobs_service.succeed(
            session,
            job_id=context.job_id,
            result_reference=str(delivery.id),
            owner_token=context.owner_token,
        )
        logger.info("notification.email.skipped", reason="delivery_terminal")
        return

    # The durable reference context is complete and owned before the delivery
    # row moves to running or an external provider can be called.
    await notifications_service.mark_delivery_running(session, delivery_id=delivery.id)
    settings = get_settings()
    provider = get_email_provider()
    try:
        result = await provider.send_email(
            from_address=settings.email_from,
            to_address=delivery.recipient,
            subject=notification.title,
            text_body=notification.body,
        )
    except EmailSendError as exc:
        await notifications_service.mark_delivery_failed(
            session,
            delivery_id=delivery.id,
            organisation_id=job.organisation_id,
            error_message=str(exc),
        )
        await jobs_service.fail(
            session,
            job_id=context.job_id,
            error_code=ERROR_CODE_EMAIL_DELIVERY_FAILED,
            error_message="The notification email could not be sent.",
            owner_token=context.owner_token,
        )
        logger.warning("notification.email.failed", error_code=ERROR_CODE_EMAIL_DELIVERY_FAILED)
        raise jobs_service.JobPermanentError("the notification email could not be sent") from exc

    await notifications_service.mark_delivery_succeeded(
        session,
        delivery_id=delivery.id,
        provider_message_id=result.provider_message_id,
    )
    await jobs_service.succeed(
        session,
        job_id=context.job_id,
        result_reference=result.provider_message_id,
        owner_token=context.owner_token,
    )
    logger.info(
        "notification.email.succeeded",
        provider_message_id=result.provider_message_id,
    )


async def _fail_invalid_context(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    reason: str,
    owner_token: uuid.UUID | None = None,
) -> NoReturn:
    """Fail a malformed durable notification reference without provider work."""
    await jobs_service.fail(
        session,
        job_id=job_id,
        error_code=ERROR_CODE_INVALID_JOB_CONTEXT,
        error_message="The notification job references inconsistent tenant data.",
        owner_token=owner_token,
    )
    logger.warning(
        "notification.email.failed",
        error_code=ERROR_CODE_INVALID_JOB_CONTEXT,
        reason=reason,
    )
    raise jobs_service.JobPermanentError("the notification job context is invalid")


send_notification_email_actor = dramatiq.actor(
    queue_name=HANDLER_QUEUE,
    **jobs_service.retry_policy(),
)(send_notification_email)
