"""Add record versions/revisions and enforce append-only audit tables.

Plan P8 (auditable business records and conflict-safe edits):

- ``records.version`` is the integer optimistic-concurrency field (BP §10);
  it starts at 1 and every conditional update increments it. Additive and
  non-destructive: existing rows backfill to 1 via the server default.
- ``record_revisions`` is the bounded immutable history. One snapshot row per
  accepted create/update/delete holds the record's API-bounded ``title``/
  ``body``, so approved changes are reconstructable without putting record
  content into generic audit metadata. A brand-new table changes no existing
  row.
- ``audit_events.actor_user_id`` and ``audit_events.organisation_id`` lose
  their foreign keys. ``ON DELETE SET NULL`` is itself an UPDATE, which the new
  append-only trigger must reject, and it erased the actor/organisation
  identity the trail exists to keep. The columns become opaque UUIDs whose
  provenance survives the referenced row's deletion (ADR-0020). The
  ``ix_audit_events_*`` indexes stay.
- ``reject_append_only_mutation`` is installed on both ``audit_events`` and
  ``record_revisions`` as two triggers each: a row-level ``BEFORE UPDATE OR
  DELETE`` trigger, and a statement-level ``BEFORE TRUNCATE`` trigger.
  PostgreSQL's ``TRUNCATE`` fires no row-level trigger, so a single
  ``FOR EACH ROW`` trigger would leave ``TRUNCATE <table>`` able to empty the
  ledger outright; the statement-level trigger closes that bypass. The
  append-only guarantee is therefore enforced at the database boundary and not
  only by the absence of API write paths (blueprint §29, §10).

Revision ID: f4a1b2c3d4e5
Revises: b3c4d5e6f7a8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY_FUNCTION = """
CREATE FUNCTION reject_append_only_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only and cannot be modified', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;
"""


def upgrade() -> None:
    op.add_column(
        "records",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.create_check_constraint("ck_records_positive_version", "records", "version > 0")

    op.create_table(
        "record_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_record_revisions_positive_version"),
        ),
        sa.CheckConstraint(
            "action IN ('created', 'updated', 'deleted')",
            name=op.f("ck_record_revisions_record_revision_action"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_record_revisions")),
    )
    op.create_index(
        "ix_record_revisions_record_id_created_at",
        "record_revisions",
        ["record_id", "created_at"],
    )
    op.create_index(
        "ix_record_revisions_organisation_id_created_at",
        "record_revisions",
        ["organisation_id", "created_at"],
    )
    op.create_index(
        "ix_record_revisions_actor_user_id",
        "record_revisions",
        ["actor_user_id"],
    )

    # A referential action would be an UPDATE/DELETE the trigger rejects, and
    # SET NULL would discard the identity an audit trail must retain.
    op.drop_constraint("fk_audit_events_actor_user_id_users", "audit_events", type_="foreignkey")
    op.drop_constraint(
        "fk_audit_events_organisation_id_organisations", "audit_events", type_="foreignkey"
    )

    op.execute(_APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_audit_events_append_only "
        "BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_record_revisions_append_only "
        "BEFORE UPDATE OR DELETE ON record_revisions "
        "FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()"
    )
    # ``TRUNCATE`` does not fire row-level triggers, and the application uses
    # the table-owning role, so a row-level trigger alone leaves a direct
    # ``TRUNCATE audit_events``/``TRUNCATE record_revisions`` able to destroy
    # the append-only ledger. A statement-level trigger fires for TRUNCATE and
    # closes that database-boundary bypass (reject_append_only_mutation, 55000).
    op.execute(
        "CREATE TRIGGER trg_audit_events_append_only_truncate "
        "BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_record_revisions_append_only_truncate "
        "BEFORE TRUNCATE ON record_revisions "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_append_only_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_record_revisions_append_only_truncate ON record_revisions"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only_truncate ON audit_events")
    op.execute("DROP TRIGGER trg_record_revisions_append_only ON record_revisions")
    op.execute("DROP TRIGGER trg_audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION reject_append_only_mutation()")

    # Restore the prior ``ON DELETE SET NULL`` behaviour for the provenance the
    # upgrade deliberately retained: an opaque actor/organisation id whose
    # referent was hard-deleted cannot satisfy the re-added foreign key. The
    # downgrade is therefore lossy for those orphaned identities (a downgrade
    # is for rollback/local work, never the documented production path).
    op.execute(
        "UPDATE audit_events SET actor_user_id = NULL "
        "WHERE actor_user_id IS NOT NULL AND actor_user_id NOT IN (SELECT id FROM users)"
    )
    op.execute(
        "UPDATE audit_events SET organisation_id = NULL "
        "WHERE organisation_id IS NOT NULL "
        "AND organisation_id NOT IN (SELECT id FROM organisations)"
    )

    op.create_foreign_key(
        op.f("fk_audit_events_organisation_id_organisations"),
        "audit_events",
        "organisations",
        ["organisation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_audit_events_actor_user_id_users"),
        "audit_events",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_index("ix_record_revisions_actor_user_id", table_name="record_revisions")
    op.drop_index("ix_record_revisions_organisation_id_created_at", table_name="record_revisions")
    op.drop_index("ix_record_revisions_record_id_created_at", table_name="record_revisions")
    op.drop_table("record_revisions")

    op.drop_constraint("ck_records_positive_version", "records", type_="check")
    op.drop_column("records", "version")
