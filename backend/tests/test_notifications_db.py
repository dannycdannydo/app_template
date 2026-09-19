"""Real-database integration tests for the notifications module (Scope §6.3).

The request-flow tests in ``test_notifications.py`` never execute SQL, so the
table shape, the permission seed, the org+user scoping and the delivery
lifecycle could silently regress at the query and constraint level. These
tests run the real migrations and the real service against a reachable
PostgreSQL (same skip pattern as ``test_jobs_db.py``: migrated to head up
front, reverted to base afterwards), and run the real ``send_notification_email``
task handler against the real database with the in-memory fake email provider
(pinned by ``EMAIL_PROVIDER=fake`` in ``tests/conftest.py``).

Acceptance §5.4/§5.5 are proven here: the tables exist with the blueprint §20
shape; the ``notifications.read``/``notifications.manage`` codes are granted
exactly to owner/administrator/manager (both) and member (read) with viewer
untouched; the list is scoped to the caller's organisation *and* the caller's
user with the ``type`` filter applied at the SQL level; a foreign or
other-user notification is a 404; and the test-send flow writes the
notification, its email delivery, the durable ``notification.email`` job and
the audit row in one transaction, with the task driving the delivery row
queued -> running -> succeeded/failed and recording ``provider_message_id``.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dramatiq
import pytest
from alembic import command
from alembic.config import Config
from dramatiq.brokers.stub import StubBroker
from dramatiq.worker import Worker
from sqlalchemy import inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import NotFoundError
from app.email.base import (
    AcceptanceUnknownEmailSendError,
    EmailSendError,
    TransientEmailSendError,
)
from app.modules.audit.models import AuditEvent
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobAttemptStatus, JobStatus
from app.modules.jobs.queries import job_attempt_history_statement
from app.modules.notifications import service as notifications_service
from app.modules.notifications import tasks as notifications_tasks
from app.modules.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationDeliveryStatus,
)
from app.modules.notifications.queries import (
    unread_notifications_count_statement,
    user_notifications_count_statement,
    user_notifications_statement,
)
from app.modules.organisations.models import Organisation
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus
from app.modules.permissions.models import Permission, Role, RolePermission
from app.modules.users.models import User

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_QUEUE = "test-notifications-db"


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


async def _seed_org_and_user(
    session: AsyncSession, *, email: str = "ada@example.com"
) -> tuple[Organisation, User]:
    organisation = Organisation(name="Notifications Ltd")
    user = User(workos_user_id=f"user_{uuid.uuid4().hex}", email=email, name="Ada Lovelace")
    session.add_all([organisation, user])
    await session.commit()
    return organisation, user


# --- Migration shape (acceptance §5.4) ---


async def test_migration_creates_notification_tables(migrated_database: str) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:

            def _inspect(sync_connection: Any) -> dict[str, Any]:
                inspector = inspect(sync_connection)
                return {
                    "columns": {col["name"] for col in inspector.get_columns("notifications")},
                    "delivery_columns": {
                        col["name"] for col in inspector.get_columns("notification_deliveries")
                    },
                    "indexes": {index["name"] for index in inspector.get_indexes("notifications")},
                }

            tables = await connection.run_sync(_inspect)
        assert {
            "id",
            "organisation_id",
            "user_id",
            "type",
            "title",
            "body",
            "resource_type",
            "resource_id",
            "read_at",
            "created_at",
            "updated_at",
        } <= tables["columns"]

        assert {
            "id",
            "notification_id",
            "channel",
            "recipient",
            "delivery_identity",
            "status",
            "provider_message_id",
            "error_code",
            "attempt_count",
            "sent_at",
            "created_at",
            "updated_at",
        } <= tables["delivery_columns"]

        assert "ix_notifications_organisation_id_user_id_created_at" in tables["indexes"]
        assert "ix_notifications_organisation_id_user_id_read_at" in tables["indexes"]
    finally:
        await engine.dispose()


# --- Permission seed (acceptance §5.4) ---


async def test_notification_permission_codes_and_role_grants(
    migrated_database: str,
) -> None:
    """owner/administrator/manager hold both codes, member read, viewer none."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            expected_grants: dict[str, set[str]] = {
                "owner": {"notifications.read", "notifications.manage"},
                "administrator": {"notifications.read", "notifications.manage"},
                "manager": {"notifications.read", "notifications.manage"},
                "member": {"notifications.read"},
                "viewer": set(),
            }
            for role_code, expected in expected_grants.items():
                rows = await session.scalars(
                    select(Permission.code)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .join(Role, Role.id == RolePermission.role_id)
                    .where(Role.code == role_code)
                )
                assert set(rows.all()) & {"notifications.read", "notifications.manage"} == expected
    finally:
        await engine.dispose()


