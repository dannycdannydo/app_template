"""Production enablement of the RLS jobs table group (plan P3, group 4b).

Plan P3 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). This is the last direct-tenant group of ``docs/rls-rollout.md`` §3:
``jobs`` and its indirectly owned attempt ledger ``job_attempts``. It also
delivers the **mandatory prerequisite** that blocked 4b — the distinct
non-bypass ``app_coordinator`` role (ADR-0022 decision 3, ``docs/rls-rollout.md``
§1 and §3.1) — because three consumers read ``jobs``/``job_attempts`` across
tenants: the outbox coordinator, the in-process reliability-metrics refresh and
the ``reconcile_jobs`` operator CLI. They now connect as ``app_coordinator`` and
satisfy coordinator policies scoped to dispatch state rather than a tenant; none
of them gains ``BYPASSRLS``.

What this migration installs:

- the ``app_coordinator`` role (``NOLOGIN``, non-owner, non-superuser,
  non-``BYPASSRLS``), provisioned or safely adopted exactly like ``app_runtime``
  in the P2 prototype; its credential is configured separately as
  ``DATABASE_COORDINATOR_URL``;
- the denormalised, non-null ``job_attempts.organisation_id`` (ADR-0022
  decision 6), backfilled from the parent job, so a single direct policy applies
  without a join, and tied to its parent job's organisation by a composite
  ``(job_id, organisation_id)`` foreign key (with the matching unique pair on
  ``jobs``) so the copied tenant key cannot diverge from the parent;
- the ``app_current_job_id()`` fail-closed helper for the one-row worker
  bootstrap;
- default-deny policies: ``app_runtime`` gets the canonical organisation
  isolation policy on both tables plus the single-row worker-bootstrap
  ``FOR SELECT`` on ``jobs``; ``app_coordinator`` gets dispatch-state-scoped
  read/settlement policies;
- ``ENABLE`` and ``FORCE`` row-level security on both tables.

Absent, empty or malformed context resolves to ``NULL`` in
``app_current_tenant_id()`` / ``app_current_job_id()`` and therefore matches no
row and authorises no write; it never means unrestricted access. The
application layer remains the first enforcement layer: every jobs service query
keeps its ``organisation_id`` predicate and a foreign job stays a ``404``.

The narrow coordinator addendum for the notification exhaustion hook: when the
coordinator's bounded recovery terminally fails an attempt
(``enforce_attempt_ceiling_locked``), the registered ``notification.email``
exhaustion hook finalizes the delivery row. That hook binds the durable job's
own organisation and recipient user as transaction-local context before it
touches the user-private delivery rows, so ``app_coordinator`` is granted DML on
``notifications``/``notification_deliveries`` and relies on the existing
user-private policies (which are context-gated, never tenant-broad). This is a
deliberately narrow, reviewed consequence of the coordinator running the same
settlement code, and it is recorded in the group-4b handoff and ADR-0022.

The downgrade reverses exactly this group: it drops the new policies, the
``app_current_job_id()`` helper, the denormalised column (a destructive schema
change, applied through the destructive-migration human gate) and the
migration-owned ``app_coordinator`` role, leaving earlier groups intact.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d0e1f2a3b4c5"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Group 4b from ``docs/rls-rollout.md`` §3.
GROUP_TABLES = ("jobs", "job_attempts")

#: The tenant-scoped runtime role shared by every enabled table group.
RUNTIME_ROLE = "app_runtime"

#: The second non-bypass role for global dispatch work (ADR-0022 decision 3).
COORDINATOR_ROLE = "app_coordinator"

#: Canonical runtime policy naming convention: ``<table>_organisation_isolation``.
ORGANISATION_POLICY_SUFFIX = "_organisation_isolation"

#: The single-row worker bootstrap read policy on ``jobs``. It is deliberately
#: ``FOR SELECT`` only: a worker's broker id resolves exactly one durable row,
#: and PostgreSQL applies the UPDATE policies to ``SELECT ... FOR UPDATE`` too,
#: so the lock is taken *after* the bootstrap phase clears ``app.job_id`` and
#: binds the durable row's organisation. A permissive bootstrap UPDATE policy
#: would otherwise authorise a real update (including a tenant-key move) for
#: anyone who knows a job id.
WORKER_BOOTSTRAP_POLICY = "jobs_worker_bootstrap"

#: Coordinator policies: a broad read keyed to the dispatch identity and a
#: settlement update keyed to in-flight dispatch state.
COORDINATOR_JOBS_READ_POLICY = "jobs_coordinator_dispatch_read"
COORDINATOR_JOBS_SETTLE_POLICY = "jobs_coordinator_dispatch_settle"
COORDINATOR_ATTEMPTS_READ_POLICY = "job_attempts_coordinator_dispatch_read"
COORDINATOR_ATTEMPTS_SETTLE_POLICY = "job_attempts_coordinator_dispatch_settle"

#: The exact columns the coordinator's settlement/reconciliation paths write.
#: The coordinator is granted **column-level** UPDATE on only these, so it can
#: settle a dispatch but can never move a job's tenant key, rewrite its input,
#: result or creator, change its type, edit progress, or rewrite arbitrary
#: attempt identity fields (ADR-0022 decision 3 least privilege). The
#: post-update ``WITH CHECK`` on the settle policies additionally confines the
#: reachable states to the dispatch transitions the coordinator owns.
COORDINATOR_JOBS_UPDATE_COLUMNS = (
    "status",
    "dispatch_id",
    "owner_token",
    "execution_lease_expires_at",
    "error_code",
    "error_message",
    "completed_at",
)
COORDINATOR_ATTEMPTS_UPDATE_COLUMNS = ("status", "completed_at", "error_code")
#: Post-update states the coordinator settlement policies permit.
COORDINATOR_JOBS_SETTLE_STATES = ("queued", "failed")
COORDINATOR_ATTEMPTS_SETTLE_STATES = ("failed", "exhausted", "abandoned")

#: The denormalised tenant key added to the indirect attempt ledger.
ATTEMPT_TENANT_COLUMN = "organisation_id"
ATTEMPT_TENANT_INDEX = "ix_job_attempts_organisation_id"
ATTEMPT_TENANT_FK = "fk_job_attempts_organisation_id"

#: The composite ``(job_id, organisation_id)`` foreign key and the unique pair
#: on ``jobs`` it references, tying an attempt's copied tenant key to its parent
#: job's organisation as a database invariant (ADR-0022 decision 6). The
#: single-column ``job_id`` foreign key is replaced by this composite one.
ATTEMPT_JOB_FK = "fk_job_attempts_job_id_jobs"
ATTEMPT_JOB_ORG_FK = "fk_job_attempts_job_org_jobs"
JOBS_ID_ORG_UNIQUE = "uq_jobs_id_organisation_id"

#: Provenance recorded on the production policies for review and rollback.
POLICY_COMMENT = f"plan-p3-group4b:{revision}:organisation isolation (ADR-0022)"
WORKER_BOOTSTRAP_POLICY_COMMENT = (
    f"plan-p3-group4b:{revision}:worker single-row bootstrap read (ADR-0022 decision 3)"
)
COORDINATOR_POLICY_COMMENT = (
    f"plan-p3-group4b:{revision}:coordinator dispatch-state access (ADR-0022 decision 3)"
)

#: Role-comment marker that records "this migration created the role", so a
#: downgrade never drops an adopted, deployment-provisioned coordinator role.
_COORDINATOR_ROLE_OWNERSHIP_MARKER = f"alembic:{revision}:outbox-coordinator"

# ``current_setting(name, true)`` returns NULL when the setting was never set.
# An empty string (after ``clear_job_context``) or a malformed value resolves to
# NULL here rather than raising, so every "no usable context" case fails closed
# to "no rows".
_CURRENT_JOB_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_job_id() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE
    raw text;
BEGIN
    raw := current_setting('app.job_id', true);
    IF raw IS NULL OR raw = '' THEN
        RETURN NULL;
    END IF;
    RETURN raw::uuid;
EXCEPTION
    WHEN invalid_text_representation THEN
        RETURN NULL;
END;
$$;
"""

