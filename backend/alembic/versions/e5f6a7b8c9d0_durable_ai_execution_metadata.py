"""Add bounded transient metadata for durable AI task variables.

Revision ID: e5f6a7b8c9d0
Revises: b4c5d6e7f8a9

The ``ai.execute`` broker contract remains reference-only (``job_id``). A
durable ``document.ask`` operation nevertheless has to recover its bounded
question after the submitting HTTP request ends. The tenant-scoped queued
``ai_requests`` row now carries that JSON-safe execution metadata together
with a mandatory hard expiry. Workers clear it at terminal settlement and the
AI retention sweep clears crash/orphan leftovers independently of an
organisation's optional output-retention policy.

This is additive and does not change the existing forced RLS policy on
``ai_requests``; the two new columns inherit the row's organisation boundary.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the paired metadata/expiry columns and cleanup index."""
    op.add_column(
        "ai_requests",
        sa.Column("execution_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "ai_requests",
        sa.Column("execution_metadata_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "execution_metadata_expiry_pair",
        "ai_requests",
        "(execution_metadata IS NULL) = (execution_metadata_expires_at IS NULL)",
    )
    op.create_index(
        "ix_ai_requests_execution_metadata_expires_at",
        "ai_requests",
        ["execution_metadata_expires_at"],
        postgresql_where=sa.text("execution_metadata IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove the additive durable-execution metadata fields."""
    op.drop_index("ix_ai_requests_execution_metadata_expires_at", table_name="ai_requests")
    op.drop_constraint(
        op.f("ck_ai_requests_execution_metadata_expiry_pair"),
        "ai_requests",
        type_="check",
    )
    op.drop_column("ai_requests", "execution_metadata_expires_at")
    op.drop_column("ai_requests", "execution_metadata")