# --- Service scoping, filtering and mark-read (acceptance §5.5) ---


async def test_list_is_scoped_to_caller_and_applies_type_filter(
    migrated_database: str,
) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            other_org, other_user = await _seed_org_and_user(session, email="other@example.com")

            # Two notifications for the caller (one read), one for the other
            # user in the same org, one in the other org.
            unread = Notification(
                organisation_id=org.id,
                user_id=user.id,
                type="file.ready",
                title="File ready",
                body="Your file is ready.",
            )
            read = Notification(
                organisation_id=org.id,
                user_id=user.id,
                type="file.failed",
                title="File failed",
                body="Your file failed.",
                read_at=datetime.now(UTC),
            )
            other_user_notification = Notification(
                organisation_id=org.id,
                user_id=other_user.id,
                type="file.ready",
                title="Other",
                body="Other user's notification.",
            )
            foreign_org_notification = Notification(
                organisation_id=other_org.id,
                user_id=user.id,
                type="file.ready",
                title="Foreign",
                body="Other organisation's notification.",
            )
            session.add_all([unread, read, other_user_notification, foreign_org_notification])
            await session.commit()

            # The list returns only the caller's own notifications.
            rows = await session.scalars(
                user_notifications_statement(org.id, user.id).order_by(
                    Notification.created_at.desc(), Notification.id.desc()
                )
            )
            listed = list(rows.all())
            assert {n.id for n in listed} == {unread.id, read.id}

            total = await session.scalar(user_notifications_count_statement(org.id, user.id))
            assert total == 2

            # Type filter is applied at the SQL level.
            filtered = await session.scalars(
                user_notifications_statement(org.id, user.id, type="file.ready")
            )
            assert {n.id for n in filtered.all()} == {unread.id}

            # Unread count ignores read notifications and other users.
            unread_count = await session.scalar(
                unread_notifications_count_statement(org.id, user.id)
            )
            assert unread_count == 1
    finally:
        await engine.dispose()


async def test_mark_read_is_idempotent_and_isolated(migrated_database: str) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _, other_user = await _seed_org_and_user(session, email="other@example.com")
            notification = Notification(
                organisation_id=org.id,
                user_id=user.id,
                type="file.ready",
                title="File ready",
                body="Your file is ready.",
            )
            session.add(notification)
            await session.commit()

            marked = await notifications_service.mark_read(
                session,
                organisation_id=org.id,
                user_id=user.id,
                notification_id=notification.id,
            )
            assert marked.read_at is not None
            first_read_at = marked.read_at

            # Idempotent: a second mark keeps the original read_at.
            again = await notifications_service.mark_read(
                session,
                organisation_id=org.id,
                user_id=user.id,
                notification_id=notification.id,
            )
            assert again.read_at == first_read_at

            # Isolation: another user's notification is a 404 for the caller.
            other_notification = Notification(
                organisation_id=org.id,
                user_id=other_user.id,
                type="file.ready",
                title="Other",
                body="Other user's notification.",
            )
            session.add(other_notification)
            await session.commit()
            with pytest.raises(NotFoundError):
                await notifications_service.mark_read(
                    session,
                    organisation_id=org.id,
                    user_id=user.id,
                    notification_id=other_notification.id,
                )
            # The other user's row is untouched.
            fresh = await session.get(Notification, other_notification.id)
            assert fresh is not None and fresh.read_at is None
    finally:
        await engine.dispose()


