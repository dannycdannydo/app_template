"""files metadata records table

Revision ID: e7f3d9c2b5a1
Revises: c4d0e9a6b152
Create Date: 2026-08-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f3d9c2b5a1"
down_revision: str | Sequence[str] | None = "c4d0e9a6b152"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the files table, the v0.5 metadata record for stored objects.

    Blueprint §17 shape: every row hangs off exactly one organisation through
    the ``organisation_id`` foreign key (``ON DELETE CASCADE`` keeps the tenant
    boundary clean if an organisation is ever removed); the provider plane
    (provider, bucket, key) is captured at intent time and the key is
    server-generated, so it is unique. ``created_by_user_id`` references the
    uploader with ``ON DELETE SET NULL`` (a file outlives its uploader; users
    are deactivated, never hard deleted). ``status`` is a plain varchar with a
    check constraint covering the blueprint §17 lifecycle; ``deleted_at`` and
    the ``deleted`` status implement the soft delete, and the composite index
    ``(organisation_id, created_at)`` serves the org-scoped list ordered
    newest-first.
    """
    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("storage_provider", sa.String(length=32), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'uploaded', 'processing', 'ready', "
            "'failed', 'quarantined', 'deleted')",
            name=op.f("ck_files_file_status"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_files_positive_size_bytes")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_files_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_files_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_files")),
        sa.UniqueConstraint("object_key", name="uq_files_object_key"),
    )
    op.create_index(op.f("ix_files_organisation_id"), "files", ["organisation_id"], unique=False)
    op.create_index(
        "ix_files_organisation_id_created_at",
        "files",
        ["organisation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_files_created_by_user_id"), "files", ["created_by_user_id"], unique=False
    )


def downgrade() -> None:
    """Drop the files table."""
    op.drop_index(op.f("ix_files_created_by_user_id"), table_name="files")
    op.drop_index("ix_files_organisation_id_created_at", table_name="files")
    op.drop_index(op.f("ix_files_organisation_id"), table_name="files")
    op.drop_table("files")
