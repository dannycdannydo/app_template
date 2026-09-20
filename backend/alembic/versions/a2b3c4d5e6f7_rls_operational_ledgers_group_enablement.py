"""Production enablement of the RLS operational-ledger group (plan P4, group 6).

Plan P4 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). This is the operational-ledger group of ``docs/rls-rollout.md`` §3:

- ``audit_events`` — append-only audit ledger with a **nullable**
  ``organisation_id`` (platform/system events have no tenant);
- ``outbox_events`` — transactional outbox with a **nullable**
  ``organisation_id`` (global maintenance events have no tenant);
- ``maintenance_runs`` — global infrastructure ledger with no tenant key; and
- ``webhook_events`` — global provider-delivery dedup ledger.

A ``NULL`` tenant key never means "all rows" (ADR-0022 decision 7). The group
keeps every existing application access path working without a runtime bypass:

- **`audit_events`.** The ordinary runtime role may append only rows it may
  legitimately attribute — its own validated tenant's rows or a global
  (null-tenant) row — and, under the validated platform context, any row; the
  table is append-only and every writer is a trusted server-side service, but
  the ``WITH CHECK`` now makes a foreign-tenant attribution a policy violation
  even if an application predicate is ever missed. It may read only rows whose
  ``organisation_id`` equals the validated tenant, and may never update or
  delete. The cross-tenant platform audit screen reads through an **explicit,
  validated, transaction-local platform context** (``app.platform_admin``) the
  platform permission dependency binds after authorisation — the reviewed
  platform path of ADR-0022 decision 4, never a table exemption. Global
  (null-tenant) rows are therefore reachable only under that platform context.
- **`outbox_events`.** The runtime role reads and appends only its own
  organisation's rows, or the global (null-tenant) maintenance rows it
  legitimately produces; ``app_coordinator`` reads the whole dispatch ledger
  and may append, update the claim/settle/release/recovery lifecycle columns of
  in-flight rows (including locking published rows for the retention sweep) and
  delete published rows. The coordinator's UPDATE authority is **column-level**
  plus state-scoped, so it cannot rewrite a settled row's tenant key, event
  identity/contract, payload or aggregate reference, nor delete a live
  dispatch.
- **`maintenance_runs`** and **`webhook_events`** carry no tenant key and no
  tenant payload: they are global infrastructure, so RLS admits the roles that
  own those paths and denies every other role.

The group also delivers the reviewed operational credential of ADR-0022
decision 4: ``app_operator`` is created ``NOLOGIN`` with ``BYPASSRLS`` (its
legitimate scope is exactly the cross-tenant read a policy cannot express), is
granted read access for backup/support tooling, and is never a member of the
owner, superuser, runtime or coordinator roles. It is loaded only by audited
CLI/ops tooling (``DATABASE_OPERATOR_URL``), never by an HTTP process or a
worker. The adoption gate's deployment check proves the runtime role is
non-owner and lacks ``BYPASSRLS``; ``app_operator`` is the deliberately
separate, auditable exception.

The downgrade removes exactly this group's policies, helpers, grants and the
migration-owned ``app_operator`` role, leaving earlier groups intact.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Group 6 from ``docs/rls-rollout.md`` §3.
GROUP_TABLES = ("audit_events", "outbox_events", "maintenance_runs", "webhook_events")

#: The tenant-scoped runtime role shared by every enabled table group.
RUNTIME_ROLE = "app_runtime"

#: The second non-bypass role for global dispatch work (ADR-0022 decision 3).
COORDINATOR_ROLE = "app_coordinator"

#: The isolated, audited operational credential (ADR-0022 decision 4).
OPERATOR_ROLE = "app_operator"

#: Canonical organisation-isolation policy naming convention.
ORGANISATION_POLICY_SUFFIX = "_organisation_isolation"

#: Operational-ledger policies.
AUDIT_PLATFORM_READ_POLICY = "audit_events_platform_read"
AUDIT_APPEND_POLICY = "audit_events_append"
AUDIT_COORDINATOR_APPEND_POLICY = "audit_events_coordinator_append"
OUTBOX_APPEND_POLICY = "outbox_events_append"
OUTBOX_COORDINATOR_READ_POLICY = "outbox_events_coordinator_read"
OUTBOX_COORDINATOR_APPEND_POLICY = "outbox_events_coordinator_append"
OUTBOX_COORDINATOR_UPDATE_POLICY = "outbox_events_coordinator_update"
OUTBOX_COORDINATOR_DELETE_POLICY = "outbox_events_coordinator_delete"
MAINTENANCE_RUNTIME_READ_POLICY = "maintenance_runs_runtime_read"
MAINTENANCE_RUNTIME_UPDATE_POLICY = "maintenance_runs_runtime_update"
MAINTENANCE_COORDINATOR_POLICY = "maintenance_runs_coordinator_access"
WEBHOOK_RUNTIME_READ_POLICY = "webhook_events_runtime_read"
WEBHOOK_RUNTIME_APPEND_POLICY = "webhook_events_runtime_append"

#: Post-update states the coordinator's outbox UPDATE policy permits. The
#: ``USING`` domain includes ``published`` so the retention sweep may take its
#: ``SELECT ... FOR UPDATE`` lock (PostgreSQL applies the UPDATE policy to a
#: locking read), while ``WITH CHECK`` still confines the reachable post-update
#: states to the dispatch lifecycles the coordinator owns.
OUTBOX_COORDINATOR_UPDATE_USING_STATES = ("pending", "publishing", "published")
OUTBOX_COORDINATOR_UPDATE_CHECK_STATES = ("pending", "publishing", "published", "dead")

#: The exact ``outbox_events`` columns the coordinator's claim/settle/release/
#: recovery lifecycle writes. The coordinator is granted **column-level**
#: ``UPDATE`` on only these, so even a permissive state-scoped ``WITH CHECK``
#: cannot let it rewrite a dispatch's tenant key, its event identity/contract,
#: its payload, its aggregate reference or other immutable dispatch data
#: (ADR-0022 decision 3 least privilege; the same pattern as group 4b's
#: settlement columns). The lifecycle columns are:
#: ``status``/``claimed_at``/``claim_token``/``attempt_count`` (claim, release,
#: stale-claim recovery), ``processed_at``/``last_error`` (settle/publish
#: outcome) and ``available_at`` (release backoff computed from the database
#: clock).
OUTBOX_COORDINATOR_UPDATE_COLUMNS = (
    "status",
    "claimed_at",
    "claim_token",
    "attempt_count",
    "processed_at",
    "last_error",
    "available_at",
)

#: ``audit_events`` append predicates (ADR-0022 decision 7). An append is
#: authorised only for a row the writer may legitimately attribute: the
#: validated tenant's own rows, a global/system row with no tenant key, or any
#: row under the validated transaction-local platform context. A tenant context
#: can therefore never attach an event to another organisation, and an unbound
#: ordinary runtime session can append only a null-tenant (global) row — never
#: a foreign tenant's. This keeps the append-only ledger writable from the
#: tenant, platform/global, webhook/bootstrap and coordinator paths while
#: making foreign-tenant attribution a policy violation even if an application
#: predicate is ever missed.
_AUDIT_RUNTIME_APPEND_CHECK = (
    "organisation_id = app_current_tenant_id() "
    "OR organisation_id IS NULL "
    "OR app_current_platform_admin()"
)
_AUDIT_COORDINATOR_APPEND_CHECK = (
    "organisation_id = app_current_tenant_id() OR organisation_id IS NULL"
)

#: Provenance recorded on the production policies for review and rollback.
POLICY_COMMENT = f"plan-p4-group6:{revision}:operational ledger isolation (ADR-0022)"
PLATFORM_POLICY_COMMENT = (
    f"plan-p4-group6:{revision}:validated platform-context cross-tenant audit read "
    "(ADR-0022 decision 4)"
)
APPEND_POLICY_COMMENT = f"plan-p4-group6:{revision}:append-only ledger write (ADR-0022 decision 7)"
COORDINATOR_POLICY_COMMENT = (
    f"plan-p4-group6:{revision}:coordinator dispatch-state access (ADR-0022 decision 3)"
)
GLOBAL_POLICY_COMMENT = (
    f"plan-p4-group6:{revision}:global operational ledger, no tenant key (ADR-0022 decision 7)"
)

#: Role-comment marker that records "this migration created the role", so a
#: downgrade never drops an adopted, deployment-provisioned operator role.
_OPERATOR_ROLE_OWNERSHIP_MARKER = f"alembic:{revision}:operational-operator"

# ``current_setting(name, true)`` returns NULL when the setting was never set.
# An absent, empty or malformed value resolves to false rather than raising, so
# every "no usable platform context" case fails closed to "no platform rows".
_CURRENT_PLATFORM_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_platform_admin() RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
DECLARE
    raw text;
BEGIN
    raw := current_setting('app.platform_admin', true);
    IF raw IS NULL OR raw = '' THEN
        RETURN false;
    END IF;
    RETURN lower(raw) IN ('true', 't', '1', 'on', 'yes');
END;
$$;
"""

