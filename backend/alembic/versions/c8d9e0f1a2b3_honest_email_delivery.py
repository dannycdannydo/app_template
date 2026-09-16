"""Add stable and ambiguity-aware notification delivery fields.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_deliveries",
        sa.Column("delivery_identity", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("error_code", sa.String(length=80), nullable=True),
    )
    # Existing rows already have opaque UUID primary keys, which provide a
    # safe, deterministic backfill without inspecting recipient or content.
    op.execute(
        "UPDATE notification_deliveries SET delivery_identity = CAST(id AS VARCHAR) "
        "WHERE delivery_identity IS NULL"
    )
    op.alter_column("notification_deliveries", "delivery_identity", nullable=False)
    op.create_unique_constraint(
        op.f("uq_notification_deliveries_delivery_identity"),
        "notification_deliveries",
        ["delivery_identity"],
    )
    op.drop_constraint(
        op.f("ck_notification_deliveries_delivery_status"),
        "notification_deliveries",
        type_="check",
    )
    op.alter_column(
        "notification_deliveries",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_notification_deliveries_delivery_status"),
        "notification_deliveries",
        "status IN ('queued', 'running', 'succeeded', 'failed', 'attention_required')",
    )


def downgrade() -> None:
    # Preserve the terminal/no-resend property on downgrade.
    op.execute(
        "UPDATE notification_deliveries SET status = 'failed' WHERE status = 'attention_required'"
    )
    op.drop_constraint(
        op.f("ck_notification_deliveries_delivery_status"),
        "notification_deliveries",
        type_="check",
    )
    op.alter_column(
        "notification_deliveries",
        "status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_notification_deliveries_delivery_status"),
        "notification_deliveries",
        "status IN ('queued', 'running', 'succeeded', 'failed')",
    )
    op.drop_constraint(
        op.f("uq_notification_deliveries_delivery_identity"),
        "notification_deliveries",
        type_="unique",
    )
    op.drop_column("notification_deliveries", "error_code")
    op.drop_column("notification_deliveries", "delivery_identity")
