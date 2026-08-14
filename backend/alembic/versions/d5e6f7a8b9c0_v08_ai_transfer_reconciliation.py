"""Add deletion-attempt tracking for the §6.7 reconciliation sweep.

Revision ID: d5e6f7a8b9c0
Revises: c3d4e5f6a7b8
Create Date: 2026-08-14 12:00:00.000000

Additive change: ``ai_attachment_references.deletion_attempted_at`` records
when terminal cleanup last attempted to delete an AI-owned provider copy
(v0.8 Scope §2.5/§6.7). The terminal execution tail stamps it before the
best-effort provider deletion; the scheduled reconciliation sweep targets
exactly the rows whose owning AI request is terminal but whose provider copy
was never deleted (null timestamp) or whose last deletion attempt failed and
has waited past the bounded retry window. The sweep is idempotent by
construction — a successful deletion marks the row ``deleted`` and drops it
from every candidate query, and a crash mid-sweep simply leaves the rows for
the next run.

The column is never the managed signed URL or its query string: it is a UTC
timestamp only, and it carries no bearer material (Scope §2.3, BP §28). The
partial index serves the bounded candidate scan (provider-upload rows ordered
by claim time).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the deletion-attempt timestamp and its candidate-scan index."""
    op.add_column(
        "ai_attachment_references",
        sa.Column("deletion_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_ai_attachment_references_deletion_attempted_at",
        "ai_attachment_references",
        ["deletion_attempted_at"],
        postgresql_where=sa.text("transfer_mode = 'provider_upload' AND status <> 'deleted'"),
    )


def downgrade() -> None:
    """Drop the index and the column (both additive, reversible)."""
    op.drop_index(
        "ix_ai_attachment_references_deletion_attempted_at",
        table_name="ai_attachment_references",
    )
    op.drop_column("ai_attachment_references", "deletion_attempted_at")
