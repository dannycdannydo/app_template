"""invitations table

Revision ID: 27f2b8d4a6c1
Revises: 8a3ae5b53433
Create Date: 2026-08-06 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "27f2b8d4a6c1"
down_revision: str | Sequence[str] | None = "8a3ae5b53433"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the invitations table (Scope §6.5, design plan §2.3).

    The table records every invite the application sends through the WorkOS
    Invitation API: the target organisation, the invitee email, the intended
    organisation role, the WorkOS invitation id and expiry mirror, the actor
    who sent it, and a local lifecycle status. No membership row is created
    here — acceptance-time linking (the login-time service) owns membership
    creation. ``workos_invitation_id`` is unique but nullable (PostgreSQL
    treats NULLs as distinct, so pre-send rows never collide); ``email`` is
    indexed because login-time linking matches pending invitations by email.
    """
    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.Column("workos_invitation_id", sa.String(length=255), nullable=True),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="sent",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            "status IN ('sent', 'accepted', 'revoked', 'expired')",
            name=op.f("ck_invitations_invitation_status"),
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name=op.f("fk_invitations_invited_by_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_invitations_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint(
            "workos_invitation_id",
            name="uq_invitations_workos_invitation_id",
        ),
    )
    op.create_index(
        op.f("ix_invitations_organisation_id"), "invitations", ["organisation_id"], unique=False
    )
    op.create_index(op.f("ix_invitations_email"), "invitations", ["email"], unique=False)
    op.create_index(
        op.f("ix_invitations_invited_by_user_id"),
        "invitations",
        ["invited_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the invitations table."""
    op.drop_index(op.f("ix_invitations_invited_by_user_id"), table_name="invitations")
    op.drop_index(op.f("ix_invitations_email"), table_name="invitations")
    op.drop_index(op.f("ix_invitations_organisation_id"), table_name="invitations")
    op.drop_table("invitations")
