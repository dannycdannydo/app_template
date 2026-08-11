"""AI organisation settings, request and output tables

Revision ID: d1a2b3c4e5f6
Revises: 9e4f5c6a7d8b
Create Date: 2026-08-10 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.conventions import uuid7

# revision identifiers, used by Alembic.
revision: str = "d1a2b3c4e5f6"
down_revision: str | Sequence[str] | None = "9e4f5c6a7d8b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the AI persistence tables and backfill policy rows (v0.7 Scope §6.5).

    Three tables implement the organisation controls and the usage/cost/audit
    contract (BP §10, §27, §29, ADR-0017):

    - ``organisation_ai_settings``: one row per organisation (the unique
      ``organisation_id`` is the invariant), default **off**. Existing
      organisations are backfilled with their default-off row so AI is
      default-deny for the whole tenant fleet, exactly like new
      organisations.
    - ``ai_requests``: one row per attempted provider execution. The row is
      inserted before dispatch in ``running`` state. The first row carries the
      bounded execution reservation while ``estimated_cost`` retains every
      dispatch's own route estimate (documented reservation policy), and
      settlement updates it with actual usage/cost and terminal status. The
      org-scoped triple
      ``(organisation_id, request_id, attempt_number)`` is unique, so a
      retried job re-using the execution id cannot double-reserve budget and
      every dispatch of one execution (retries, repairs, fallbacks) is
      persisted as its own row. Cost is ``NUMERIC`` and timestamps are
      timezone-aware UTC (BP §10).
    - ``ai_outputs``: the validated, privacy-safe result of one request —
      output JSON (only when the task-level opt-in and the organisation
      retention policy both permit content retention) plus references/digests,
      never attachment bytes (BP §28, ADR-0017) — with the
      one-output-per-request unique pair and the human rating/approval fields
      from the §2 output contract.
    """
    op.create_table(
        "organisation_ai_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "allowed_provider_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "allowed_model_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("provider_override", sa.String(length=128), nullable=True),
        sa.Column("model_override", sa.String(length=128), nullable=True),
        sa.Column("monthly_budget", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("retention_policy_days", sa.Integer(), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
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
            name=op.f("fk_organisation_ai_settings_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name=op.f("fk_organisation_ai_settings_updated_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organisation_ai_settings")),
        sa.CheckConstraint(
            "monthly_budget IS NULL OR monthly_budget >= 0",
            name="ck_organisation_ai_settings_non_negative_monthly_budget",
        ),
        sa.CheckConstraint(
            "retention_policy_days IS NULL OR retention_policy_days > 0",
            name="ck_organisation_ai_settings_positive_retention_policy_days",
        ),
    )
    op.create_index(
        op.f("ix_organisation_ai_settings_organisation_id"),
        "organisation_ai_settings",
        ["organisation_id"],
        unique=True,
    )
    # Default-off backfill: every organisation that exists before this release
    # gets its policy row now, so AI is default-deny for the whole fleet. The
    # backfilled ids use the application's uuid7 generator; the insert is
    # executed as a Python loop so it works on every PostgreSQL version.
    connection = op.get_bind()
    organisation_ids = connection.execute(
        sa.text("SELECT id FROM organisations")
    ).scalars().all()
    for organisation_id in organisation_ids:
        connection.execute(
            sa.text(
                "INSERT INTO organisation_ai_settings (id, organisation_id) "
                "VALUES (:id, :organisation_id) ON CONFLICT DO NOTHING"
            ).bindparams(
                id=uuid7(),
                organisation_id=organisation_id,
            )
        )

    op.create_table(
        "ai_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.String(length=32), nullable=False),
        sa.Column(
            "attempt_number", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("task", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("prompt_name", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("routing_reason", sa.String(length=512), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="ai_request_status",
                native_enum=False,
                length=16,
            ),
            server_default="running",
            nullable=False,
        ),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column(
            "estimated_cost",
            sa.Numeric(precision=18, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "cost", sa.Numeric(precision=18, scale=6), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("latency_ms", sa.BigInteger(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("input_reference", sa.String(length=1024), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
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
            name=op.f("fk_ai_requests_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_ai_requests_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_requests")),
        sa.UniqueConstraint(
            "organisation_id",
            "request_id",
            "attempt_number",
            name="uq_ai_requests_org_request_attempt",
        ),
        sa.UniqueConstraint(
            "id",
            "organisation_id",
            name="uq_ai_requests_id_organisation_id",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_ai_requests_positive_attempt_number"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_ai_requests_ai_request_status",
        ),
        sa.CheckConstraint("input_tokens >= 0", name="ck_ai_requests_non_negative_input_tokens"),
        sa.CheckConstraint(
            "output_tokens >= 0", name="ck_ai_requests_non_negative_output_tokens"
        ),
        sa.CheckConstraint(
            "estimated_cost >= 0", name="ck_ai_requests_non_negative_estimated_cost"
        ),
        sa.CheckConstraint("cost >= 0", name="ck_ai_requests_non_negative_cost"),
        sa.CheckConstraint(
            "latency_ms >= 0", name="ck_ai_requests_non_negative_latency_ms"
        ),
    )
    op.create_index(
        op.f("ix_ai_requests_organisation_id"),
        "ai_requests",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ai_requests_organisation_id_created_at"),
        "ai_requests",
        ["organisation_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "ai_outputs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ai_request_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_reference", sa.String(length=1024), nullable=True),
        sa.Column("output_digest", sa.String(length=64), nullable=True),
        sa.Column("input_reference", sa.String(length=1024), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("human_rating", sa.Integer(), nullable=True),
        sa.Column("approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
            ["ai_request_id", "organisation_id"],
            ["ai_requests.id", "ai_requests.organisation_id"],
            name="fk_ai_outputs_ai_request_org_ai_requests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_ai_outputs_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_outputs")),
        sa.UniqueConstraint("ai_request_id", name="uq_ai_outputs_ai_request_id"),
        sa.CheckConstraint(
            "human_rating IS NULL OR human_rating BETWEEN 1 AND 5",
            name="ck_ai_outputs_human_rating_range",
        ),
    )
    op.create_index(
        op.f("ix_ai_outputs_organisation_id"),
        "ai_outputs",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ai_outputs_organisation_id_created_at"),
        "ai_outputs",
        ["organisation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the AI persistence tables (reverse order of dependencies)."""
    op.drop_index(op.f("ix_ai_outputs_organisation_id_created_at"), table_name="ai_outputs")
    op.drop_index(op.f("ix_ai_outputs_organisation_id"), table_name="ai_outputs")
    op.drop_table("ai_outputs")
    op.drop_index(op.f("ix_ai_requests_organisation_id_created_at"), table_name="ai_requests")
    op.drop_index(op.f("ix_ai_requests_organisation_id"), table_name="ai_requests")
    op.drop_table("ai_requests")
    op.drop_index(
        op.f("ix_organisation_ai_settings_organisation_id"),
        table_name="organisation_ai_settings",
    )
    op.drop_table("organisation_ai_settings")
    # The enum type is created natively by PostgreSQL even with
    # native_enum=False (SQLAlchemy emits a CHECK-style enum); drop it so a
    # full downgrade leaves no orphan types behind.
    sa.Enum(name="ai_request_status").drop(op.get_bind(), checkfirst=True)