async def test_mark_all_read_is_idempotent_and_scoped(migrated_database: str) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _, other_user = await _seed_org_and_user(session, email="bulk-other@example.com")
            own_unread = Notification(
                organisation_id=org.id,
                user_id=user.id,
                type="file.ready",
                title="Own unread",
                body="Read me.",
            )
            other_unread = Notification(
                organisation_id=org.id,
                user_id=other_user.id,
                type="file.ready",
                title="Other unread",
                body="Do not read me.",
            )
            session.add_all([own_unread, other_unread])
            await session.commit()

            marked_count = await notifications_service.mark_all_read(
                session,
                organisation_id=org.id,
                user_id=user.id,
            )
            assert marked_count == 1

            own_fresh = await session.get(Notification, own_unread.id)
            other_fresh = await session.get(Notification, other_unread.id)
            # The bulk statement bypasses the ORM unit of work, so the
            # identity-map instances keep their stale ``read_at``; refresh
            # re-reads the committed rows asynchronously (expiring attributes
            # would trigger a synchronous lazy load, which async sessions
            # forbid).
            await session.refresh(own_fresh)
            await session.refresh(other_fresh)
            assert own_fresh is not None and own_fresh.read_at is not None
            assert other_fresh is not None and other_fresh.read_at is None

            assert (
                await notifications_service.mark_all_read(
                    session,
                    organisation_id=org.id,
                    user_id=user.id,
                )
                == 0
            )
    finally:
        await engine.dispose()


# --- Test-send flow and delivery lifecycle ---


async def test_send_test_notification_writes_rows_job_and_audit(
    migrated_database: str,
) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)

            notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )

            assert notification.organisation_id == org.id
            assert notification.user_id == user.id
            assert notification.type == "notification.test_sent"
            assert delivery.notification_id == notification.id
            assert delivery.channel == "email"
            assert delivery.recipient == user.email
            assert delivery.status == NotificationDeliveryStatus.QUEUED
            assert job.job_type == "notification.email"
            assert job.input_reference == str(delivery.id)
            assert job.status == JobStatus.QUEUED

            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "notification.test_sent",
                    AuditEvent.resource_id == str(notification.id),
                )
            )
            assert audit is not None
            assert audit.actor_user_id == user.id
            assert audit.organisation_id == org.id
    finally:
        await engine.dispose()


async def test_delivery_lifecycle_helpers(migrated_database: str) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            notification = Notification(
                organisation_id=org.id,
                user_id=user.id,
                type="file.ready",
                title="File ready",
                body="Your file is ready.",
            )
            session.add(notification)
            await session.commit()
            delivery = NotificationDelivery(
                notification_id=notification.id,
                channel="email",
                recipient=user.email,
                status=NotificationDeliveryStatus.QUEUED,
            )
            session.add(delivery)
            await session.commit()

            running = await notifications_service.mark_delivery_running(
                session,
                delivery_id=delivery.id,
                organisation_id=org.id,
                user_id=user.id,
            )
            assert running.status == NotificationDeliveryStatus.RUNNING
            assert running.attempt_count == 1

            succeeded = await notifications_service.mark_delivery_succeeded(
                session,
                delivery_id=delivery.id,
                provider_message_id="fake-1",
                organisation_id=org.id,
                user_id=user.id,
            )
            assert succeeded.status == NotificationDeliveryStatus.SUCCEEDED
            assert succeeded.provider_message_id == "fake-1"
            assert succeeded.sent_at is not None
    finally:
        await engine.dispose()


