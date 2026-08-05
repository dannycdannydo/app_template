"""roles and membership roles

Revision ID: a99ef551ece1
Revises: 4fe59729839c
Create Date: 2026-08-05 06:44:44.278376

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.conventions import uuid7

# revision identifiers, used by Alembic.
revision: str = "a99ef551ece1"
down_revision: str | Sequence[str] | None = "4fe59729839c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The five default roles from blueprint §9. The organisation creation flow
# (Scope §6.3) assigns the creator the ``owner`` role, so the seed ships with
# the tables rather than waiting for the roles work unit (Scope §6.4).
DEFAULT_ROLES = (
    ("owner", "Owner"),
    ("administrator", "Administrator"),
    ("manager", "Manager"),
    ("member", "Member"),
    ("viewer", "Viewer"),
)


def upgrade() -> None:
    """Create the roles and membership_roles tables and seed the default roles.

    Constraint names follow the shared naming convention from
    ``app.db.conventions`` so they match what the ORM metadata declares; the
    membership_roles foreign keys cascade so deleting a membership or role
    removes its join rows at the database level.
    """
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("code", name=op.f("uq_roles_code")),
    )
    op.create_table(
        "membership_roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
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
            ["membership_id"],
            ["organisation_memberships.id"],
            name=op.f("fk_membership_roles_membership_id_organisation_memberships"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_membership_roles_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_membership_roles")),
        sa.UniqueConstraint(
            "membership_id",
            "role_id",
            name="uq_membership_roles_membership_id_role_id",
        ),
    )
    op.create_index(
        op.f("ix_membership_roles_membership_id"),
        "membership_roles",
        ["membership_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_membership_roles_role_id"),
        "membership_roles",
        ["role_id"],
        unique=False,
    )

    roles_table = sa.table(
        "roles",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String(length=64)),
        sa.column("name", sa.String(length=255)),
    )
    op.bulk_insert(
        roles_table,
        [{"id": uuid7(), "code": code, "name": name} for code, name in DEFAULT_ROLES],
    )


def downgrade() -> None:
    """Drop the roles tables; the seeded role rows disappear with them."""
    op.drop_index(op.f("ix_membership_roles_role_id"), table_name="membership_roles")
    op.drop_index(op.f("ix_membership_roles_membership_id"), table_name="membership_roles")
    op.drop_table("membership_roles")
    op.drop_table("roles")
