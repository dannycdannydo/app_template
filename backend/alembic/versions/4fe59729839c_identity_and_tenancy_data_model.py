"""identity and tenancy data model

Revision ID: 4fe59729839c
Revises: b99dfca86cab
Create Date: 2026-08-05 05:20:51.036693

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4fe59729839c"
down_revision: str | Sequence[str] | None = "b99dfca86cab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the users, organisations and organisation_memberships tables.

    Constraint names follow the shared naming convention from
    ``app.db.conventions`` so they match what the ORM metadata declares.
    """
    op.create_table(
        "organisations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organisations")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workos_user_id", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("workos_user_id", name=op.f("uq_users_workos_user_id")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)
    op.create_table(
        "organisation_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="active",
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
        sa.CheckConstraint(
            "status IN ('active', 'invited', 'suspended', 'left')",
            name=op.f("ck_organisation_memberships_membership_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_organisation_memberships_organisation_id_organisations"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_organisation_memberships_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organisation_memberships")),
        sa.UniqueConstraint(
            "user_id",
            "organisation_id",
            name="uq_organisation_memberships_user_id_organisation_id",
        ),
    )
    op.create_index(
        op.f("ix_organisation_memberships_organisation_id"),
        "organisation_memberships",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organisation_memberships_user_id"),
        "organisation_memberships",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the identity and tenancy tables."""
    op.drop_index(
        op.f("ix_organisation_memberships_user_id"), table_name="organisation_memberships"
    )
    op.drop_index(
        op.f("ix_organisation_memberships_organisation_id"),
        table_name="organisation_memberships",
    )
    op.drop_table("organisation_memberships")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
    op.drop_table("organisations")
