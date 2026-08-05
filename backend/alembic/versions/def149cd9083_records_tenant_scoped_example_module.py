"""records tenant-scoped example module

Revision ID: def149cd9083
Revises: daa9d5af1521
Create Date: 2026-08-05 08:03:06.428551

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "def149cd9083"
down_revision: str | Sequence[str] | None = "daa9d5af1521"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the records table, the v0.2 tenant-scoped example entity.

    Every row hangs off exactly one organisation through the ``organisation_id``
    foreign key; the database-level ``ON DELETE CASCADE`` keeps the tenant
    boundary clean if an organisation is ever removed. Constraint and index
    names follow the shared naming convention from ``app.db.conventions`` so
    they match what the ORM metadata declares. The composite index
    ``(organisation_id, created_at)`` serves the org-scoped list ordered
    newest-first; the single-column index covers any other org-scoped lookup.
    """
    op.create_table(
        "records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
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
            name=op.f("fk_records_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_records")),
    )
    op.create_index(
        op.f("ix_records_organisation_id"),
        "records",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        "ix_records_organisation_id_created_at",
        "records",
        ["organisation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the records table."""
    op.drop_index("ix_records_organisation_id_created_at", table_name="records")
    op.drop_index(op.f("ix_records_organisation_id"), table_name="records")
    op.drop_table("records")
