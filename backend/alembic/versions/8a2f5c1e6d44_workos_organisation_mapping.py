"""workos_organisation_mapping

Revision ID: 8a2f5c1e6d44
Revises: 58d33cd1ac4c
Create Date: 2026-08-06 10:33:13.385842

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8a2f5c1e6d44"
down_revision: str | Sequence[str] | None = "58d33cd1ac4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable, unique WorkOS organisation mapping (Scope §6.3).

    The column is nullable because pre-existing organisations have no
    mapping until one is created (eagerly at platform creation, lazily as a
    backfill at first invite); unique because the mapping is 1:1 with the
    internal organisation (ADR-0001). PostgreSQL permits any number of NULL
    values under a unique constraint, so unmapped organisations stay valid.
    """
    op.add_column(
        "organisations",
        sa.Column("workos_organisation_id", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_organisations_workos_organisation_id"),
        "organisations",
        ["workos_organisation_id"],
    )


def downgrade() -> None:
    """Drop the mapping column and its uniqueness constraint."""
    op.drop_constraint(
        op.f("uq_organisations_workos_organisation_id"),
        "organisations",
        type_="unique",
    )
    op.drop_column("organisations", "workos_organisation_id")
