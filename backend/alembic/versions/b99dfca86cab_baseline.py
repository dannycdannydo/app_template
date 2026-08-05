"""baseline

Revision ID: b99dfca86cab
Revises:
Create Date: 2026-08-05 00:55:04.347171

"""

# revision identifiers, used by Alembic.
revision: str = "b99dfca86cab"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the baseline schema. v0.1 defines no tables yet."""
    pass


def downgrade() -> None:
    """Drop the baseline schema. v0.1 defines no tables yet."""
    pass
