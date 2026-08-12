"""Add the organisation transfer policy columns (v0.8 Scope §6.2).

Revision ID: b2c3d4e5f6a7
Revises: a7c8d9e0f1a2
Create Date: 2026-08-12 10:00:00.000000

Additive change: ``organisation_ai_settings`` gains the default-deny transfer
policy — ``allowed_transfer_modes`` (default ``["inline"]`` only) and
``max_large_attachment_bytes`` (default 50,000,000, the template ceiling,
which the organisation can only tighten). Existing rows are covered by the
server defaults, so no backfill is needed and the one-row-per-organisation
invariant is untouched (BP §10, v0.8 Scope §2.2/§6.2).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the organisation transfer-policy columns and ceiling constraint."""
    op.add_column(
        "organisation_ai_settings",
        sa.Column(
            "allowed_transfer_modes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[\"inline\"]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "organisation_ai_settings",
        sa.Column(
            "max_large_attachment_bytes",
            sa.Integer(),
            server_default=sa.text("50000000"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_organisation_ai_settings_max_large_attachment_bytes_range"),
        "organisation_ai_settings",
        "max_large_attachment_bytes > 0 AND max_large_attachment_bytes <= 50000000",
    )


def downgrade() -> None:
    """Drop the ceiling constraint and the transfer-policy columns."""
    op.drop_constraint(
        op.f("ck_organisation_ai_settings_max_large_attachment_bytes_range"),
        "organisation_ai_settings",
        type_="check",
    )
    op.drop_column("organisation_ai_settings", "max_large_attachment_bytes")
    op.drop_column("organisation_ai_settings", "allowed_transfer_modes")
