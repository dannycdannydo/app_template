"""bootstrap_platform_admin

Revision ID: 8a3ae5b53433
Revises: 8a2f5c1e6d44
Create Date: 2026-08-06 11:43:40.976860

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8a3ae5b53433"
down_revision: str | Sequence[str] | None = "8a2f5c1e6d44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the single-row bootstrap_state table (Scope §6.4).

    The row records which verified email consumed the one-time platform
    bootstrap and when. The id is a fixed sentinel guarded by a check
    constraint, so the primary key itself is the invariant that makes a
    concurrent double first-login impossible: the second transaction to insert
    the sentinel row violates the constraint and is treated by the grant hook
    as an already-consumed bootstrap (acceptance §5.5). The foreign key
    cascades so deleting the consuming user also removes the record.
    """
    op.create_table(
        "bootstrap_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("consumed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_bootstrap_state_single_row")),
        sa.ForeignKeyConstraint(
            ["consumed_by_user_id"],
            ["users.id"],
            name=op.f("fk_bootstrap_state_consumed_by_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bootstrap_state")),
    )
    op.create_index(
        op.f("ix_bootstrap_state_consumed_by_user_id"),
        "bootstrap_state",
        ["consumed_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the bootstrap record; the grant hook then re-arms on next login."""
    op.drop_index(
        op.f("ix_bootstrap_state_consumed_by_user_id"),
        table_name="bootstrap_state",
    )
    op.drop_table("bootstrap_state")