#: Provision (or safely adopt) the restricted coordinator role.
_PROVISION_COORDINATOR_ROLE = f"""
DO $$
DECLARE
    privileged_role name;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{COORDINATOR_ROLE}') THEN
        CREATE ROLE {COORDINATOR_ROLE}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        COMMENT ON ROLE {COORDINATOR_ROLE} IS '{_COORDINATOR_ROLE_OWNERSHIP_MARKER}';
    ELSE
        ALTER ROLE {COORDINATOR_ROLE}
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        IF EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_roles r ON r.oid = c.relowner
            WHERE r.rolname = '{COORDINATOR_ROLE}'
              AND c.relname IN ('jobs', 'job_attempts')
        ) THEN
            RAISE EXCEPTION
                'pre-existing role {COORDINATOR_ROLE} owns a protected table; '
                'refusing to treat it as the restricted coordinator role';
        END IF;
        FOR privileged_role IN
            SELECT DISTINCT r.rolname
            FROM pg_roles r
            WHERE r.rolname <> '{COORDINATOR_ROLE}'
              AND (
                    r.rolsuper
                 OR r.rolbypassrls
                 OR EXISTS (
                    SELECT 1 FROM pg_class c
                    WHERE c.relowner = r.oid
                      AND c.relname IN ('jobs', 'job_attempts')
                 )
              )
        LOOP
            EXECUTE format('REVOKE %I FROM {COORDINATOR_ROLE}', privileged_role);
        END LOOP;
    END IF;
END
$$;
"""