#: Provision (or safely adopt) the isolated operational role. ``BYPASSRLS`` is
#: the reviewed privilege of ADR-0022 decision 4: the operator's legitimate
#: scope is exactly the cross-tenant read a policy cannot express, and it is
#: never loaded by an HTTP process or a worker. The role owns no protected
#: table and is stripped of every superuser/owner membership.
#:
#: Adoption of a deployment-provisioned role is **fully normalising**: because
#: this is the one ``BYPASSRLS`` application credential, any pre-existing
#: membership in either direction is a privilege-escalation boundary and is
#: revoked. In particular a pre-existing ``app_runtime -> app_operator`` grant
#: (which would let the ordinary runtime role ``SET ROLE`` to the bypass
#: credential) and an ``app_operator -> app_runtime`` inheritance are both
#: removed before the migration proceeds.
_PROVISION_OPERATOR_ROLE = f"""
DO $$
DECLARE
    granted_role name;
    member_role name;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{OPERATOR_ROLE}') THEN
        CREATE ROLE {OPERATOR_ROLE}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
        COMMENT ON ROLE {OPERATOR_ROLE} IS '{_OPERATOR_ROLE_OWNERSHIP_MARKER}';
        RETURN;
    END IF;

    ALTER ROLE {OPERATOR_ROLE}
        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_roles r ON r.oid = c.relowner
        WHERE r.rolname = '{OPERATOR_ROLE}'
    ) THEN
        RAISE EXCEPTION
            'pre-existing role {OPERATOR_ROLE} owns a table; '
            'refusing to treat it as the isolated operational role';
    END IF;

    -- Outgoing: roles this pre-existing operator is a member of (for example
    -- it was granted ``app_runtime``), which ``SET ROLE`` could assume.
    FOR granted_role IN
        SELECT granted.rolname
        FROM pg_auth_members m
        JOIN pg_roles granted ON granted.oid = m.roleid
        JOIN pg_roles member ON member.oid = m.member
        WHERE member.rolname = '{OPERATOR_ROLE}'
    LOOP
        EXECUTE format('REVOKE %I FROM {OPERATOR_ROLE}', granted_role);
    END LOOP;

    -- Incoming: roles that are members of this pre-existing operator (for
    -- example ``app_runtime`` was granted ``app_operator``), which would
    -- otherwise let them ``SET ROLE`` to the BYPASSRLS credential.
    FOR member_role IN
        SELECT member.rolname
        FROM pg_auth_members m
        JOIN pg_roles granted ON granted.oid = m.roleid
        JOIN pg_roles member ON member.oid = m.member
        WHERE granted.rolname = '{OPERATOR_ROLE}'
    LOOP
        EXECUTE format('REVOKE {OPERATOR_ROLE} FROM %I', member_role);
    END LOOP;
END
$$;
"""

