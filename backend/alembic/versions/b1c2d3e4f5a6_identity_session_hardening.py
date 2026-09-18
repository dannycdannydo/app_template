"""identity and session hardening

Revision ID: b1c2d3e4f5a6
Revises: f4a1b2c3d4e5
Create Date: 2026-09-18 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = "f4a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add webhook delivery dedup and the invitation provider organisation.

    Two additive, non-destructive changes (plan P1):

    - ``invitations.workos_organisation_id`` records the WorkOS organisation an
      invitation was issued against, captured at send time. Login-time linking
      requires the live WorkOS invitation's organisation to equal this value so
      a local row cross-wired to another tenant's provider invitation cannot
      grant a membership. Nullable for pre-existing rows; those fail closed
      until re-invited.
    - ``webhook_events`` stores each processed WorkOS event id with a uniqueness
      constraint so a redelivery is a deterministic no-op. It holds only the
      provider event id, type and receipt time — never a payload, token or
      signature.
    """
    op.add_column(
        "invitations",
        sa.Column("workos_organisation_id", sa.String(length=255), nullable=True),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
        sa.UniqueConstraint("event_id", name="uq_webhook_events_event_id"),
    )


def downgrade() -> None:
    """Drop the webhook ledger and the invitation provider organisation."""
    op.drop_table("webhook_events")
    op.drop_column("invitations", "workos_organisation_id")
