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
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import NotFoundError
from app.email.base import EmailSendError
from app.modules.audit.models import AuditEvent
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatus
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
from app.modules.permissions.models import Permission, Role, RolePermission
from app.modules.users.models import User

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


def _stub_send(**kwargs: Any) -> None:
    """No-op stand-in for an Actor's ``send`` during enqueue-proof tests."""


def _stub_task() -> Any:
    """Return an object shaped like a Dramatiq actor whose send does nothing."""
    return SimpleNamespace(send=_stub_send)


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
            "status",
            "provider_message_id",
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


# --- Test-send flow and delivery lifecycle ---


async def test_send_test_notification_writes_rows_job_and_audit(
    migrated_database: str,
) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    stub_task = _stub_task()
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)

            notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
                delivery_task=stub_task,
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
                session, delivery_id=delivery.id
            )
            assert running.status == NotificationDeliveryStatus.RUNNING
            assert running.attempt_count == 1

            succeeded = await notifications_service.mark_delivery_succeeded(
                session, delivery_id=delivery.id, provider_message_id="fake-1"
            )
            assert succeeded.status == NotificationDeliveryStatus.SUCCEEDED
            assert succeeded.provider_message_id == "fake-1"
            assert succeeded.sent_at is not None
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
                error_message="relay refused the message",
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
    yield factory
    await engine.dispose()


# --- The real task handler (fake provider, real database) ---


async def test_send_notification_email_task_success(
    migrated_database: str, task_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    stub_task = _stub_task()
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
                delivery_task=stub_task,
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
    stub_task = _stub_task()

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
                delivery_task=stub_task,
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
    finally:
        await engine.dispose()


async def test_send_notification_email_task_is_idempotent_on_redelivery(
    migrated_database: str,
    task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A re-delivered message for a succeeded delivery never sends twice."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    stub_task = _stub_task()
    try:
        async with session_factory() as session:
            org, user = await _seed_org_and_user(session)
            _notification, delivery, job = await notifications_service.send_test_notification(
                session,
                organisation_id=org.id,
                user_id=user.id,
                recipient_email=user.email,
                actor_user_id=user.id,
                delivery_task=stub_task,
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