#: Revoke this migration's operator grants **always**, and drop the role only
#: when this migration created it. An adopted, deployment-provisioned
#: credential keeps its own out-of-band attributes/login but does not retain
#: the migration-added ``USAGE``/``SELECT`` operational read surface after a
#: downgrade.
_REVOKE_AND_DROP_OWNED_OPERATOR_ROLE = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = '{OPERATOR_ROLE}'
    ) THEN
        RETURN;
    END IF;
    EXECUTE 'REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM {OPERATOR_ROLE}';
    EXECUTE 'REVOKE SELECT ON ALL SEQUENCES IN SCHEMA public FROM {OPERATOR_ROLE}';
    EXECUTE 'REVOKE USAGE ON SCHEMA public FROM {OPERATOR_ROLE}';
    IF (
        SELECT shobj_description(oid, 'pg_authid')
        FROM pg_roles
        WHERE rolname = '{OPERATOR_ROLE}'
    ) = '{_OPERATOR_ROLE_OWNERSHIP_MARKER}' THEN
        DROP ROLE {OPERATOR_ROLE};
    END IF;
END
$$;
"""


def _organisation_select_policy_sql(table: str, role: str = RUNTIME_ROLE) -> str:
    return f"""
        CREATE POLICY {table}{ORGANISATION_POLICY_SUFFIX} ON {table}
            FOR SELECT
            TO {role}
            USING (organisation_id = app_current_tenant_id())
    """


def _quoted(values: tuple[str, ...]) -> str:
    """Render a tuple of literals as a SQL ``IN`` list (trusted constants)."""
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """Enable the operational-ledger group and provision ``app_operator``."""
    op.execute(_PROVISION_OPERATOR_ROLE)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {OPERATOR_ROLE}")
    # The operational credential reads for backup/support tooling. It is never
    # granted write here: destructive recovery is an explicit, separately
    # reviewed operation. ``BYPASSRLS`` plus SELECT is what makes the
    # cross-tenant read possible without granting the ordinary runtime role a
    # bypass. Sequence ``SELECT`` is required too: ``pg_dump`` reads each
    # sequence's ``last_value``, so a table-only grant leaves the documented
    # logical backup non-executable.
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {OPERATOR_ROLE}")
    op.execute(f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {OPERATOR_ROLE}")

    op.execute(_CURRENT_PLATFORM_FUNCTION)
    op.execute(f"GRANT EXECUTE ON FUNCTION app_current_platform_admin() TO {RUNTIME_ROLE}")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON audit_events TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON outbox_events TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, UPDATE ON maintenance_runs TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON webhook_events TO {RUNTIME_ROLE}")

    # The group-4b coordinator grants are re-asserted for clarity; this
    # migration owns the coordinator policies on the operational ledgers. The
    # outbox UPDATE authority is deliberately **column-level**: the group-4b
    # table-wide UPDATE grant is revoked first, then only the claim/settle/
    # release/recovery lifecycle columns are granted, so the coordinator can
    # move a dispatch through its states but can never rewrite its tenant key,
    # event identity/contract, payload or aggregate reference (ADR-0022
    # decision 3 least privilege).
    op.execute(f"REVOKE UPDATE ON outbox_events FROM {COORDINATOR_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON outbox_events TO {COORDINATOR_ROLE}")
    op.execute(
        "GRANT UPDATE ("
        + ", ".join(OUTBOX_COORDINATOR_UPDATE_COLUMNS)
        + f") ON outbox_events TO {COORDINATOR_ROLE}"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON maintenance_runs TO {COORDINATOR_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON audit_events TO {COORDINATOR_ROLE}")

    # ``audit_events``: own-tenant read, explicit platform cross-tenant read,
    # append-only writes. No UPDATE/DELETE policy exists, so those commands stay
    # default-denied even before the append-only trigger rejects them.
    op.execute(f"DROP POLICY IF EXISTS audit_events{ORGANISATION_POLICY_SUFFIX} ON audit_events")
    op.execute(_organisation_select_policy_sql("audit_events"))
    op.execute(
        f"COMMENT ON POLICY audit_events{ORGANISATION_POLICY_SUFFIX} ON audit_events IS "
        f"'{POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {AUDIT_PLATFORM_READ_POLICY} ON audit_events")
    op.execute(
        f"""
        CREATE POLICY {AUDIT_PLATFORM_READ_POLICY} ON audit_events
            FOR SELECT
            TO {RUNTIME_ROLE}
            USING (app_current_platform_admin())
        """
    )
    op.execute(
        f"COMMENT ON POLICY {AUDIT_PLATFORM_READ_POLICY} ON audit_events IS "
        f"'{PLATFORM_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {AUDIT_APPEND_POLICY} ON audit_events")
    op.execute(
        f"""
        CREATE POLICY {AUDIT_APPEND_POLICY} ON audit_events
            FOR INSERT
            TO {RUNTIME_ROLE}
            WITH CHECK ({_AUDIT_RUNTIME_APPEND_CHECK})
        """
    )
    op.execute(
        f"COMMENT ON POLICY {AUDIT_APPEND_POLICY} ON audit_events IS '{APPEND_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {AUDIT_COORDINATOR_APPEND_POLICY} ON audit_events")
    op.execute(
        f"""
        CREATE POLICY {AUDIT_COORDINATOR_APPEND_POLICY} ON audit_events
            FOR INSERT
            TO {COORDINATOR_ROLE}
            WITH CHECK ({_AUDIT_COORDINATOR_APPEND_CHECK})
        """
    )
    op.execute(
        f"COMMENT ON POLICY {AUDIT_COORDINATOR_APPEND_POLICY} ON audit_events IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )

    # ``outbox_events``: runtime reads/appends its own tenant (or the global
    # null-tenant maintenance rows); the coordinator owns the whole dispatch
    # lifecycle. A null tenant key never admits a row to a tenant.
    op.execute(f"DROP POLICY IF EXISTS outbox_events{ORGANISATION_POLICY_SUFFIX} ON outbox_events")
    op.execute(_organisation_select_policy_sql("outbox_events"))
    op.execute(
        f"COMMENT ON POLICY outbox_events{ORGANISATION_POLICY_SUFFIX} ON outbox_events IS "
        f"'{POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {OUTBOX_APPEND_POLICY} ON outbox_events")
    op.execute(
        f"""
        CREATE POLICY {OUTBOX_APPEND_POLICY} ON outbox_events
            FOR INSERT
            TO {RUNTIME_ROLE}
            WITH CHECK (
                organisation_id = app_current_tenant_id()
                OR organisation_id IS NULL
            )
        """
    )
    op.execute(
        f"COMMENT ON POLICY {OUTBOX_APPEND_POLICY} ON outbox_events IS '{APPEND_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {OUTBOX_COORDINATOR_READ_POLICY} ON outbox_events")
    op.execute(
        f"""
        CREATE POLICY {OUTBOX_COORDINATOR_READ_POLICY} ON outbox_events
            FOR SELECT
            TO {COORDINATOR_ROLE}
            USING (true)
        """
    )
    op.execute(
        f"COMMENT ON POLICY {OUTBOX_COORDINATOR_READ_POLICY} ON outbox_events IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {OUTBOX_COORDINATOR_APPEND_POLICY} ON outbox_events")
    op.execute(
        f"""
        CREATE POLICY {OUTBOX_COORDINATOR_APPEND_POLICY} ON outbox_events
            FOR INSERT
            TO {COORDINATOR_ROLE}
            WITH CHECK (true)
        """
    )
    op.execute(
        f"COMMENT ON POLICY {OUTBOX_COORDINATOR_APPEND_POLICY} ON outbox_events IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {OUTBOX_COORDINATOR_UPDATE_POLICY} ON outbox_events")
    op.execute(
        f"""
        CREATE POLICY {OUTBOX_COORDINATOR_UPDATE_POLICY} ON outbox_events
            FOR UPDATE
            TO {COORDINATOR_ROLE}
            USING (status IN ({_quoted(OUTBOX_COORDINATOR_UPDATE_USING_STATES)}))
            WITH CHECK (status IN ({_quoted(OUTBOX_COORDINATOR_UPDATE_CHECK_STATES)}))
        """
    )
    op.execute(
        f"COMMENT ON POLICY {OUTBOX_COORDINATOR_UPDATE_POLICY} ON outbox_events IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {OUTBOX_COORDINATOR_DELETE_POLICY} ON outbox_events")
    op.execute(
        f"""
        CREATE POLICY {OUTBOX_COORDINATOR_DELETE_POLICY} ON outbox_events
            FOR DELETE
            TO {COORDINATOR_ROLE}
            USING (status = 'published')
        """
    )
    op.execute(
        f"COMMENT ON POLICY {OUTBOX_COORDINATOR_DELETE_POLICY} ON outbox_events IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )

    # ``maintenance_runs``: global infrastructure with no tenant key. The
    # runtime role (the maintenance worker) reads and settles the runs the
    # coordinator scheduled, and the coordinator schedules and recovers them.
    op.execute(f"DROP POLICY IF EXISTS {MAINTENANCE_RUNTIME_READ_POLICY} ON maintenance_runs")
    op.execute(
        f"""
        CREATE POLICY {MAINTENANCE_RUNTIME_READ_POLICY} ON maintenance_runs
            FOR SELECT
            TO {RUNTIME_ROLE}
            USING (true)
        """
    )
    op.execute(
        f"COMMENT ON POLICY {MAINTENANCE_RUNTIME_READ_POLICY} ON maintenance_runs IS "
        f"'{GLOBAL_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {MAINTENANCE_RUNTIME_UPDATE_POLICY} ON maintenance_runs")
    op.execute(
        f"""
        CREATE POLICY {MAINTENANCE_RUNTIME_UPDATE_POLICY} ON maintenance_runs
            FOR UPDATE
            TO {RUNTIME_ROLE}
            USING (true)
            WITH CHECK (true)
        """
    )
    op.execute(
        f"COMMENT ON POLICY {MAINTENANCE_RUNTIME_UPDATE_POLICY} ON maintenance_runs IS "
        f"'{GLOBAL_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {MAINTENANCE_COORDINATOR_POLICY} ON maintenance_runs")
    op.execute(
        f"""
        CREATE POLICY {MAINTENANCE_COORDINATOR_POLICY} ON maintenance_runs
            FOR ALL
            TO {COORDINATOR_ROLE}
            USING (true)
            WITH CHECK (true)
        """
    )
    op.execute(
        f"COMMENT ON POLICY {MAINTENANCE_COORDINATOR_POLICY} ON maintenance_runs IS "
        f"'{COORDINATOR_POLICY_COMMENT}'"
    )

    # ``webhook_events``: global provider-delivery dedup ledger. The webhook
    # consumer looks up and inserts one event id; no other role touches it.
    op.execute(f"DROP POLICY IF EXISTS {WEBHOOK_RUNTIME_READ_POLICY} ON webhook_events")
    op.execute(
        f"""
        CREATE POLICY {WEBHOOK_RUNTIME_READ_POLICY} ON webhook_events
            FOR SELECT
            TO {RUNTIME_ROLE}
            USING (true)
        """
    )
    op.execute(
        f"COMMENT ON POLICY {WEBHOOK_RUNTIME_READ_POLICY} ON webhook_events IS "
        f"'{GLOBAL_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {WEBHOOK_RUNTIME_APPEND_POLICY} ON webhook_events")
    op.execute(
        f"""
        CREATE POLICY {WEBHOOK_RUNTIME_APPEND_POLICY} ON webhook_events
            FOR INSERT
            TO {RUNTIME_ROLE}
            WITH CHECK (true)
        """
    )
    op.execute(
        f"COMMENT ON POLICY {WEBHOOK_RUNTIME_APPEND_POLICY} ON webhook_events IS "
        f"'{GLOBAL_POLICY_COMMENT}'"
    )

    for table in GROUP_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Reverse exactly the operational-ledger group, its helper and credential."""
    policies = {
        "audit_events": (
            f"audit_events{ORGANISATION_POLICY_SUFFIX}",
            AUDIT_PLATFORM_READ_POLICY,
            AUDIT_APPEND_POLICY,
            AUDIT_COORDINATOR_APPEND_POLICY,
        ),
        "outbox_events": (
            f"outbox_events{ORGANISATION_POLICY_SUFFIX}",
            OUTBOX_APPEND_POLICY,
            OUTBOX_COORDINATOR_READ_POLICY,
            OUTBOX_COORDINATOR_APPEND_POLICY,
            OUTBOX_COORDINATOR_UPDATE_POLICY,
            OUTBOX_COORDINATOR_DELETE_POLICY,
        ),
        "maintenance_runs": (
            MAINTENANCE_RUNTIME_READ_POLICY,
            MAINTENANCE_RUNTIME_UPDATE_POLICY,
            MAINTENANCE_COORDINATOR_POLICY,
        ),
        "webhook_events": (
            WEBHOOK_RUNTIME_READ_POLICY,
            WEBHOOK_RUNTIME_APPEND_POLICY,
        ),
    }
    for table, table_policies in policies.items():
        for policy in table_policies:
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute(f"REVOKE EXECUTE ON FUNCTION app_current_platform_admin() FROM {RUNTIME_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS app_current_platform_admin()")

    # This migration narrowed the group-4b coordinator grant on ``outbox_events``
    # from table-wide UPDATE to the lifecycle columns; the downgrade restores
    # the table-wide grant so the revision below is left exactly as it was.
    op.execute(
        "REVOKE UPDATE ("
        + ", ".join(OUTBOX_COORDINATOR_UPDATE_COLUMNS)
        + f") ON outbox_events FROM {COORDINATOR_ROLE}"
    )
    op.execute(f"GRANT UPDATE ON outbox_events TO {COORDINATOR_ROLE}")

    op.execute(_REVOKE_AND_DROP_OWNED_OPERATOR_ROLE)
