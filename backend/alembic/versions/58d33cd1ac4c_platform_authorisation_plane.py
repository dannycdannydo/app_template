"""platform authorisation plane

Revision ID: 58d33cd1ac4c
Revises: 497b079d9509
Create Date: 2026-08-06 07:59:29.247086

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.conventions import uuid7
from app.modules.permissions.constants import (
    PLATFORM_ADMIN_ROLE_CODE,
    PLATFORM_PERMISSIONS,
)

# revision identifiers, used by Alembic.
revision: str = "58d33cd1ac4c"
down_revision: str | Sequence[str] | None = "497b079d9509"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the platform authorisation plane and seed its first role.

    The three tables mirror the org plane (``roles``, ``role_permissions``,
    ``membership_roles``) so the platform plane is enforced by the same
    machinery, not a flag (Scope §6.2). ``platform_role_permissions`` reuses
    the shared ``permissions`` table with the ``platform.*`` codes; the seed
    adds the ``platform.admin`` permission (it was not part of the v0.2 seed)
    and grants it to the ``platform_admin`` role, so a user is a platform
    admin exactly when a platform membership row links them to that role.
    Foreign keys cascade: deleting a role, permission or user removes the
    platform-plane rows that reference it.
    """
    op.create_table(
        "platform_roles",
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_roles")),
        sa.UniqueConstraint("code", name=op.f("uq_platform_roles_code")),
    )
    op.create_table(
        "platform_role_permissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("platform_role_id", sa.Uuid(), nullable=False),
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
            ["platform_role_id"],
            ["platform_roles.id"],
            name=op.f("fk_platform_role_permissions_platform_role_id_platform_roles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_platform_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_role_permissions")),
        sa.UniqueConstraint(
            "platform_role_id",
            "permission_id",
            name="uq_platform_role_permissions_platform_role_id_permission_id",
        ),
    )
    op.create_index(
        op.f("ix_platform_role_permissions_platform_role_id"),
        "platform_role_permissions",
        ["platform_role_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_role_permissions_permission_id"),
        "platform_role_permissions",
        ["permission_id"],
        unique=False,
    )
    op.create_table(
        "platform_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("platform_role_id", sa.Uuid(), nullable=False),
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
            ["user_id"],
            ["users.id"],
            name=op.f("fk_platform_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["platform_role_id"],
            ["platform_roles.id"],
            name=op.f("fk_platform_memberships_platform_role_id_platform_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_memberships")),
        sa.UniqueConstraint(
            "user_id",
            "platform_role_id",
            name="uq_platform_memberships_user_id_platform_role_id",
        ),
    )
    op.create_index(
        op.f("ix_platform_memberships_user_id"),
        "platform_memberships",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_platform_memberships_platform_role_id"),
        "platform_memberships",
        ["platform_role_id"],
        unique=False,
    )

    platform_roles_table = sa.table(
        "platform_roles",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String(length=64)),
        sa.column("name", sa.String(length=255)),
    )
    op.bulk_insert(
        platform_roles_table,
        [
            {
                "id": uuid7(),
                "code": PLATFORM_ADMIN_ROLE_CODE,
                "name": "Platform Admin",
            }
        ],
    )

    # The platform.admin code joins the shared permission catalogue; the grant
    # joins the role and permission rows on their stable codes because both
    # ids are generated UUIDs unknown to this migration.
    for code, name in PLATFORM_PERMISSIONS:
        op.execute(
            sa.text(
                "INSERT INTO permissions (id, code, name) VALUES (:id, :code, :name)"
            ).bindparams(id=uuid7(), code=code, name=name)
        )
        op.execute(
            sa.text(
                "INSERT INTO platform_role_permissions (id, platform_role_id, permission_id) "
                "SELECT :id, r.id, p.id FROM platform_roles r, permissions p "
                "WHERE r.code = :role_code AND p.code = :permission_code"
            ).bindparams(
                id=uuid7(),
                role_code=PLATFORM_ADMIN_ROLE_CODE,
                permission_code=code,
            )
        )


def downgrade() -> None:
    """Drop the platform plane; the seeded permission row leaves with it.

    The seeded ``platform.admin`` permission is removed from the shared
    ``permissions`` table too, so a downgrade returns the database exactly to
    the pre-platform state and a later re-upgrade can re-seed it (unique
    constraint on ``permissions.code``).
    """
    op.drop_index(
        op.f("ix_platform_memberships_platform_role_id"), table_name="platform_memberships"
    )
    op.drop_index(op.f("ix_platform_memberships_user_id"), table_name="platform_memberships")
    op.drop_table("platform_memberships")
    op.drop_index(
        op.f("ix_platform_role_permissions_permission_id"),
        table_name="platform_role_permissions",
    )
    op.drop_index(
        op.f("ix_platform_role_permissions_platform_role_id"),
        table_name="platform_role_permissions",
    )
    op.drop_table("platform_role_permissions")
    op.drop_table("platform_roles")
    for code, _name in PLATFORM_PERMISSIONS:
        op.execute(sa.text("DELETE FROM permissions WHERE code = :code").bindparams(code=code))