#: Drop the coordinator role only when this migration created it.
_DROP_OWNED_COORDINATOR_ROLE = f"""
DO $$
BEGIN
    IF (
        SELECT shobj_description(oid, 'pg_authid')
        FROM pg_roles
        WHERE rolname = '{COORDINATOR_ROLE}'
    ) = '{_COORDINATOR_ROLE_OWNERSHIP_MARKER}' THEN
        DROP ROLE {COORDINATOR_ROLE};
    END IF;
END
$$;
"""

_RUNTIME_ORGANISATION_POLICY = """
    FOR ALL
    TO {role}
    USING (organisation_id = app_current_tenant_id())
    WITH CHECK (organisation_id = app_current_tenant_id())
"""


def _organisation_policy_sql(table: str, role: str) -> str:
    return f"""
        CREATE POLICY {table}{ORGANISATION_POLICY_SUFFIX} ON {table}
        {_RUNTIME_ORGANISATION_POLICY.format(role=role)}
    """


def _quoted(values: tuple[str, ...]) -> str:
    """Render a tuple of literals as a SQL ``IN`` list (trusted constants)."""
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """Enable the jobs group: coordinator role, denormalised key and policies."""
    op.execute(_PROVISION_COORDINATOR_ROLE)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {COORDINATOR_ROLE}")
    # Least privilege for global dispatch work: the coordinator reads the
    # dispatch state of jobs and attempts and owns the outbox/maintenance
    # ledgers. Its UPDATE authority is **column-level** and limited to the
    # settlement/reconciliation columns, so even a permissive WITH CHECK cannot
    # let it move a tenant key, rewrite a payload/reference, edit progress or
    # change ownership identity. It never needs INSERT on jobs/attempts (workers
    # and the API own those), and it never gains BYPASSRLS.
    op.execute(f"GRANT SELECT ON jobs TO {COORDINATOR_ROLE}")
    op.execute(
        "GRANT UPDATE ("
        + ", ".join(COORDINATOR_JOBS_UPDATE_COLUMNS)
        + f") ON jobs TO {COORDINATOR_ROLE}"
    )
    op.execute(f"GRANT SELECT ON job_attempts TO {COORDINATOR_ROLE}")
    op.execute(
        "GRANT UPDATE ("
        + ", ".join(COORDINATOR_ATTEMPTS_UPDATE_COLUMNS)
        + f") ON job_attempts TO {COORDINATOR_ROLE}"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON outbox_events TO {COORDINATOR_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON maintenance_runs TO {COORDINATOR_ROLE}")
    # The coordinator's bounded recovery writes the job-failed audit event in
    # the same transaction as its settlement (``jobs_service._fail_locked``), so
    # it needs the append-only audit surface; ``audit_events`` remains
    # unprotected until the operational-ledger group (P4 group 6).
    op.execute(f"GRANT SELECT, INSERT ON audit_events TO {COORDINATOR_ROLE}")
    # The notification exhaustion hook runs inside a coordinator settlement
    # transaction; it binds the durable job's own organisation+recipient before
    # touching these user-private rows, so the existing context-gated policies
    # still apply and no tenant-broad read is granted (ADR-0022 decisions 3/5).
    op.execute(f"GRANT SELECT, UPDATE ON notifications TO {COORDINATOR_ROLE}")
    op.execute(f"GRANT SELECT, UPDATE ON notification_deliveries TO {COORDINATOR_ROLE}")
    # The in-process reliability-metrics refresh now runs on the coordinator
    # credential; it must be able to call the group-2 scalar aggregate (the
    # function still executes as the narrow app_metrics role).
    op.execute(
        f"GRANT EXECUTE ON FUNCTION app_attention_required_delivery_count() TO {COORDINATOR_ROLE}"
    )

    # Every group migration grants its own tables to the runtime role (the P2
    # ``ALL TABLES`` grant only covered the tables present when it ran).
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON jobs TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON job_attempts TO {RUNTIME_ROLE}")

    # Denormalised tenant key on the indirect attempt ledger (ADR-0022
    # decision 6): additive, backfilled from the parent job, then made
    # non-null with the parent's tenant foreign key.
    op.add_column(
        "job_attempts",
        sa.Column(ATTEMPT_TENANT_COLUMN, postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        "UPDATE job_attempts a SET organisation_id = j.organisation_id "
        "FROM jobs j WHERE a.job_id = j.id"
    )
    op.alter_column("job_attempts", ATTEMPT_TENANT_COLUMN, nullable=False)
    op.create_foreign_key(
        ATTEMPT_TENANT_FK,
        "job_attempts",
        "organisations",
        [ATTEMPT_TENANT_COLUMN],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(ATTEMPT_TENANT_INDEX, "job_attempts", [ATTEMPT_TENANT_COLUMN])
    # The copied tenant key must equal its parent job's organisation, not merely
    # reference *some* organisation (ADR-0022 decision 6). The unique pair on
    # ``jobs`` is the composite target; the composite FK replaces the earlier
    # single-column ``job_id`` FK so the (job, tenant) pair is one invariant.
    op.create_unique_constraint(JOBS_ID_ORG_UNIQUE, "jobs", ["id", ATTEMPT_TENANT_COLUMN])
    op.drop_constraint(ATTEMPT_JOB_FK, "job_attempts", type_="foreignkey")
    op.create_foreign_key(
        ATTEMPT_JOB_ORG_FK,
        "job_attempts",
        "jobs",
        ["job_id", ATTEMPT_TENANT_COLUMN],
        ["id", "organisation_id"],
        ondelete="RESTRICT",
    )

    op.execute(_CURRENT_JOB_FUNCTION)

    # Runtime: canonical organisation isolation plus the one-row worker
    # bootstrap. The bootstrap is a ``FOR SELECT`` policy keyed to the opaque
    # broker id: it grants no enumeration and no write authority. PostgreSQL
    # applies the UPDATE policy to ``SELECT ... FOR UPDATE`` too, so the worker
    # deliberately clears ``app.job_id`` and binds the durable row's
    # organisation *before* it locks the row, rather than adding a permissive
    # bootstrap UPDATE policy that would authorise a real update by job id.
    for table in GROUP_TABLES:
        policy = f"{table}{ORGANISATION_POLICY_SUFFIX}"
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(_organisation_policy_sql(table, RUNTIME_ROLE))
        op.execute(f"COMMENT ON POLICY {policy} ON {table} IS '{POLICY_COMMENT}'")
    op.execute(f"DROP POLICY IF EXISTS {WORKER_BOOTSTRAP_POLICY} ON jobs")
    op.execute(
        f"""
        CREATE POLICY {WORKER_BOOTSTRAP_POLICY} ON jobs
            FOR SELECT
            TO {RUNTIME_ROLE}
            USING (id = app_current_job_id())
        """
    )
    op.execute(
        f"COMMENT ON POLICY {WORKER_BOOTSTRAP_POLICY} ON jobs IS "
        f"'{WORKER_BOOTSTRAP_POLICY_COMMENT}'"
    )

    # Coordinator: dispatch-state-scoped read and settlement, never a tenant.
    # The read exposes any row that still carries a dispatch identity,
    # **including terminal rows that retain one**: that is deliberate, so the
    # coordinator can resolve a late dispatch event for a job that has since
    # settled instead of declaring the event dead. Terminal rows with no
    # dispatch identity are not visible to it.
    op.execute(f"DROP POLICY IF EXISTS {COORDINATOR_JOBS_READ_POLICY} ON jobs")
    op.execute(
        f"""
        CREATE POLICY {COORDINATOR_JOBS_READ_POLICY} ON jobs
            FOR SELECT
            TO {COORDINATOR_ROLE}
            USING (dispatch_id IS NOT NULL)
        """
    )
    op.execute(
        f"COMMENT ON POLICY {COORDINATOR_JOBS_READ_POLICY} ON jobs IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {COORDINATOR_JOBS_SETTLE_POLICY} ON jobs")
    op.execute(
        f"""
        CREATE POLICY {COORDINATOR_JOBS_SETTLE_POLICY} ON jobs
            FOR UPDATE
            TO {COORDINATOR_ROLE}
            USING (status IN ('queued', 'running'))
            WITH CHECK (status IN ({_quoted(COORDINATOR_JOBS_SETTLE_STATES)}))
        """
    )
    op.execute(
        f"COMMENT ON POLICY {COORDINATOR_JOBS_SETTLE_POLICY} ON jobs IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {COORDINATOR_ATTEMPTS_READ_POLICY} ON job_attempts")
    op.execute(
        f"""
        CREATE POLICY {COORDINATOR_ATTEMPTS_READ_POLICY} ON job_attempts
            FOR SELECT
            TO {COORDINATOR_ROLE}
            USING (owner_token IS NOT NULL)
        """
    )
    op.execute(
        f"COMMENT ON POLICY {COORDINATOR_ATTEMPTS_READ_POLICY} ON job_attempts IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {COORDINATOR_ATTEMPTS_SETTLE_POLICY} ON job_attempts")
    op.execute(
        f"""
        CREATE POLICY {COORDINATOR_ATTEMPTS_SETTLE_POLICY} ON job_attempts
            FOR UPDATE
            TO {COORDINATOR_ROLE}
            USING (status = 'running')
            WITH CHECK (status IN ({_quoted(COORDINATOR_ATTEMPTS_SETTLE_STATES)}))
        """
    )
    op.execute(
        f"COMMENT ON POLICY {COORDINATOR_ATTEMPTS_SETTLE_POLICY} ON job_attempts IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )

    for table in GROUP_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Reverse exactly the jobs group, its helper, key and owned role."""
    policies = {
        "jobs": (
            f"jobs{ORGANISATION_POLICY_SUFFIX}",
            WORKER_BOOTSTRAP_POLICY,
            COORDINATOR_JOBS_READ_POLICY,
            COORDINATOR_JOBS_SETTLE_POLICY,
        ),
        "job_attempts": (
            f"job_attempts{ORGANISATION_POLICY_SUFFIX}",
            COORDINATOR_ATTEMPTS_READ_POLICY,
            COORDINATOR_ATTEMPTS_SETTLE_POLICY,
        ),
    }
    for table, table_policies in policies.items():
        for policy in table_policies:
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS app_current_job_id()")
    # Reverse the composite parent key before dropping the tenant column:
    # restore the original single-column ``job_id`` FK, drop the unique pair.
    op.execute(f"ALTER TABLE job_attempts DROP CONSTRAINT IF EXISTS {ATTEMPT_JOB_ORG_FK}")
    op.create_foreign_key(
        ATTEMPT_JOB_FK,
        "job_attempts",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(f"ALTER TABLE jobs DROP CONSTRAINT IF EXISTS {JOBS_ID_ORG_UNIQUE}")
    op.execute(f"DROP INDEX IF EXISTS {ATTEMPT_TENANT_INDEX}")
    op.execute(f"ALTER TABLE job_attempts DROP CONSTRAINT IF EXISTS {ATTEMPT_TENANT_FK}")
    op.drop_column("job_attempts", ATTEMPT_TENANT_COLUMN)
    op.execute(f"REVOKE ALL ON notifications FROM {COORDINATOR_ROLE}")
    op.execute(f"REVOKE ALL ON notification_deliveries FROM {COORDINATOR_ROLE}")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION app_attention_required_delivery_count() "
        f"FROM {COORDINATOR_ROLE}"
    )
    op.execute(f"REVOKE ALL ON audit_events FROM {COORDINATOR_ROLE}")
    op.execute(f"REVOKE ALL ON maintenance_runs FROM {COORDINATOR_ROLE}")
    op.execute(f"REVOKE ALL ON outbox_events FROM {COORDINATOR_ROLE}")
    op.execute(f"REVOKE ALL ON job_attempts FROM {COORDINATOR_ROLE}")
    op.execute(f"REVOKE ALL ON jobs FROM {COORDINATOR_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {COORDINATOR_ROLE}")
    op.execute(_DROP_OWNED_COORDINATOR_ROLE)