async def test_create_file_notification_is_idempotent_on_retry(
    migrated_database: str,
) -> None:
    """Scope §6.4: a retried producer never double-notifies or double-sends.

    ``create_file_notification`` (the worker-side producer the ``process_file``
    task calls) deduplicates on (organisation, user, type, file): calling it a
    second time for the same outcome returns the existing notification without
    creating a second delivery row or a second ``notification.email`` job, so a
    retried or re-delivered message cannot produce a double notification or a
    double email send (acceptance §5.5 idempotency rule).
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            first = await notifications_service.create_file_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                notification_type="file.ready",
                title=notifications_service.FILE_READY_TITLE,
                body=notifications_service.FILE_READY_BODY.format(filename="report.pdf"),
                resource_id="file-1",
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            assert first.resource_type == "file"
            assert first.resource_id == "file-1"

            second = await notifications_service.create_file_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                notification_type="file.ready",
                title=notifications_service.FILE_READY_TITLE,
                body=notifications_service.FILE_READY_BODY.format(filename="report.pdf"),
                resource_id="file-1",
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            assert second.id == first.id  # never double-notified

            deliveries = (
                await session.scalars(
                    select(NotificationDelivery).where(
                        NotificationDelivery.notification_id == first.id
                    )
                )
            ).all()
            assert len(deliveries) == 1  # one delivery, never two

            jobs = (
                await session.scalars(
                    select(Job).where(
                        Job.job_type == "notification.email",
                        Job.input_reference == str(deliveries[0].id),
                    )
                )
            ).all()
            assert len(jobs) == 1  # one email job, never two

        # A different file's outcome is a distinct notification (no over-match).
        async with session_factory() as session:
            other = await notifications_service.create_file_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                notification_type="file.ready",
                title=notifications_service.FILE_READY_TITLE,
                body=notifications_service.FILE_READY_BODY.format(filename="other.pdf"),
                resource_id="file-2",
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            assert other.id != first.id
    finally:
        await engine.dispose()


async def test_notification_job_requires_actor_to_equal_recipient(
    migrated_database: str,
) -> None:
    """A `notification.email` job cannot schedule on behalf of another user.

    The worker derives the user-private RLS context from the durable
    ``jobs.created_by_user_id``, so the producers enforce that the job actor is
    the recipient until a dedicated durable recipient identity exists.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            actor = User(
                workos_user_id=f"user_{uuid.uuid4().hex}",
                email="actor@example.com",
                name="Actor",
            )
            session.add(actor)
            await session.commit()
            with pytest.raises(ValueError, match="job actor must equal"):
                await notifications_service.send_test_notification(
                    session,
                    organisation_id=org.id,
                    user_id=user.id,
                    recipient_email=user.email,
                    actor_user_id=actor.id,
                )
    finally:
        await engine.dispose()


async def test_mark_delivery_failed_writes_audit(migrated_database: str) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            notification = Notification(
                organisation_id=org.id,
                user_id=user.id,
                type="file.ready",
                title="File ready",
                body="Your file is ready.",
            )
            session.add(notification)
            await session.commit()
            delivery = NotificationDelivery(
                notification_id=notification.id,
                channel="email",
                recipient=user.email,
                status=NotificationDeliveryStatus.RUNNING,
            )
            session.add(delivery)
            await session.commit()

            failed = await notifications_service.mark_delivery_failed(
                session,
                delivery_id=delivery.id,
                organisation_id=org.id,
                user_id=user.id,
                error_code=notifications_service.DELIVERY_ERROR_PERMANENTLY_REJECTED,
            )
            assert failed.status == NotificationDeliveryStatus.FAILED

            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "notification.delivery_failed",
                    AuditEvent.resource_id == str(notification.id),
                )
            )
            assert audit is not None
            assert audit.organisation_id == org.id
    finally:
        await engine.dispose()


@pytest.fixture
async def task_session_factory(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Point the task handler's session factory at a per-test NullPool engine.

    The task opens its own session through ``async_session_factory``; the
    module-level singleton's engine pools connections across event loops,
    which pytest-asyncio tears down between tests. A NullPool engine created on
    this test's loop keeps every connection on that loop (the same isolation
    the handler runs under in production, where the worker owns one loop).
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(notifications_tasks, "async_session_factory", factory)
    # The task drives its domain work through the shared execution wrapper
    # (plan P2), which opens its own sessions through the wrapper's module
    # factory; point it at the same NullPool engine so direct handler calls
    # stay on this test's event loop.
    from app.modules.jobs import execution as jobs_execution

    monkeypatch.setattr(jobs_execution, "async_session_factory", factory)
    yield factory
    await engine.dispose()


