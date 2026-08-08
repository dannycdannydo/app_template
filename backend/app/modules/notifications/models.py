"""Notification ORM models (Scope §6.3, blueprint §20, §10).

``notifications`` is the in-app notification record: it hangs off exactly one
organisation and exactly one recipient user (``user_id``), so the module's
queries filter on both first and a notification that exists for another
organisation or another user is simply not found (404), never visible. The
blueprint §20 shape is kept exactly: ``type`` (the dotted event name, e.g.
``file.ready`` in Scope §6.4), ``title``/``body``, optional
``resource_type``/``resource_id`` linking the notification back to the subject
entity, and ``read_at`` for the read lifecycle. There are deliberately no ORM
relationships: loading is deliberate (BP §7) and the database-level
``ON DELETE CASCADE`` keeps the tenant boundary clean.

``notification_deliveries`` tracks one channel delivery per notification
(starting with ``email``, blueprint §20). The row is the durable record the
worker task operates on: it moves ``queued -> running -> succeeded/failed``,
records the provider's message id and the attempt count, and is idempotent by
construction — a terminal delivery is never re-sent (the same rule the durable
jobs table applies to terminal jobs, Scope §6.4).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7


class NotificationDeliveryStatus(enum.StrEnum):
    """Lifecycle state of one delivery attempt (blueprint §20 statuses)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _delivery_status_values(enum_class: type[NotificationDeliveryStatus]) -> list[str]:
    """Return the values a status column stores, not the enum names."""
    return [member.value for member in enum_class]


class Notification(Base, TimestampMixin):
    """One in-app notification for one recipient inside one organisation.

    The blueprint §20 shape: an org-scoped, recipient-scoped row carrying the
    event ``type``, the display text, and the optional resource link. The two
    composite indexes serve the module's hot paths — the org+user list ordered
    newest-first and the org+user unread filter — while the single-column
    indexes declared on the foreign keys keep join-back and admin lookups
    cheap.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index(
            "ix_notifications_organisation_id_user_id_created_at",
            "organisation_id",
            "user_id",
            "created_at",
        ),
        Index(
            "ix_notifications_organisation_id_user_id_read_at",
            "organisation_id",
            "user_id",
            "read_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The recipient user. The module scopes every query on this column, so the
    # "own notifications only" rule (acceptance §5.5) is enforced at the query
    # level, exactly like ``organisation_id`` enforces the tenant boundary.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The event type is the dotted event name (``file.ready``, ``file.failed``,
    # ``notification.test_sent``), a plain string not an enum: new notification
    # producers are expected in later releases (rule of three), so the
    # catalogue is open-ended.
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Optional link to the subject entity (e.g. resource_type ``file`` with the
    # file id as ``resource_id``). Deliberately not a foreign key: a
    # notification may reference a file, a record, an import batch or an
    # organisation in future releases, so the reference stays provider-neutral.
    resource_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationDelivery(Base, TimestampMixin):
    """One durable delivery attempt for a notification.

    The blueprint §20 delivery-tracking shape: the channel (``email`` today),
    the recipient address, the lifecycle status, the provider's message id and
    the attempt count. ``sent_at`` records when the provider accepted the
    message; ``updated_at`` (from ``TimestampMixin``) records the last status
    transition. Terminal statuses (``succeeded``/``failed``) are never re-sent:
    the worker task checks the status before sending (Scope §6.4 idempotency
    rule), so a re-delivered message cannot double-send.
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="delivery_status",
        ),
        CheckConstraint("attempt_count >= 0", name="non_negative_attempt_count"),
        Index("ix_notification_deliveries_notification_id", "notification_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[NotificationDeliveryStatus] = mapped_column(
        Enum(
            NotificationDeliveryStatus,
            name="delivery_status",
            native_enum=False,
            length=16,
            # Persist the enum values ("queued", ...) so rows match the check
            # constraint and server default; SQLAlchemy defaults to names
            # ("QUEUED") for Python enums, which the constraint rejects.
            values_callable=_delivery_status_values,
        ),
        nullable=False,
        default=NotificationDeliveryStatus.QUEUED,
        server_default=NotificationDeliveryStatus.QUEUED.value,
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
