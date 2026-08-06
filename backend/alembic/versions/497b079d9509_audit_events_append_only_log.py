"""audit events append-only log

Revision ID: 497b079d9509
Revises: def149cd9083
Create Date: 2026-08-06 07:59:28.688056

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "497b079d9509"
down_revision: str | Sequence[str] | None = "def149cd9083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the append-only audit log (blueprint §29).

    Every lifecycle action writes one row through the audit service; there is
    deliberately no ``updated_at`` column (a row is never modified) and no
    update or delete endpoint anywhere in the API. ``organisation_id`` and
    ``actor_user_id`` are nullable because platform-wide and system events have
    no organisation or actor; the actor foreign key uses ``ON DELETE SET NULL``
    so an audit trail survives the removal of the user who caused it, while the
    organisation foreign key cascades with the tenant boundary like every other
    org-scoped table. ``metadata`` is a JSONB snapshot of request context
    (request id at minimum) plus any event-specific detail. The composite
    index ``(organisation_id, created_at)`` serves the filtered listing ordered
    newest-first; the single-column indexes cover the other approved filter
    fields.
    """
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("resource_type", sa.String(length=80), nullable=False),
        sa.Column("resource_id", sa.String(length=80), nullable=False),
        sa.Column(
            "metadata",
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
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_audit_events_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        op.f("ix_audit_events_organisation_id"),
        "audit_events",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_events_actor_user_id"),
        "audit_events",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(op.f("ix_audit_events_action"), "audit_events", ["action"], unique=False)
    op.create_index(
        "ix_audit_events_organisation_id_created_at",
        "audit_events",
        ["organisation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the audit log."""
    op.drop_index("ix_audit_events_organisation_id_created_at", table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_action"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_actor_user_id"), table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_organisation_id"), table_name="audit_events")
    op.drop_table("audit_events")