# --- The real task handler (fake provider, real database) ---


async def test_send_notification_email_task_success(
    migrated_database: str, task_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job_id = job.id
            delivery_id = delivery.id

        # The task opens its own session (the app's session factory bound to
        # the same DATABASE_URL) and drives the durable row to completion.
        await notifications_tasks.send_notification_email(str(job_id))

        async with session_factory() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.SUCCEEDED
            # The fake's counter is process-wide, so the exact id depends on
            # earlier tests; the shape is deterministic.
            assert delivery.provider_message_id is not None
            assert delivery.provider_message_id.startswith("fake-")
            assert delivery.sent_at is not None
            assert delivery.attempt_count == 1

            job = await session.get(Job, job_id)
            assert job is not None
            assert job.status == JobStatus.SUCCEEDED
            assert job.result_reference == delivery.provider_message_id
    finally:
        await engine.dispose()


async def test_send_notification_email_task_failure_is_permanent(
    migrated_database: str,
    task_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    class _FailingProvider:
        async def send_email(self, **kwargs: Any) -> Any:
            raise EmailSendError("relay refused the message")

    monkeypatch.setattr(notifications_tasks, "get_email_provider", lambda: _FailingProvider())
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job_id = job.id
            delivery_id = delivery.id
            notification_id = _notification.id

        with pytest.raises(jobs_service.JobPermanentError):
            await notifications_tasks.send_notification_email(str(job_id))

        async with session_factory() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.FAILED
            assert delivery.error_code == "unclassified_provider_error"

            job = await session.get(Job, job_id)
            assert job is not None
            assert job.status == JobStatus.FAILED
            assert job.error_code == "email_delivery_failed"

            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "notification.delivery_failed",
                    AuditEvent.resource_id == str(notification_id),
                )
            )
            assert audit is not None
            assert audit.organisation_id == org.id
            assert audit.event_metadata is not None
            assert audit.event_metadata.get("channel") == "email"
            assert audit.event_metadata.get("error_code") == "unclassified_provider_error"
            assert "error" not in audit.event_metadata
            assert "recipient" not in audit.event_metadata
            assert audit.event_metadata.get("delivery_id") is not None
    finally:
        await engine.dispose()


