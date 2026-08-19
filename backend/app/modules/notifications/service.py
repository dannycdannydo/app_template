"""Notification services (Scope §6.3, blueprint §11, §20).

The service owns transaction boundaries (BP §11): each function is one atomic
operation that commits itself, and the router never commits. Every query is
org+user-scoped through ``queries.py``, so a notification that exists for
another organisation or another recipient surfaces as a 404 (the isolation
boundary), and domain failures are raised as domain exceptions for the central
handlers (``NotFoundError`` -> 404).

``send_test_notification`` and ``create_file_notification`` are the two
producers in this release: the former is the test-send endpoint's fixed copy,
the latter is what the ``process_file`` task calls when a file finishes
processing (``file.ready`` / ``file.failed``, Scope §6.4). Each creates the
in-app notification, its email delivery row and the durable ``notification.email``
job in a single transaction, then durably schedules the job (plan P3: the
``job.dispatch_requested`` outbox event commits with the job row; the
coordinator publishes the reference-only broker message). The delivery
row helpers (``get_delivery_for_task``, ``mark_delivery_*``) are the worker-side
surface the ``send_notification_email`` task calls, mirroring the
``jobs_service`` helpers for durable jobs: a terminal delivery is never re-sent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.audit.service import (
    ACTION_NOTIFICATION_DELIVERY_FAILED,
    ACTION_NOTIFICATION_TEST_SENT,
    record_event,
)
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job
from app.modules.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationDeliveryStatus,
)
from app.modules.notifications.queries import (
    mark_all_notifications_read_statement,
    unread_notifications_count_statement,
    user_notifications_count_statement,
    user_notifications_statement,
)

# The pagination envelope contract shared with every org-scoped module (BP
# §12): ``?page=1&page_size=50`` with the ``{items, page, page_size, total}``
# body, page_size clamped to ``MAX_PAGE_SIZE``.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# The notification type the test-send endpoint produces. It follows the
# blueprint §20 dotted-event naming so the frontend can group by it later.
NOTIFICATION_TYPE_TEST = "notification.test_sent"

# Test-send copy: fixed, server-owned content — the endpoint takes no request
# body and a test notification must never carry client-supplied text.
TEST_TITLE = "Test notification"
TEST_BODY = (
    "This is a test notification. It verifies that in-app notifications and "
    "their email deliveries are wired correctly."
)

# The only delivery channel in this release (blueprint §20 delivery tracking).
DELIVERY_CHANNEL_EMAIL = "email"

# The notification types the file-processing task produces (Scope §6.4). They
# reuse the file lifecycle audit action names as dotted event names, so the
# frontend can group notifications and audit rows by the same type.
NOTIFICATION_TYPE_FILE_READY = "file.ready"
NOTIFICATION_TYPE_FILE_FAILED = "file.failed"

# The resource type file notifications link back to (blueprint §17).
RESOURCE_TYPE_FILE = "file"

# File-status notification copy: server-owned, names the file so the recipient
# knows what changed. The title stays constant per event type (the frontend can
# rely on it); the body carries the original filename.
FILE_READY_TITLE = "File ready"
FILE_READY_BODY = "Your file {filename} is ready."
FILE_FAILED_TITLE = "File failed"
FILE_FAILED_BODY = "Your file {filename} could not be processed."


def _notification_not_found() -> NotFoundError:
    return NotFoundError(
        code="notification_not_found",
        message="The notification could not be found.",
    )


def _delivery_not_found() -> NotFoundError:
    return NotFoundError(
        code="delivery_not_found",
        message="The notification delivery could not be found.",
    )


async def get_notification(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> Notification:
    """Return one notification; a foreign or other-user notification is a 404.

    The org+user-scoped filter is the isolation boundary: a notification id
    that exists in another organisation, or for another recipient in the same
    organisation, simply does not match, so cross-organisation and
    cross-recipient reads are indistinguishable from missing rows (acceptance
    §5.5).
    """
    notification = await session.scalar(
        user_notifications_statement(organisation_id, user_id).where(
            Notification.id == notification_id
        )
    )
    if notification is None:
        raise _notification_not_found()
    return notification


async def list_notifications(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    page: int,
    page_size: int,
    type: str | None = None,
) -> tuple[list[Notification], int, int]:
    """Return one page of the caller's notifications plus total and unread.

    Newest first, ties broken by id so paging is stable (the same ordering as
    files, jobs and records). ``page``/``page_size`` are validated by the
    router's query parameters; ``type`` is the only approved filter field
    (BP §12) and is enforced at the query level. The unread count is scoped to
    the same caller/org pair and rides on the envelope so a single request
    refreshes both the list and the header badge (acceptance §5.5).
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total = await session.scalar(
        user_notifications_count_statement(organisation_id, user_id, type=type)
    )
    rows = await session.scalars(
        user_notifications_statement(organisation_id, user_id, type=type)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    unread_count = await session.scalar(
        unread_notifications_count_statement(organisation_id, user_id)
    )
    return list(rows.all()), total or 0, unread_count or 0


async def unread_count(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Return the caller's unread notification count in the organisation."""
    count = await session.scalar(unread_notifications_count_statement(organisation_id, user_id))
    return count or 0


async def mark_read(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> Notification:
    """Mark one of the caller's notifications read; a foreign row is a 404.

    Idempotent: marking an already-read notification keeps its existing
    ``read_at`` (PATCH semantics — the endpoint is only ever called with the
    same effect, so there is nothing to conflict over).
    """
    notification = await get_notification(
        session,
        organisation_id=organisation_id,
        user_id=user_id,
        notification_id=notification_id,
    )
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(notification)
    return notification


async def mark_all_read(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Mark all unread notifications for the caller as read.

    The statement carries both tenant boundaries, so the bulk action cannot
    alter another organisation's or recipient's rows. Repeating it is safe:
    already-read rows do not match and the result is zero.
    """
    result = await session.execute(
        mark_all_notifications_read_statement(
            organisation_id,
            user_id,
            read_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return cast(CursorResult[Any], result).rowcount or 0


async def send_test_notification(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    recipient_email: str,
    actor_user_id: uuid.UUID | None = None,
) -> tuple[Notification, NotificationDelivery, Job]:
    """Create a test in-app notification, its email delivery and the job.

    One transaction (BP §11): the notification row, its ``email`` delivery row
    (status ``queued``), the ``notification.test_sent`` audit event and the
    durable ``notification.email`` job are written, and the job's
    ``job.dispatch_requested`` outbox event commits with it (plan P3,
    blueprint §19 — a broker outage never prevents the API from committing
    the durable queued job). The coordinator publishes the reference-only
    message with the delivery id as the job's ``input_reference`` — the same
    flow ``complete_upload`` uses for file processing.
    """
    notification = Notification(
        organisation_id=organisation_id,
        user_id=user_id,
        type=NOTIFICATION_TYPE_TEST,
        title=TEST_TITLE,
        body=TEST_BODY,
    )
    session.add(notification)
    await session.flush()
    delivery = NotificationDelivery(
        notification_id=notification.id,
        channel=DELIVERY_CHANNEL_EMAIL,
        recipient=recipient_email,
        status=NotificationDeliveryStatus.QUEUED,
    )
    session.add(delivery)
    await session.flush()
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_NOTIFICATION_TEST_SENT,
        resource_type="notification",
        resource_id=str(notification.id),
        metadata={"channel": DELIVERY_CHANNEL_EMAIL, "recipient": recipient_email},
    )
    # Imported lazily: the task module imports this service, so a module-level
    # import would be circular. By the time the test-send flow runs the module
    # is cached, so the import is a dict lookup. The task module is the single
    # source of truth for the ``job_type`` identity.
    from app.modules.notifications import tasks as notifications_tasks

    job = await jobs_service.schedule_job(
        session,
        organisation_id=organisation_id,
        job_type=notifications_tasks.JOB_TYPE_NOTIFICATION_EMAIL,
        input_reference=str(delivery.id),
        actor_user_id=actor_user_id,
    )
    await session.refresh(notification)
    await session.refresh(delivery)
    return notification, delivery, job


async def create_file_notification(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    notification_type: str,
    title: str,
    body: str,
    resource_id: str,
    recipient_email: str,
    actor_user_id: uuid.UUID | None = None,
) -> Notification:
    """Create a file-status notification, its email delivery and the job.

    Called by the ``process_file`` task (Scope §6.4) when a file finishes
    processing (``file.ready``) or fails (``file.failed``): the in-app
    notification for the uploader, its ``email`` delivery row and the durable
    ``notification.email`` job are written in one transaction and durably
    scheduled (plan P3) with the delivery id as its ``input_reference`` — the
    same scheduling flow ``send_test_notification`` uses.

    Idempotent on retry (Scope §6.4): a notification of the same type for the
    same resource and user already existing means this file's outcome was
    already notified, so the function returns the existing row without
    creating a second delivery or enqueueing a second email job. A retried or
    re-delivered ``process_file`` message therefore cannot double-notify or
    double-send.
    """
    existing = await session.scalar(
        select(Notification).where(
            Notification.organisation_id == organisation_id,
            Notification.user_id == user_id,
            Notification.type == notification_type,
            Notification.resource_type == RESOURCE_TYPE_FILE,
            Notification.resource_id == resource_id,
        )
    )
    if existing is not None:
        return existing

    notification = Notification(
        organisation_id=organisation_id,
        user_id=user_id,
        type=notification_type,
        title=title,
        body=body,
        resource_type=RESOURCE_TYPE_FILE,
        resource_id=resource_id,
    )
    session.add(notification)
    await session.flush()
    delivery = NotificationDelivery(
        notification_id=notification.id,
        channel=DELIVERY_CHANNEL_EMAIL,
        recipient=recipient_email,
        status=NotificationDeliveryStatus.QUEUED,
    )
    session.add(delivery)
    await session.flush()
    # Imported lazily: the task module imports this service, so a module-level
    # import would be circular (the same pattern as ``send_test_notification``).
    from app.modules.notifications import tasks as notifications_tasks

    await jobs_service.schedule_job(
        session,
        organisation_id=organisation_id,
        job_type=notifications_tasks.JOB_TYPE_NOTIFICATION_EMAIL,
        input_reference=str(delivery.id),
        actor_user_id=actor_user_id,
    )
    await session.refresh(notification)
    return notification


# --- Worker-side delivery helpers (called by ``send_notification_email``) ---


async def get_delivery_for_task(
    session: AsyncSession,
    *,
    delivery_id: uuid.UUID,
) -> NotificationDelivery:
    """Return the delivery row a worker task operates on (worker-side read).

    Like ``jobs_service.get_job_for_task`` this is deliberately not
    org-scoped: the worker knows only the delivery id it was messaged with.
    """
    delivery = await session.scalar(
        select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
    )
    if delivery is None:
        raise _delivery_not_found()
    return delivery


async def get_notification_for_task(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
) -> Notification:
    """Return the notification a delivery belongs to (worker-side read).

    The task needs the notification's title/body to compose the email; like
    ``get_delivery_for_task`` it is not org-scoped, because the worker knows
    only the ids it was messaged with.
    """
    notification = await session.scalar(
        select(Notification).where(Notification.id == notification_id)
    )
    if notification is None:
        raise _notification_not_found()
    return notification


def is_delivery_terminal(status: NotificationDeliveryStatus) -> bool:
    """Return whether a delivery reached a terminal state (never re-sent)."""
    return status in (
        NotificationDeliveryStatus.SUCCEEDED,
        NotificationDeliveryStatus.FAILED,
    )


async def mark_delivery_running(
    session: AsyncSession,
    *,
    delivery_id: uuid.UUID,
) -> NotificationDelivery:
    """Transition a delivery to ``running`` at the start of a task attempt.

    Idempotent across retries: an already-``running`` row stays running and
    the attempt counter increments (one per attempt), mirroring the durable
    job's atomic dispatch claim (``jobs_service.claim_dispatch``). A terminal
    delivery is never re-sent: a message that arrives after the delivery
    already finished raises a 409, which the task's terminal check avoids
    before sending.
    """
    delivery = await get_delivery_for_task(session, delivery_id=delivery_id)
    if is_delivery_terminal(delivery.status):
        raise ConflictError(
            code="delivery_in_terminal_state",
            message="A finished delivery cannot be sent again.",
        )
    delivery.status = NotificationDeliveryStatus.RUNNING
    delivery.attempt_count = delivery.attempt_count + 1
    await session.commit()
    await session.refresh(delivery)
    return delivery


async def mark_delivery_succeeded(
    session: AsyncSession,
    *,
    delivery_id: uuid.UUID,
    provider_message_id: str,
) -> NotificationDelivery:
    """Record a successful send on the delivery row.

    Sets ``succeeded``, stores the provider's message id as the delivery
    evidence and stamps ``sent_at`` (blueprint §20 delivery tracking).
    """
    delivery = await get_delivery_for_task(session, delivery_id=delivery_id)
    delivery.status = NotificationDeliveryStatus.SUCCEEDED
    delivery.provider_message_id = provider_message_id
    delivery.sent_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(delivery)
    return delivery


async def mark_delivery_failed(
    session: AsyncSession,
    *,
    delivery_id: uuid.UUID,
    organisation_id: uuid.UUID,
    error_message: str,
) -> NotificationDelivery:
    """Record a failed send on the delivery row and audit it.

    Sets ``failed`` and writes the ``notification.delivery_failed`` audit event
    in the same transaction, with the reason in the metadata (the worker-side
    failure path, acceptance §5.5). Idempotent across re-delivery: a delivery
    already in a terminal state is returned untouched, so a retried message
    cannot double-audit.
    """
    delivery = await get_delivery_for_task(session, delivery_id=delivery_id)
    if is_delivery_terminal(delivery.status):
        return delivery
    delivery.status = NotificationDeliveryStatus.FAILED
    await record_event(
        session,
        organisation_id=organisation_id,
        action=ACTION_NOTIFICATION_DELIVERY_FAILED,
        resource_type="notification",
        resource_id=str(delivery.notification_id),
        metadata={
            "channel": delivery.channel,
            "recipient": delivery.recipient,
            "delivery_id": str(delivery.id),
            "error": error_message,
        },
    )
    await session.commit()
    await session.refresh(delivery)
    return delivery
