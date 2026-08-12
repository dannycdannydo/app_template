"""Add the organisation-scoped AI attachment reference table (v0.8 Scope §6.3).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-12 12:00:00.000000

Additive change: the durable, organisation-scoped record of one non-inline
transfer (Scope §2.3). Every non-inline transfer of a private source object —
provider upload, managed signed URL or Vertex ``gs://`` staging reference —
persists exactly one row here with the logical request it belongs to, the
provider/mode/region, the opaque external id (a provider file id or ``gs://``
URI, never a managed signed URL), the verified source identity (reference,
SHA-256 digest, size, MIME, lifecycle) and the lifecycle state/timestamps. It
never stores bytes, credentials, request headers, a managed signed URL or its
query string, or raw provider responses (Scope §2.3, BP §28).

The partial unique index on ``(organisation_id, idempotency_key) WHERE status
= 'live'`` enforces at most one live transfer per derived idempotency key per
organisation (the key is the SHA-256 of provider|mode|organisation|logical
request|digest|region), so a retry reuses only a live matching record from the
same logical request, a changed digest/provider/region creates a new idempotent
transfer, and an expired/deleted row never blocks its replacement (Scope §2.3,
§5.4). The transfer-mode CHECK stores only the three non-inline modes: the
table is the durable record of non-inline transfers, so ``inline`` is rejected
at the database, not just by the application.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``ai_attachment_references`` table (Scope §2.3, BP §9-§10, §28)."""
    op.create_table(
        "ai_attachment_references",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("logical_request_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("transfer_mode", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=2048), nullable=False),
        sa.Column("source_reference", sa.String(length=1024), nullable=False),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("source_lifecycle", sa.String(length=16), nullable=False),
        sa.Column("region", sa.String(length=128), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'live'"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisations.id"],
            name=op.f("fk_ai_attachment_references_organisation_id_organisations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_attachment_references")),
        sa.CheckConstraint(
            "transfer_mode IN ('provider_upload', 'managed_signed_url', 'storage_reference')",
            name="ck_ai_attachment_references_transfer_mode",
        ),
        sa.CheckConstraint(
            "source_lifecycle IN ('transient', 'retained')",
            name="ck_ai_attachment_references_source_lifecycle",
        ),
        sa.CheckConstraint(
            "status IN ('live', 'expired', 'deleted')",
            name="ck_ai_attachment_references_status",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 50000000",
            name="ck_ai_attachment_references_size_range",
        ),
        sa.CheckConstraint(
            "source_digest ~ '^[0-9a-f]{64}$'",
            name="ck_ai_attachment_references_digest_format",
        ),
    )
    # Retry-only reuse idempotency (Scope §2.3): at most one live transfer per
    # organisation per derived idempotency key; the partial predicate lets an
    # expired/deleted row be replaced without first deleting it.
    op.create_index(
        "uq_ai_attachment_references_org_key_live",
        "ai_attachment_references",
        ["organisation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status = 'live'"),
    )
    # Request-scoped lifecycle operations and the org-scoped newest-first scan.
    op.create_index(
        "ix_ai_attachment_references_org_request",
        "ai_attachment_references",
        ["organisation_id", "logical_request_id"],
    )
    op.create_index(
        "ix_ai_attachment_references_organisation_id_created_at",
        "ai_attachment_references",
        ["organisation_id", "created_at"],
    )
    # The organisation column's own index (mirrors the ai_requests pattern).
    op.create_index(
        "ix_ai_attachment_references_organisation_id",
        "ai_attachment_references",
        ["organisation_id"],
    )


def downgrade() -> None:
    """Drop the reference table and its indexes."""
    op.drop_index(
        "ix_ai_attachment_references_organisation_id",
        table_name="ai_attachment_references",
    )
    op.drop_index(
        "ix_ai_attachment_references_organisation_id_created_at",
        table_name="ai_attachment_references",
    )
    op.drop_index("ix_ai_attachment_references_org_request", table_name="ai_attachment_references")
    op.drop_index("uq_ai_attachment_references_org_key_live", table_name="ai_attachment_references")
    op.drop_table("ai_attachment_references")
