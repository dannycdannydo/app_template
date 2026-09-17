"""Add pinned file content identity and durable AI scratch intents.

Plan P6 (document authority, immutable uploads):

- ``files.content_identity`` pins the provider checksum of the promoted final
  object; the source authority and download compare against it so a same-key
  overwrite after approval fails closed. Nullable: existing rows have no
  recorded identity and are therefore not treated as trusted input until they
  are re-completed (additive, non-destructive).
- ``ai_scratch_uploads`` is the durable lifecycle record for transient AI
  scratch objects, closing the null-retention gap: an intent carries the
  bounded global expiry enforced independently of per-organisation retention.
  Additive: a brand-new table, no existing row is changed.

Revision ID: b3c4d5e6f7a8
Revises: d9e0f1a2b3c4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "files",
        sa.Column("content_identity", sa.String(length=255), nullable=True),
    )
    op.create_table(
        "ai_scratch_uploads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 50000000",
            name=op.f("ck_ai_scratch_uploads_size_range"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'expired', 'deleted')",
            name=op.f("ck_ai_scratch_uploads_scratch_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_ai_scratch_uploads_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_scratch_uploads")),
        sa.UniqueConstraint("object_key", name=op.f("uq_ai_scratch_uploads_object_key")),
        sa.UniqueConstraint("organisation_id", "upload_id", name="uq_ai_scratch_org_upload_id"),
    )
    op.create_index(
        "ix_ai_scratch_uploads_expires_at",
        "ai_scratch_uploads",
        ["expires_at"],
    )
    op.create_index(
        "ix_ai_scratch_uploads_organisation_id",
        "ai_scratch_uploads",
        ["organisation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_scratch_uploads_organisation_id", table_name="ai_scratch_uploads")
    op.drop_index("ix_ai_scratch_uploads_expires_at", table_name="ai_scratch_uploads")
    op.drop_table("ai_scratch_uploads")
    op.drop_column("files", "content_identity")
