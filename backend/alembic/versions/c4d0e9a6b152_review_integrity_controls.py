"""v0.4 review integrity controls

Revision ID: c4d0e9a6b152
Revises: f3a9c1b2d4e7
Create Date: 2026-08-07 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4d0e9a6b152"
down_revision: str | Sequence[str] | None = "f3a9c1b2d4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enforce pending-invitation uniqueness and preserve audit history."""
    op.execute(
        "CREATE UNIQUE INDEX uq_invitations_pending_organisation_email "
        "ON invitations (organisation_id, lower(email)) WHERE status = 'sent'"
    )
    op.drop_constraint(
        op.f("fk_audit_events_organisation_id_organisations"),
        "audit_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_audit_events_organisation_id_organisations"),
        "audit_events",
        "organisations",
        ["organisation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Restore the prior v0.4 foreign-key and index definitions."""
    op.drop_constraint(
        op.f("fk_audit_events_organisation_id_organisations"),
        "audit_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_audit_events_organisation_id_organisations"),
        "audit_events",
        "organisations",
        ["organisation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute("DROP INDEX uq_invitations_pending_organisation_email")
