"""transactional outbox and durable delivery columns

Revision ID: a5b6c7d8e9f0
Revises: d5e6f7a8b9c0
Create Date: 2026-08-14 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a5b6c7d8e9f0"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``outbox_events`` and add the job delivery-ownership columns.

    Blueprint §19 outbox shape (plan P1): the transactional outbox in front of
    the Redis broker. ``organisation_id`` is nullable so global maintenance
    events carry no tenant context; every durable job event copies the job's
    validated organisation id. ``deduplication_key`` is unique so a duplicate
    scheduling request or schedule tick fails instead of double-publishing.
    ``payload`` is JSONB bounded by a check constraint so producers cannot
    persist an unbounded blob. ``status`` is a plain varchar with a check
    constraint covering pending/publishing/published/dead. The four composite
    indexes serve the coordinator's due claims, aggregate-history (cooldown),
    stale-claim recovery and published-retention (30-day cleanup) queries.

    ``jobs`` gains two nullable internal columns — ``dispatch_id`` (the outbox
    event id whose publication requested the current dispatch) and
    ``execution_lease_expires_at`` (when a worker's ownership of the attempt
    expires) — plus the indexes that support ownership settlement by dispatch
    id and queued-job reconciliation by status. No API schema references them.
    """
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("event_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("aggregate_type", sa.String(length=80), nullable=True),
        sa.Column("aggregate_id", sa.Uuid(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("deduplication_key", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'dead')",
            name=op.f("ck_outbox_events_outbox_event_status"),
        ),
        sa.CheckConstraint(
            "event_version >= 1", name=op.f("ck_outbox_events_positive_event_version")
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_outbox_events_non_negative_attempt_count")
        ),
        sa.CheckConstraint(
            "char_length(payload::text) <= 16384",
            name=op.f("ck_outbox_events_bounded_payload"),
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_outbox_events_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
        sa.UniqueConstraint("deduplication_key", name=op.f("uq_outbox_events_deduplication_key")),
    )
    op.create_index(
        op.f("ix_outbox_events_organisation_id"), "outbox_events", ["organisation_id"], unique=False
    )
    op.create_index(
        "ix_outbox_events_due_claims", "outbox_events", ["status", "available_at"], unique=False
    )
    op.create_index(
        "ix_outbox_events_aggregate_history",
        "outbox_events",
        ["aggregate_type", "aggregate_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_events_stale_claims", "outbox_events", ["status", "claimed_at"], unique=False
    )
    op.create_index(
        "ix_outbox_events_published_retention",
        "outbox_events",
        ["status", "created_at"],
        unique=False,
    )

    op.add_column("jobs", sa.Column("dispatch_id", sa.Uuid(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_jobs_dispatch_id"), "jobs", ["dispatch_id"], unique=False)
    op.create_index("ix_jobs_status_created_at", "jobs", ["status", "created_at"], unique=False)


def downgrade() -> None:
    """Drop the outbox table and the job delivery-ownership columns."""
    op.drop_index("ix_jobs_status_created_at", table_name="jobs")
    op.drop_index(op.f("ix_jobs_dispatch_id"), table_name="jobs")
    op.drop_column("jobs", "execution_lease_expires_at")
    op.drop_column("jobs", "dispatch_id")

    op.drop_index("ix_outbox_events_published_retention", table_name="outbox_events")
    op.drop_index("ix_outbox_events_stale_claims", table_name="outbox_events")
    op.drop_index("ix_outbox_events_aggregate_history", table_name="outbox_events")
    op.drop_index("ix_outbox_events_due_claims", table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_organisation_id"), table_name="outbox_events")
    op.drop_table("outbox_events")
