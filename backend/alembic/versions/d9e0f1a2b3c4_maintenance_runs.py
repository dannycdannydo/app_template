"""Add the durable global maintenance-run ledger.

Additive and non-destructive: one new table, no change to any existing row or
column. Scheduled sweeps previously had no durable execution record (plan P4),
so there is no historical data to backfill — the first coordinator tick after
this migration creates the first run.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: str | Sequence[str] | None = "c8d9e0f1a2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("schedule_key", sa.String(length=120), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("owner_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("taken_over", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "task_type IN ('ai.retention', 'ai.transfer_reconcile')",
            name=op.f("ck_maintenance_runs_maintenance_run_task_type"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_maintenance_runs_maintenance_run_status"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_maintenance_runs_non_negative_attempt_count")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_maintenance_runs")),
        sa.UniqueConstraint("schedule_key", name="uq_maintenance_runs_schedule_key"),
        sa.UniqueConstraint("owner_token", name="uq_maintenance_runs_owner_token"),
    )
    op.create_index(
        "ix_maintenance_runs_status_lease",
        "maintenance_runs",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "ix_maintenance_runs_task_scheduled",
        "maintenance_runs",
        ["task_type", "scheduled_for"],
    )


def downgrade() -> None:
    op.drop_index("ix_maintenance_runs_task_scheduled", table_name="maintenance_runs")
    op.drop_index("ix_maintenance_runs_status_lease", table_name="maintenance_runs")
    op.drop_table("maintenance_runs")
