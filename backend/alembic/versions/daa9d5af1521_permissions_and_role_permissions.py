"""permissions and role permissions

Revision ID: daa9d5af1521
Revises: a99ef551ece1
Create Date: 2026-08-05 07:28:31.123329

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.conventions import uuid7
from app.modules.permissions.constants import PERMISSIONS, ROLE_PERMISSION_MAP

# revision identifiers, used by Alembic.
revision: str = "daa9d5af1521"
down_revision: str | Sequence[str] | None = "a99ef551ece1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the permission tables and seed the catalogue plus role grants.

    Constraint names follow the shared naming convention from
    ``app.db.conventions`` so they match what the ORM metadata declares. The
    role grants are seeded by joining the role and permission rows on their
    stable codes, because both ids are generated UUIDs unknown to this
    migration (roles were seeded by the previous migration).
    """
    op.create_table(
        "permissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("code", name=op.f("uq_permissions_code")),
    )
    op.create_table(
        "role_permissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("permission_id", sa.Uuid(), nullable=False),
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
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_permissions")),
        sa.UniqueConstraint(
            "role_id",
            "permission_id",
            name="uq_role_permissions_role_id_permission_id",
        ),
    )
    op.create_index(
        op.f("ix_role_permissions_role_id"),
        "role_permissions",
        ["role_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_role_permissions_permission_id"),
        "role_permissions",
        ["permission_id"],
        unique=False,
    )

    permissions_table = sa.table(
        "permissions",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String(length=128)),
        sa.column("name", sa.String(length=255)),
    )
    op.bulk_insert(
        permissions_table,
        [{"id": uuid7(), "code": code, "name": name} for code, name in PERMISSIONS],
    )

    for role_code, permission_codes in ROLE_PERMISSION_MAP.items():
        for permission_code in permission_codes:
            op.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id) "
                    "SELECT :id, r.id, p.id FROM roles r, permissions p "
                    "WHERE r.code = :role_code AND p.code = :permission_code"
                ).bindparams(
                    id=uuid7(),
                    role_code=role_code,
                    permission_code=permission_code,
                )
            )


def downgrade() -> None:
    """Drop the permission tables; the seeded rows disappear with them."""
    op.drop_index(op.f("ix_role_permissions_permission_id"), table_name="role_permissions")
    op.drop_index(op.f("ix_role_permissions_role_id"), table_name="role_permissions")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
