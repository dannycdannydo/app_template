"""organisation_features table

Revision ID: f3a9c1b2d4e7
Revises: 27f2b8d4a6c1
Create Date: 2026-08-07 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f3a9c1b2d4e7"
down_revision: str | Sequence[str] | None = "27f2b8d4a6c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the organisation_features table (blueprint §27, Scope §6.7).

    One row is one platform-controlled override for one organisation. The
    unique ``(organisation_id, feature_key)`` pair is the invariant that an
    organisation has at most one override per known flag; ``enabled`` defaults
    to false so an explicit row is only ever created by a platform
    administrator's PUT, and ``configuration_json`` mirrors the blueprint's
    per-org flag configuration. No row means "no override", which the
    enforcement helper resolves to the catalogue default (off).
    """
    op.create_table(
        "organisation_features",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("feature_key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "configuration_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            name=op.f("fk_organisation_features_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organisation_features")),
        sa.UniqueConstraint(
            "organisation_id",
            "feature_key",
            name="uq_organisation_features_organisation_id_feature_key",
        ),
    )
    op.create_index(
        op.f("ix_organisation_features_organisation_id"),
        "organisation_features",
        ["organisation_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the organisation_features table."""
    op.drop_index(
        op.f("ix_organisation_features_organisation_id"),
        table_name="organisation_features",
    )
    op.drop_table("organisation_features")
