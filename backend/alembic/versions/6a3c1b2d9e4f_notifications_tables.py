"""notifications and notification_deliveries tables

Revision ID: 6a3c1b2d9e4f
Revises: a1b2c3d4e5f6
Create Date: 2026-08-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6a3c1b2d9e4f"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the notifications and notification_deliveries tables.

    Blueprint §20 shape with the §10 conventions: UUIDv7 primary keys,
    timezone-aware UTC timestamps, snake_case naming, and foreign keys named
    ``<entity>_id``. ``notifications`` hangs off exactly one organisation and
    exactly one recipient user (``user_id``), so every query filters on both
    first and a notification that exists for another organisation or another
    recipient is simply not found (404). ``notification_deliveries`` tracks
    one channel delivery per notification with the blueprint §20 lifecycle
    (``queued``/``running``/``succeeded``/``failed``), the provider's message
    id and the attempt count; the ``ON DELETE CASCADE`` on ``notification_id``
    keeps the delivery rows tied to their notification.

    The composite index ``(organisation_id, user_id, created_at)`` serves the
    caller's list ordered newest-first; ``(organisation_id, user_id, read_at)``
    serves the unread-count filter (the trailing ``read_at IS NULL`` predicate
    is a range condition on the index).
    """
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("resource_type", sa.String(length=80), nullable=True),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_notifications_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(
        op.f("ix_notifications_organisation_id"),
        "notifications",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False)
    op.create_index(
        "ix_notifications_organisation_id_user_id_created_at",
        "notifications",
        ["organisation_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_organisation_id_user_id_read_at",
        "notifications",
        ["organisation_id", "user_id", "read_at"],
        unique=False,
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=20), server_default="email", nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_notification_deliveries_delivery_status"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_notification_deliveries_non_negative_attempt_count"),
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_deliveries_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
    )
    op.create_index(
        "ix_notification_deliveries_notification_id",
        "notification_deliveries",
        ["notification_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the notification tables."""
    op.drop_index(
        "ix_notification_deliveries_notification_id", table_name="notification_deliveries"
    )
    op.drop_table("notification_deliveries")
    op.drop_index("ix_notifications_organisation_id_user_id_read_at", table_name="notifications")
    op.drop_index("ix_notifications_organisation_id_user_id_created_at", table_name="notifications")
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_organisation_id"), table_name="notifications")
    op.drop_table("notifications")
