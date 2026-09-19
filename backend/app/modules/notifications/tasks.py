"""Notification email Dramatiq task (Scope §6.3/§6.4, blueprint §18, §20).

``send_notification_email`` delivers one notification's email through the
provider-neutral adapter (ADR-0015): it loads the durable ``notification.email``
job (whose ``input_reference`` is the delivery id), advances the delivery row
from ``queued`` through ``running`` to a terminal outcome, records its stable
identity and provider message id, and closes the durable job exactly like
``process_file`` does for file processing.

Email is only ever sent from this worker task — never inside an HTTP handler
(blueprint §20), a rule the test suite enforces structurally.

Idempotency and execution ownership follow the durable delivery plan (P2):
the task runs its domain work through ``app.modules.jobs.execution``'s shared
wrapper, which claims the dispatch atomically, defers a duplicate with a live
lease, persists the retry decision after a transient error and treats a stale
attempt as a no-op. A re-delivered message for a job or delivery that
already reached a terminal state is a no-op, so a retried or re-delivered
message can never double-send. A takeover that finds an in-flight delivery
records ``attention_required`` instead of resending. The delivery row is
marked ``failed`` (with its ``notification.delivery_failed`` audit row) and
the durable job ``failed`` with an ``error_code`` before
:class:`JobPermanentError` is raised, so the message is never retried (a
failed delivery is terminal by the same rule a succeeded one is).
Definitely-unsent failures retry, explicit rejection fails, and
acceptance-unknown failures terminally require operator attention.

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
from app.email.base import (
    AcceptanceUnknownEmailSendError,
    EmailSendError,
    PermanentEmailSendError,
    TransientEmailSendError,
)
from app.modules.jobs import service as jobs_service
from app.modules.jobs.execution import DurableJobContext, run_claimed
from app.modules.jobs.models import Job
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
ERROR_CODE_EMAIL_DELIVERY_ATTENTION_REQUIRED = "email_delivery_acceptance_unknown"
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
    if context.job_type != JOB_TYPE_NOTIFICATION_EMAIL:
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
    # The notifications group is user-private (plan P3, ADR-0022 decisions 3
    # and 5): the worker binds the durable job's organisation and recipient user
    # as transaction-local context before it reads the protected delivery and
    # notification rows. The user comes from the durable row, never the broker.
    # ``created_by_user_id`` is the recipient by invariant: the producers
    # enforce actor == recipient for ``notification.email`` jobs
    # (``notifications_service._require_durable_recipient``) until a dedicated
    # durable recipient identity is modelled.
    if context.created_by_user_id is None:
        await _fail_invalid_context(
            session,
            job_id=context.job_id,
            reason="missing_recipient_user",
            owner_token=context.owner_token,
        )
    recipient_user_id = context.created_by_user_id
    try:
        delivery = await notifications_service.get_delivery_for_task(
            session,
            delivery_id=uuid.UUID(context.input_reference),
            organisation_id=context.organisation_id,
            user_id=recipient_user_id,
        )
        notification = await notifications_service.get_notification_for_task(
            session,
            notification_id=delivery.notification_id,
            organisation_id=context.organisation_id,
            user_id=recipient_user_id,
        )
    except notifications_service.NotFoundError:
        # A missing row under the durable context means the referenced delivery
        # or notification does not belong to this organisation/recipient, or was
        # deleted. RLS makes a foreign row indistinguishable from a missing one;
        # fail closed and permanently rather than retrying.
        await _fail_invalid_context(
            session,
            job_id=context.job_id,
            reason="protected_row_not_visible",
            owner_token=context.owner_token,
        )
    # The application-level scope check stays the first enforcement layer even
    # where RLS is bypassed (owner/maintenance paths, or a superuser test
    # credential): a notification that is not the durable job's organisation and
    # recipient is never sent.
    if (
        notification.organisation_id != context.organisation_id
        or notification.user_id != recipient_user_id
    ):
        await _fail_invalid_context(
            session,
            job_id=context.job_id,
            reason="context_mismatch",
            owner_token=context.owner_token,
        )

    if notifications_service.is_delivery_terminal(delivery.status):
        await _settle_job_for_terminal_delivery(session, context=context, delivery=delivery)
        return

    if delivery.status is notifications_service.NotificationDeliveryStatus.RUNNING:
        # A newer attempt finding RUNNING means the prior worker crossed the
        # durable pre-send boundary but never recorded a definite outcome. It
        # may have died after provider acceptance, so automatic resend is unsafe.
        await _settle_acceptance_unknown(
            session,
            context=context,
            delivery_id=delivery.id,
            user_id=recipient_user_id,
        )

    # The durable reference context is complete and owned before the delivery
    # row moves to running or an external provider can be called.
    await notifications_service.mark_delivery_running(
        session,
        delivery_id=delivery.id,
        organisation_id=context.organisation_id,
        user_id=recipient_user_id,
        ownership=context.ownership,
    )
    # Revalidate ownership immediately before the external provider call (plan
    # P2, AC5): holding the job row lock across the network call is
    # undesirable, so this is a lock-free read that still rejects a superseded
    # attempt before any external effect. The outcome commit below re-locks and
    # re-verifies through ``mark_delivery_succeeded``.
    await jobs_service.verify_ownership(session, context.ownership, lock=False)
    settings = get_settings()
    provider = get_email_provider()
    try:
        result = await provider.send_email(
            delivery_identity=delivery.delivery_identity,
            from_address=settings.email_from,
            to_address=delivery.recipient,
            subject=notification.title,
            text_body=notification.body,
        )
    except TransientEmailSendError:
        await notifications_service.return_delivery_to_queue(
            session,
            delivery_id=delivery.id,
            organisation_id=context.organisation_id,
            user_id=recipient_user_id,
            ownership=context.ownership,
        )
        logger.warning("notification.email.retrying", error_code="email_delivery_transient")
        raise
    except AcceptanceUnknownEmailSendError as exc:
        await _settle_acceptance_unknown(
            session,
            context=context,
            delivery_id=delivery.id,
            user_id=recipient_user_id,
            cause=exc,
        )
    except PermanentEmailSendError as exc:
        await notifications_service.mark_delivery_failed(
            session,
            delivery_id=delivery.id,
            organisation_id=context.organisation_id,
            user_id=recipient_user_id,
            error_code=notifications_service.DELIVERY_ERROR_PERMANENTLY_REJECTED,
            commit=False,
            ownership=context.ownership,
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
    except EmailSendError as exc:
        await notifications_service.mark_delivery_failed(
            session,
            delivery_id=delivery.id,
            organisation_id=context.organisation_id,
            user_id=recipient_user_id,
            error_code=notifications_service.DELIVERY_ERROR_UNCLASSIFIED_PROVIDER,
            commit=False,
            ownership=context.ownership,
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
        organisation_id=context.organisation_id,
        user_id=recipient_user_id,
        commit=False,
        ownership=context.ownership,
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


async def _settle_job_for_terminal_delivery(
    session: AsyncSession,
    *,
    context: DurableJobContext,
    delivery: notifications_service.NotificationDelivery,
) -> None:
    """Make a recovered job match its already-terminal delivery outcome."""
    if delivery.status is notifications_service.NotificationDeliveryStatus.SUCCEEDED:
        await jobs_service.succeed(
            session,
            job_id=context.job_id,
            result_reference=delivery.provider_message_id or delivery.delivery_identity,
            owner_token=context.owner_token,
        )
        logger.info("notification.email.skipped", reason="delivery_succeeded")
        return

    if delivery.status is notifications_service.NotificationDeliveryStatus.ATTENTION_REQUIRED:
        error_code = ERROR_CODE_EMAIL_DELIVERY_ATTENTION_REQUIRED
        error_message = "The notification email requires operator attention."
    else:
        error_code = ERROR_CODE_EMAIL_DELIVERY_FAILED
        error_message = "The notification email could not be sent."
    await jobs_service.fail(
        session,
        job_id=context.job_id,
        error_code=error_code,
        error_message=error_message,
        owner_token=context.owner_token,
    )
    logger.warning("notification.email.skipped", reason="delivery_terminal", error_code=error_code)
    raise jobs_service.JobPermanentError("the notification delivery is already terminal")


async def _settle_acceptance_unknown(
    session: AsyncSession,
    *,
    context: DurableJobContext,
    delivery_id: uuid.UUID,
    user_id: uuid.UUID,
    cause: BaseException | None = None,
) -> NoReturn:
    """Atomically terminally settle an outcome that must not be auto-retried."""
    await notifications_service.mark_delivery_attention_required(
        session,
        delivery_id=delivery_id,
        organisation_id=context.organisation_id,
        user_id=user_id,
        commit=False,
        ownership=context.ownership,
    )
    await jobs_service.fail(
        session,
        job_id=context.job_id,
        error_code=ERROR_CODE_EMAIL_DELIVERY_ATTENTION_REQUIRED,
        error_message="The notification email acceptance could not be determined.",
        owner_token=context.owner_token,
    )
    logger.warning(
        "notification.email.attention_required",
        error_code=ERROR_CODE_EMAIL_DELIVERY_ATTENTION_REQUIRED,
    )
    error = jobs_service.JobPermanentError("the notification email requires operator attention")
    if cause is not None:
        raise error from cause
    raise error


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


async def _on_notification_email_exhausted(
    session: AsyncSession, *, job_id: uuid.UUID, failure_code: str
) -> None:
    """Finalize the delivery with a delivery-specific terminal cause.

    The exhaustion hook runs inside the owner-checked job-settlement
    transaction (``jobs_service._fail_locked`` is only reached after the
    captured owner token is verified), so the delivery mutation it performs is
    already fenced by the job ownership guard and needs no second credential.
    """
    job = await session.get(Job, job_id)
    if job is None or job.created_by_user_id is None:
        # A notification job always carries its recipient user; without it the
        # user-private delivery row cannot be read or updated and the hook
        # leaves it for the owner-checked job settlement to surface.
        return
    try:
        delivery_id = uuid.UUID(job.input_reference)
    except ValueError:
        return
    delivery_error_code = {
        jobs_service.ERROR_CODE_RETRIES_EXHAUSTED: (
            notifications_service.DELIVERY_ERROR_SAFE_RETRIES_EXHAUSTED
        ),
        jobs_service.ERROR_CODE_DISPATCH_INVALID: (
            notifications_service.DELIVERY_ERROR_DISPATCH_FAILED
        ),
    }.get(failure_code, notifications_service.DELIVERY_ERROR_UNCLASSIFIED_PROVIDER)
    await notifications_service.mark_delivery_failed(
        session,
        delivery_id=delivery_id,
        organisation_id=job.organisation_id,
        user_id=job.created_by_user_id,
        error_code=delivery_error_code,
        commit=False,
    )


jobs_service.register_exhaustion_hook(JOB_TYPE_NOTIFICATION_EMAIL, _on_notification_email_exhausted)


send_notification_email_actor = dramatiq.actor(
    queue_name=HANDLER_QUEUE,
    **jobs_service.retry_policy(),
)(send_notification_email)
