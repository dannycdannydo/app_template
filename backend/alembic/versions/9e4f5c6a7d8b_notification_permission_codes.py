"""notification permission codes and role grants

Revision ID: 9e4f5c6a7d8b
Revises: 6a3c1b2d9e4f
Create Date: 2026-08-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.conventions import uuid7
from app.modules.permissions.constants import NOTIFICATION_PERMISSIONS, NOTIFICATION_ROLE_GRANTS

# revision identifiers, used by Alembic.
revision: str = "9e4f5c6a7d8b"
down_revision: str | Sequence[str] | None = "6a3c1b2d9e4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Insert the notifications permission codes and grant them to roles.

    Permission-model change (Scope §6.3, blueprint §33) — human review
    required. The inserts are idempotent on purpose: on a database created
    after this release, the original seed migration
    (``daa9d5af1521``) already runs with the extended ``PERMISSIONS`` /
    ``ROLE_PERMISSION_MAP`` catalogue and therefore inserts the notification
    codes and grants them itself; on an existing database the seed migration
    was applied with the earlier catalogue, so this migration is what adds the
    codes and the grants. ``ON CONFLICT (code) DO NOTHING`` and the
    ``NOT EXISTS`` grant guards make either path safe, and re-running after a
    partial failure is a no-op.

    Only the two new codes are touched (re-inserting the rest of the catalogue
    would violate the unique code constraint), and only the roles whose bundle
    changes are granted: owner and administrator already receive every org
    permission through ``ALL_PERMISSION_CODES``, so the narrower
    ``NOTIFICATION_ROLE_GRANTS`` covers them plus manager (both codes) and
    member (read only). The viewer bundle is deliberately unchanged — a
    read-only viewer holds no notification access, and default deny applies to
    any code not granted (BP §9). Grants join the role and permission rows on
    their stable codes, exactly like the original seed migration.
    """
    for code, name in NOTIFICATION_PERMISSIONS:
        op.execute(
            sa.text(
                "INSERT INTO permissions (id, code, name) "
                "VALUES (:id, :code, :name) ON CONFLICT (code) DO NOTHING"
            ).bindparams(id=uuid7(), code=code, name=name)
        )

    for role_code, permission_codes in NOTIFICATION_ROLE_GRANTS.items():
        for permission_code in permission_codes:
            op.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id) "
                    "SELECT :id, r.id, p.id FROM roles r, permissions p "
                    "WHERE r.code = :role_code AND p.code = :permission_code "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM role_permissions rp "
                    "WHERE rp.role_id = r.id AND rp.permission_id = p.id"
                    ")"
                ).bindparams(
                    id=uuid7(),
                    role_code=role_code,
                    permission_code=permission_code,
                )
            )


def downgrade() -> None:
    """Remove the notification grants and the two permission rows."""
    for role_code, permission_codes in NOTIFICATION_ROLE_GRANTS.items():
        for permission_code in permission_codes:
            op.execute(
                sa.text(
                    "DELETE FROM role_permissions WHERE role_id = "
                    "(SELECT id FROM roles WHERE code = :role_code) "
                    "AND permission_id = (SELECT id FROM permissions WHERE code = :permission_code)"
                ).bindparams(role_code=role_code, permission_code=permission_code)
            )
    for permission_code, _ in NOTIFICATION_PERMISSIONS:
        op.execute(
            sa.text("DELETE FROM permissions WHERE code = :code").bindparams(code=permission_code)
        )