async def test_send_notification_email_task_transient_failure_requeues_delivery(
    migrated_database: str,
    task_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P4: retryable SMTP failures leave both delivery and job retryable."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    class _TransientProvider:
        async def send_email(self, **kwargs: Any) -> Any:
            raise TransientEmailSendError("temporary relay outage")

    monkeypatch.setattr(notifications_tasks, "get_email_provider", lambda: _TransientProvider())
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job_id = job.id
            delivery_id = delivery.id

        await notifications_tasks.send_notification_email(str(job_id))

        async with session_factory() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            job = await session.get(Job, job_id)
            assert delivery is not None and delivery.status == NotificationDeliveryStatus.QUEUED
            assert delivery.attempt_count == 1
            assert job is not None and job.status == JobStatus.QUEUED
            assert job.error_code is None
            assert job.dispatch_id is not None
            retry_event = await session.get(OutboxEvent, job.dispatch_id)
            assert retry_event is not None
            assert retry_event.status is OutboxEventStatus.PENDING
    finally:
        await engine.dispose()


async def test_safe_retry_reuses_persisted_delivery_identity(
    migrated_database: str,
    task_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A definitely-unsent retry presents the exact same provider identity."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    identities: list[str] = []

    class _RetryProvider:
        async def send_email(self, **kwargs: Any) -> Any:
            identities.append(str(kwargs["delivery_identity"]))
            if len(identities) == 1:
                raise TransientEmailSendError("not submitted")
            return type("Result", (), {"provider_message_id": "provider-stable"})()

    monkeypatch.setattr(notifications_tasks, "get_email_provider", lambda: _RetryProvider())
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            delivery_identity = delivery.delivery_identity
            job_id = job.id

        await notifications_tasks.send_notification_email(str(job_id))
        await notifications_tasks.send_notification_email(str(job_id))

        assert identities == [delivery_identity, delivery_identity]
        async with session_factory() as session:
            row = await session.get(NotificationDelivery, delivery.id)
            assert row is not None
            assert row.status is NotificationDeliveryStatus.SUCCEEDED
            assert row.provider_message_id == "provider-stable"
    finally:
        await engine.dispose()


async def test_acceptance_unknown_is_terminal_and_not_resent(
    migrated_database: str,
    task_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambiguous adapter result settles delivery, attempt and job safely."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    calls = 0

    class _AmbiguousProvider:
        async def send_email(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            raise AcceptanceUnknownEmailSendError("connection lost after DATA")

    monkeypatch.setattr(notifications_tasks, "get_email_provider", lambda: _AmbiguousProvider())
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job_id = job.id

        with pytest.raises(jobs_service.JobPermanentError):
            await notifications_tasks.send_notification_email(str(job_id))
        await notifications_tasks.send_notification_email(str(job_id))
        assert calls == 1

        async with session_factory() as session:
            delivery_row = await session.get(NotificationDelivery, delivery.id)
            job_row = await session.get(Job, job_id)
            assert delivery_row is not None
            assert delivery_row.status is NotificationDeliveryStatus.ATTENTION_REQUIRED
            assert delivery_row.error_code == "acceptance_unknown"
            assert job_row is not None
            assert job_row.status is JobStatus.FAILED
            assert job_row.error_code == "email_delivery_acceptance_unknown"
            audit = await session.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "notification.delivery_attention_required",
                    AuditEvent.resource_id == str(notification.id),
                )
            )
            assert audit is not None
            assert audit.event_metadata["error_code"] == "acceptance_unknown"
            assert "recipient" not in audit.event_metadata
    finally:
        await engine.dispose()


async def test_provider_accepted_then_worker_crashed_is_not_resent(
    migrated_database: str,
    task_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A takeover of an in-flight delivery requires attention, not resend."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    accepted: list[str] = []

    class _WorkerCrash(BaseException):
        pass

    class _AcceptedThenCrashProvider:
        async def send_email(self, **kwargs: Any) -> Any:
            accepted.append(str(kwargs["delivery_identity"]))
            raise _WorkerCrash()

    monkeypatch.setattr(
        notifications_tasks, "get_email_provider", lambda: _AcceptedThenCrashProvider()
    )
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job_id = job.id

        with pytest.raises(_WorkerCrash):
            await notifications_tasks.send_notification_email(str(job_id))

        async with session_factory() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

        with pytest.raises(jobs_service.JobPermanentError):
            await notifications_tasks.send_notification_email(str(job_id))
        assert accepted == [delivery.delivery_identity]

        async with session_factory() as session:
            delivery_row = await session.get(NotificationDelivery, delivery.id)
            job_row = await session.get(Job, job_id)
            assert delivery_row is not None
            assert delivery_row.status is NotificationDeliveryStatus.ATTENTION_REQUIRED
            assert job_row is not None
            assert job_row.status is JobStatus.FAILED
    finally:
        await engine.dispose()


async def test_send_notification_email_task_is_idempotent_on_redelivery(
    migrated_database: str,
    task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A re-delivered message for a succeeded delivery never sends twice."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job_id = job.id
            delivery_id = delivery.id

        await notifications_tasks.send_notification_email(str(job_id))
        # Simulate a re-delivered message: the delivery is already succeeded.
        await notifications_tasks.send_notification_email(str(job_id))

        async with session_factory() as session:
            delivery = await session.get(NotificationDelivery, delivery_id)
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.SUCCEEDED
            assert delivery.attempt_count == 1  # never sent twice
            # The provider's message id is recorded once (the fake's counter
            # is process-wide, so the exact id depends on earlier tests).
            assert delivery.provider_message_id is not None
            assert delivery.provider_message_id.startswith("fake-")
    finally:
        await engine.dispose()


async def test_send_notification_email_rejects_cross_org_durable_context(
    migrated_database: str,
    task_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job cannot use a notification whose tenant differs from the job."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    provider_called = False

    class _ProviderMustNotRun:
        async def send_email(self, **kwargs: Any) -> Any:
            nonlocal provider_called
            provider_called = True
            raise AssertionError("provider must not run for mismatched tenant context")

    monkeypatch.setattr(notifications_tasks, "get_email_provider", lambda: _ProviderMustNotRun())
    try:
        async with session_factory() as session:
            job_org, user = await _seed_org_and_user(session)
            foreign_org = Organisation(name="Foreign Notifications Ltd")
            session.add(foreign_org)
            await session.commit()
            notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=job_org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            notification.organisation_id = foreign_org.id
            await session.commit()
            job_id = job.id
            delivery_id = delivery.id

        with pytest.raises(jobs_service.JobPermanentError):
            await notifications_tasks.send_notification_email(str(job_id))

        assert provider_called is False
        async with session_factory() as session:
            job = await session.get(Job, job_id)
            delivery = await session.get(NotificationDelivery, delivery_id)
            assert job is not None
            assert job.status == JobStatus.FAILED
            assert job.error_code == notifications_tasks.ERROR_CODE_INVALID_JOB_CONTEXT
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.QUEUED
            assert delivery.attempt_count == 0
    finally:
        await engine.dispose()


async def test_send_notification_email_rejects_wrong_job_type(
    migrated_database: str,
    task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The email actor cannot execute a durable job owned by another task.

    The wrong-type settlement runs under the claimed owner (plan P2), so it is
    accepted even once every job row carries a dispatch id (P3): a row
    pre-populated with a dispatch id is claimed, then failed with the
    invalid-context error instead of bouncing off the owner check as
    ``StaleDispatchError``.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            job.job_type = "file.processing"
            # P3 populates the dispatch id on every durable job at creation.
            job.dispatch_id = uuid.uuid4()
            await session.commit()
            job_id = job.id
            delivery_id = delivery.id

        with pytest.raises(jobs_service.JobPermanentError):
            await notifications_tasks.send_notification_email(str(job_id))

        async with session_factory() as session:
            job = await session.get(Job, job_id)
            delivery = await session.get(NotificationDelivery, delivery_id)
            assert job is not None
            assert job.status == JobStatus.FAILED
            assert job.error_code == notifications_tasks.ERROR_CODE_INVALID_JOB_CONTEXT
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.QUEUED
    finally:
        await engine.dispose()


async def test_stale_delivery_worker_cannot_mutate_after_takeover(
    migrated_database: str,
) -> None:
    """AC5: a superseded email worker is fenced out of delivery state.

    The stale attempt is taken over after its lease expires; every delivery
    transition it then attempts with its captured owner token is rejected, so
    it can never record a success or failure over the newer owner.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            claim = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert claim.owner_token is not None
            stale_owner = jobs_service.JobOwnership(
                job_id=job.id,
                owner_token=claim.owner_token,
                organisation_id=org.id,
            )
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
            takeover = await jobs_service.claim_dispatch(session, job_id=job.id)
            assert takeover.taken_over is True
            delivery_id = delivery.id

            with pytest.raises(jobs_service.StaleDispatchError):
                await notifications_service.mark_delivery_succeeded(
                    session,
                    delivery_id=delivery_id,
                    provider_message_id="stale-provider-id",
                    organisation_id=org.id,
                    user_id=user.id,
                    ownership=stale_owner,
                )
            with pytest.raises(jobs_service.StaleDispatchError):
                await notifications_service.mark_delivery_failed(
                    session,
                    delivery_id=delivery_id,
                    organisation_id=org.id,
                    user_id=user.id,
                    error_code=notifications_service.DELIVERY_ERROR_PERMANENTLY_REJECTED,
                    ownership=stale_owner,
                )
            with pytest.raises(jobs_service.StaleDispatchError):
                await notifications_service.return_delivery_to_queue(
                    session,
                    delivery_id=delivery_id,
                    organisation_id=org.id,
                    user_id=user.id,
                    ownership=stale_owner,
                )

            row = await session.get(NotificationDelivery, delivery_id)
            assert row is not None
            assert row.status == NotificationDeliveryStatus.QUEUED
            assert row.provider_message_id is None
    finally:
        await engine.dispose()


# --- P2: paused real-worker fencing across a cross-session takeover ----------


@pytest.fixture
async def broker_and_worker() -> AsyncIterator[tuple[StubBroker, Worker, Any]]:
    """A StubBroker + in-process Worker running the real email handler.

    The actor is re-declared bound to this test's broker (``Actor.send()``
    enqueues on the actor's own broker) and the middleware stack is the same
    factory the worker process uses, so the async task runs on the AsyncIO
    event-loop thread exactly as in production.
    """
    from app.broker import worker_middleware

    broker = StubBroker(middleware=worker_middleware())
    dramatiq.set_broker(broker)
    email_task = dramatiq.actor(queue_name=_QUEUE, **jobs_service.retry_policy())(
        notifications_tasks.send_notification_email
    )
    worker = Worker(broker, worker_timeout=100, worker_threads=2)
    worker.start()
    yield broker, worker, email_task
    worker.stop()
    broker.flush_all()
    # Dispose the process-wide pool so no loop-bound connection outlives this
    # test's worker (same reason as ``test_files_jobs.py``).
    from app.db.session import engine

    await engine.dispose()


async def _wait_for_event(event: threading.Event, *, timeout: float = 20.0) -> None:
    """Wait until the worker thread signals it reached the test block point."""
    deadline = time.monotonic() + timeout
    while not event.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert event.is_set(), "the worker never reached the test block point"


class _BlockedProviderResult:
    """The minimal provider result the handler records on success."""

    def __init__(self, provider_message_id: str) -> None:
        self.provider_message_id = provider_message_id


async def test_paused_delivery_worker_cannot_record_outcome_after_takeover(
    migrated_database: str,
    broker_and_worker: tuple[StubBroker, Worker, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: a real email worker paused across lease expiry is fenced on resume.

    The delivery actor runs on the in-process worker, dispatches to a
    test-controlled provider that blocks inside ``send_email``, and is held
    there while a separate session expires its lease and takes the dispatch
    over. When the handler resumes, the provider acceptance can no longer be
    recorded: ``mark_delivery_succeeded`` re-locks the job, sees the rotated
    owner token and raises :class:`StaleDispatchError`, leaving the delivery
    ``running`` and the job owned by the new attempt.
    """
    broker, _worker, email_task = broker_and_worker
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    reached = threading.Event()
    release = threading.Event()

    class _BlockingProvider:
        async def send_email(self, **kwargs: Any) -> Any:
            reached.set()
            if not release.wait(timeout=30):
                raise AssertionError("the test never released the paused email worker")
            return _BlockedProviderResult(provider_message_id="blocked-provider-id")

    monkeypatch.setattr(notifications_tasks, "get_email_provider", lambda: _BlockingProvider())

    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
            )
            delivery_id = delivery.id
            job_id = job.id

        email_task.send(job_id=str(job_id))  # the coordinator publishes
        await _wait_for_event(reached)

        # The worker is paused inside the provider call. Expire its lease and
        # let a different session take the dispatch over.
        async with session_factory() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        async with session_factory() as session:
            takeover = await jobs_service.claim_dispatch(session, job_id=job_id)
            assert takeover.taken_over is True
            assert takeover.owner_token is not None
            new_owner = takeover.owner_token

        release.set()
        broker.join(_QUEUE, timeout=10000)

        async with session_factory() as session:
            delivery_row = await session.get(NotificationDelivery, delivery_id)
            assert delivery_row is not None
            assert delivery_row.status == NotificationDeliveryStatus.RUNNING
            assert delivery_row.provider_message_id is None
            job_row = await session.get(Job, job_id)
            assert job_row is not None
            assert job_row.status == JobStatus.RUNNING
            assert job_row.owner_token == new_owner
            attempts = list((await session.scalars(job_attempt_history_statement(job_id))).all())
            assert [attempt.status for attempt in attempts] == [
                JobAttemptStatus.ABANDONED,
                JobAttemptStatus.RUNNING,
            ]
    finally:
        await engine.dispose()
