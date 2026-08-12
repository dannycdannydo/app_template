"""v07_architecture_audit_corrections

Revision ID: a7c8d9e0f1a2
Revises: f2b3c4d5e6f7
Create Date: 2026-08-12 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "f2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add AI-settings concurrency control and pluralise the bootstrap table."""
    op.add_column(
        "organisation_ai_settings",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_organisation_ai_settings_positive_version"),
        "organisation_ai_settings",
        "version > 0",
    )

    op.rename_table("bootstrap_state", "bootstrap_states")
    op.execute(
        "ALTER TABLE bootstrap_states RENAME CONSTRAINT pk_bootstrap_state TO pk_bootstrap_states"
    )
    op.execute(
        "ALTER TABLE bootstrap_states RENAME CONSTRAINT "
        "ck_bootstrap_state_single_row TO ck_bootstrap_states_single_row"
    )
    op.execute(
        "ALTER TABLE bootstrap_states RENAME CONSTRAINT "
        "fk_bootstrap_state_consumed_by_user_id_users "
        "TO fk_bootstrap_states_consumed_by_user_id_users"
    )
    op.execute(
        "ALTER INDEX ix_bootstrap_state_consumed_by_user_id "
        "RENAME TO ix_bootstrap_states_consumed_by_user_id"
    )


def downgrade() -> None:
    """Restore the pre-audit table name and remove settings versioning."""
    op.execute(
        "ALTER INDEX ix_bootstrap_states_consumed_by_user_id "
        "RENAME TO ix_bootstrap_state_consumed_by_user_id"
    )
    op.execute(
        "ALTER TABLE bootstrap_states RENAME CONSTRAINT "
        "fk_bootstrap_states_consumed_by_user_id_users "
        "TO fk_bootstrap_state_consumed_by_user_id_users"
    )
    op.execute(
        "ALTER TABLE bootstrap_states RENAME CONSTRAINT "
        "ck_bootstrap_states_single_row TO ck_bootstrap_state_single_row"
    )
    op.execute(
        "ALTER TABLE bootstrap_states RENAME CONSTRAINT pk_bootstrap_states TO pk_bootstrap_state"
    )
    op.rename_table("bootstrap_states", "bootstrap_state")

    op.drop_constraint(
        op.f("ck_organisation_ai_settings_positive_version"),
        "organisation_ai_settings",
        type_="check",
    )
    op.drop_column("organisation_ai_settings", "version")
