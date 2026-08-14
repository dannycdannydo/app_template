"""add attempt ownership token to jobs

Revision ID: e1f2a3b4c5d6
Revises: a5b6c7d8e9f0
Create Date: 2026-08-14 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the attempt-distinguishing ownership token to ``jobs``.

    Plan P2 ownership hardening: ``dispatch_id`` identifies the outbox dispatch
    (the delivery did not change on a retry or takeover), so it cannot by
    itself prove which *attempt* currently owns the row. ``owner_token`` is
    rotated on every atomic claim — including an expired-lease takeover — and
    every worker mutation verifies the captured token, so a superseded attempt
    (an expired worker after a takeover, or a released attempt after a retry)
    can never update progress, succeed, fail or release over the current owner.
    The column is nullable for pre-claim legacy rows and is internal only: no
    API schema references it.
    """
    op.add_column("jobs", sa.Column("owner_token", sa.Uuid(), nullable=True))


def downgrade() -> None:
    """Drop the ownership token column."""
    op.drop_column("jobs", "owner_token")
