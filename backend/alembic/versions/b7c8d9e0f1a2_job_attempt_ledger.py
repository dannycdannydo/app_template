"""Add the internal durable job-attempt ledger.

Revision ID: b7c8d9e0f1a2
Revises: e1f2a3b4c5d6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_id", sa.Uuid(), nullable=False),
        sa.Column("owner_token", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("taken_over", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.CheckConstraint(
            "attempt_number > 0", name=op.f("ck_job_attempts_positive_attempt_number")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'retry_scheduled', 'succeeded', 'failed', 'exhausted', 'abandoned')",
            name=op.f("ck_job_attempts_job_attempt_status"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], ondelete="RESTRICT", name=op.f("fk_job_attempts_job_id_jobs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_attempts")),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_job_number"),
        sa.UniqueConstraint("owner_token", name="uq_job_attempts_owner_token"),
    )
    op.create_index("ix_job_attempts_job_history", "job_attempts", ["job_id", "started_at"])
    op.create_index("ix_job_attempts_status_lease", "job_attempts", ["status", "lease_expires_at"])
    op.create_index(
        "uq_job_attempts_one_running_per_job",
        "job_attempts",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_job_attempts_one_running_per_job", table_name="job_attempts")
    op.drop_index("ix_job_attempts_status_lease", table_name="job_attempts")
    op.drop_index("ix_job_attempts_job_history", table_name="job_attempts")
    op.drop_table("job_attempts")
