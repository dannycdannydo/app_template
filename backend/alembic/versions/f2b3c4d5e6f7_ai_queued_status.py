"""Add queued status and nullable routing columns to ai_requests

Revision ID: f2b3c4d5e6f7
Revises: d1a2b3c4e5f6
Create Date: 2026-08-11 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "d1a2b3c4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow a pre-enqueue ``queued`` AI request row (v0.7 Scope §5.8).

    The durable job creates an ``ai_requests`` row before publishing the broker
    message so the result endpoint is coherent immediately after the ``202``
    acknowledgement. A ``queued`` row carries no routing/budget information
    (the provider/model is not known until dispatch), so the routing columns
    become nullable and the status check constraint admits ``queued``. Budget
    reservation still happens at dispatch time when ``reserve()`` promotes the
    row to ``running``.
    """
    # The original ``d1a2b3c4e5f6`` migration created the status check via
    # ``op.create_table`` with ``sa.CheckConstraint(name="ck_ai_requests_ai_request_status")``.
    # The metadata naming convention (``ck_%(table_name)s_%(constraint_name)s``)
    # resolves that to ``ck_ai_requests_ck_ai_requests_ai_request_status`` in the
    # database — a double prefix. Drop whichever variant exists and recreate
    # with a single-prefixed name that matches what the ORM model resolves to.
    op.execute(
        "ALTER TABLE ai_requests DROP CONSTRAINT IF EXISTS "
        "ck_ai_requests_ck_ai_requests_ai_request_status"
    )
    op.execute("ALTER TABLE ai_requests DROP CONSTRAINT IF EXISTS ck_ai_requests_ai_request_status")
    op.execute(
        "ALTER TABLE ai_requests ADD CONSTRAINT ck_ai_requests_ai_request_status "
        "CHECK (status IN ('queued', 'running', 'succeeded', 'failed'))"
    )
    op.alter_column(
        "ai_requests",
        "provider",
        existing_type=sa.String(128),
        nullable=True,
    )
    op.alter_column(
        "ai_requests",
        "model",
        existing_type=sa.String(256),
        nullable=True,
    )
    op.alter_column(
        "ai_requests",
        "prompt_name",
        existing_type=sa.String(128),
        nullable=True,
    )
    op.alter_column(
        "ai_requests",
        "prompt_version",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # Queued rows only exist because of this migration's pre-enqueue feature;
    # delete them before restoring NOT NULL constraints on the routing columns.
    op.execute("DELETE FROM ai_requests WHERE status = 'queued'")
    op.alter_column(
        "ai_requests",
        "prompt_version",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "ai_requests",
        "prompt_name",
        existing_type=sa.String(128),
        nullable=False,
    )
    op.alter_column(
        "ai_requests",
        "model",
        existing_type=sa.String(256),
        nullable=False,
    )
    op.alter_column(
        "ai_requests",
        "provider",
        existing_type=sa.String(128),
        nullable=False,
    )
    op.execute("ALTER TABLE ai_requests DROP CONSTRAINT IF EXISTS ck_ai_requests_ai_request_status")
    # Restore the original double-prefixed constraint name (what the naming
    # convention produces from ``op.create_table``).
    op.execute(
        "ALTER TABLE ai_requests ADD CONSTRAINT "
        "ck_ai_requests_ck_ai_requests_ai_request_status "
        "CHECK (status IN ('running', 'succeeded', 'failed'))"
    )
