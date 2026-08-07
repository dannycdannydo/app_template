"""durable job records table

Revision ID: a1b2c3d4e5f6
Revises: e7f3d9c2b5a1
Create Date: 2026-08-07 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "e7f3d9c2b5a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the jobs table, the durable record of every background job.

    Blueprint §18 shape: every row hangs off exactly one organisation through
    the ``organisation_id`` foreign key (``ON DELETE CASCADE`` keeps the
    tenant boundary clean if an organisation is ever removed) and the requester
    through ``created_by_user_id`` (``ON DELETE SET NULL``, matching audit
    rows). ``status`` is a plain varchar with a check constraint covering the
    blueprint §18 statuses; ``progress`` and ``attempt_count`` carry range
    constraints so the worker cannot persist an impossible value. There is no
    ``updated_at`` column — the lifecycle timing lives in ``started_at`` /
    ``completed_at`` (blueprint §18 shape), exactly like the append-only
    audit table. The composite index ``(organisation_id, created_at)`` serves
    the org-scoped job list ordered newest-first.
    """
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_reference", sa.String(length=255), nullable=False),
        sa.Column("result_reference", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_jobs_job_status"),
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name=op.f("ck_jobs_job_progress_range")),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_jobs_non_negative_attempt_count")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_jobs_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_jobs_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_organisation_id"), "jobs", ["organisation_id"], unique=False)
    op.create_index(
        "ix_jobs_organisation_id_created_at",
        "jobs",
        ["organisation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_jobs_created_by_user_id"), "jobs", ["created_by_user_id"], unique=False
    )


def downgrade() -> None:
    """Drop the jobs table."""
    op.drop_index(op.f("ix_jobs_created_by_user_id"), table_name="jobs")
    op.drop_index("ix_jobs_organisation_id_created_at", table_name="jobs")
    op.drop_index(op.f("ix_jobs_organisation_id"), table_name="jobs")
    op.drop_table("jobs")
