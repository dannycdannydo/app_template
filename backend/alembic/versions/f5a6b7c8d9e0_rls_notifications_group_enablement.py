"""Production enablement of the RLS ``notifications`` table group (plan P3, group 2).

Plan P3 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). This is the third table group of ``docs/rls-rollout.md`` §3: the
user-private ``notifications`` table and its indirectly owned
``notification_deliveries`` ledger.

Unlike the direct organisation-owned groups, every notification row is scoped
to **both** an organisation and a recipient user, so the policy requires both
transaction-local keys:

- ``notifications`` uses the canonical user-private predicate
  ``organisation_id = app_current_tenant_id() AND user_id =
  app_current_user_id()`` in matching ``USING``/``WITH CHECK``;
- ``notification_deliveries`` has no tenant key of its own, so it inherits the
  parent boundary through an ``EXISTS`` on its parent notification with the same
  organisation+user predicate.

Absent, empty or malformed context resolves to ``NULL`` in
``app_current_tenant_id()`` / ``app_current_user_id()`` and therefore matches no
row and authorises no write; it never means unrestricted access.

The shared restricted ``app_runtime`` role and the ``app_current_tenant_id()``
helper are owned by the P2 prototype (``c1d2e3f4a5b6``); this migration owns the
new ``app_current_user_id()`` helper, which no earlier revision uses, and drops
it on downgrade. It grants its own tables to the runtime role and installs,
enables and forces the two policies. The application layer remains the first
enforcement layer: the notifications service keeps its org+user predicates and
a foreign or other-recipient notification stays a ``404``.

The group also installs the operationally required **non-bypass aggregate read**
(ADR-0022 decision 3): the ``app_metrics`` non-``BYPASSRLS`` role owns a
``FOR SELECT`` policy limited to ``attention_required`` delivery rows and the
``SECURITY DEFINER`` aggregate function ``app_attention_required_delivery_count()``,
which returns only a scalar count. ``app_runtime`` may execute the function but
has no cross-tenant row access, so the in-process
``attention_required_email_deliveries`` metric stays truthful without a bypass.

The downgrade removes exactly this group's policies, the user helper, ``NO
FORCE`` and ``DISABLE``s RLS, leaving earlier groups (records and files) intact.

Revision ID: f5a6b7c8d9e0
Revises: e3f4a5b6c7d8
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f5a6b7c8d9e0"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Group 2 from ``docs/rls-rollout.md`` §3.
GROUP_TABLES = ("notifications", "notification_deliveries")

#: The runtime role shared by every enabled table group (ADR-0022 decision 2).
RUNTIME_ROLE = "app_runtime"

#: User-private policy on the notification table (ADR-0022 decision 5).
NOTIFICATIONS_POLICY = "notifications_user_isolation"

#: Parent-existence policy on the indirectly owned delivery ledger.
DELIVERIES_POLICY = "notification_deliveries_parent_isolation"

#: Narrow operational policy that lets the non-bypass operational-metrics role
#: read only the attention-required delivery rows, for the aggregate count.
OPERATIONAL_METRICS_POLICY = "notification_deliveries_operational_count"

#: The dedicated non-owner, non-``BYPASSRLS`` role that owns the operational
#: metrics function and its narrow policy. It is NOLOGIN: only the
#: ``SECURITY DEFINER`` function below executes as it, so the role itself is
#: never an application credential.
METRICS_ROLE = "app_metrics"

#: The aggregate operational read the in-process metrics loop calls. It returns
#: only a scalar count, so no row content (recipient addresses, delivery
#: identity) ever leaves the database through this path (ADR-0022 decision 3).
OPERATIONAL_COUNT_FUNCTION = "app_attention_required_delivery_count"

#: Provenance recorded on the production policies for review and rollback.
POLICY_COMMENT = f"plan-p3-group2:{revision}:user-private isolation (ADR-0022)"

#: Role-comment marker that records "this migration created the role", so a
#: downgrade never drops an adopted, deployment-provisioned role.
_METRICS_ROLE_OWNERSHIP_MARKER = f"alembic:{revision}:operational-metrics"

#: Provision the operational-metrics role without embedding a credential.
_PROVISION_METRICS_ROLE = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{METRICS_ROLE}') THEN
        CREATE ROLE {METRICS_ROLE}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        COMMENT ON ROLE {METRICS_ROLE} IS '{_METRICS_ROLE_OWNERSHIP_MARKER}';
    ELSE
        ALTER ROLE {METRICS_ROLE}
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
END
$$;
"""

#: Drop the operational-metrics role only when this migration created it.
_DROP_OWNED_METRICS_ROLE = f"""
DO $$
BEGIN
    IF (
        SELECT shobj_description(oid, 'pg_authid')
        FROM pg_roles
        WHERE rolname = '{METRICS_ROLE}'
    ) = '{_METRICS_ROLE_OWNERSHIP_MARKER}' THEN
        DROP ROLE {METRICS_ROLE};
    END IF;
END
$$;
"""

# The aggregate operational read. ``SECURITY DEFINER`` executes it as the
# non-bypass ``app_metrics`` role; the role's only table privilege is SELECT on
# ``notification_deliveries`` and its only policy admits the attention-required
# rows, so the function cannot see any other delivery. ``count(*)`` means no
# row content is returned. The ``search_path`` is pinned so the function body
# cannot be redirected by a caller-controlled schema.
_OPERATIONAL_COUNT_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {OPERATIONAL_COUNT_FUNCTION}() RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT count(*) FROM public.notification_deliveries
    WHERE status = 'attention_required';
$$;
"""

# Fail-closed helper for the transaction-local user key. An absent, empty or
# non-UUID setting resolves to NULL rather than raising, so every "no usable
# context" case returns no rows. This helper is owned by this revision because
# no earlier group uses it.
_CURRENT_USER_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE
    raw text;
BEGIN
    raw := current_setting('app.user_id', true);
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

_USER_PRIVATE_PREDICATE = (
    "(organisation_id = app_current_tenant_id() AND user_id = app_current_user_id())"
)

_PARENT_EXISTENCE_PREDICATE = (
    "(EXISTS (SELECT 1 FROM notifications n "
    "WHERE n.id = notification_deliveries.notification_id "
    "AND n.organisation_id = app_current_tenant_id() "
    "AND n.user_id = app_current_user_id()))"
)


def upgrade() -> None:
    """Install the user-private and parent-existence policies for group 2."""
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(_CURRENT_USER_FUNCTION)
    op.execute(
        f"""
        DROP POLICY IF EXISTS {NOTIFICATIONS_POLICY} ON notifications
        """
    )
    op.execute(
        f"""
        CREATE POLICY {NOTIFICATIONS_POLICY} ON notifications
            FOR ALL
            USING {_USER_PRIVATE_PREDICATE}
            WITH CHECK {_USER_PRIVATE_PREDICATE}
        """
    )
    op.execute(f"COMMENT ON POLICY {NOTIFICATIONS_POLICY} ON notifications IS '{POLICY_COMMENT}'")
    op.execute(
        f"""
        DROP POLICY IF EXISTS {DELIVERIES_POLICY} ON notification_deliveries
        """
    )
    op.execute(
        f"""
        CREATE POLICY {DELIVERIES_POLICY} ON notification_deliveries
            FOR ALL
            USING {_PARENT_EXISTENCE_PREDICATE}
            WITH CHECK {_PARENT_EXISTENCE_PREDICATE}
        """
    )
    op.execute(
        f"COMMENT ON POLICY {DELIVERIES_POLICY} ON notification_deliveries IS '{POLICY_COMMENT}'"
    )
    # The operational aggregate read (plan P3, ADR-0022 decision 3). The
    # in-process metrics loop must count attention-required deliveries without
    # tenant context and without a bypass; the ``app_metrics`` role owns a
    # narrow SELECT policy and the aggregate function, and the runtime role may
    # only execute the scalar-returning function.
    op.execute(_PROVISION_METRICS_ROLE)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {METRICS_ROLE}")
    op.execute(f"GRANT SELECT ON notification_deliveries TO {METRICS_ROLE}")
    # The delivery policy's parent-existence predicate reads ``notifications``,
    # so the operational role needs the SELECT privilege there too; its own
    # user-private policy still returns no notification row without context.
    op.execute(f"GRANT SELECT ON notifications TO {METRICS_ROLE}")
    op.execute(
        f"""
        DROP POLICY IF EXISTS {OPERATIONAL_METRICS_POLICY} ON notification_deliveries
        """
    )
    op.execute(
        f"""
        CREATE POLICY {OPERATIONAL_METRICS_POLICY} ON notification_deliveries
            FOR SELECT
            TO {METRICS_ROLE}
            USING (status = 'attention_required')
        """
    )
    op.execute(_OPERATIONAL_COUNT_FUNCTION)
    op.execute(f"ALTER FUNCTION {OPERATIONAL_COUNT_FUNCTION}() OWNER TO {METRICS_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {OPERATIONAL_COUNT_FUNCTION}() TO {RUNTIME_ROLE}")
    for table in GROUP_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Remove exactly the group-2 policies, user helper and RLS enforcement."""
    op.execute(f"REVOKE EXECUTE ON FUNCTION {OPERATIONAL_COUNT_FUNCTION}() FROM {RUNTIME_ROLE}")
    op.execute(f"DROP FUNCTION IF EXISTS {OPERATIONAL_COUNT_FUNCTION}()")
    op.execute(f"DROP POLICY IF EXISTS {OPERATIONAL_METRICS_POLICY} ON notification_deliveries")
    op.execute(f"REVOKE SELECT ON notifications FROM {METRICS_ROLE}")
    op.execute(f"REVOKE SELECT ON notification_deliveries FROM {METRICS_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {METRICS_ROLE}")
    op.execute(_DROP_OWNED_METRICS_ROLE)
    op.execute(f"DROP POLICY IF EXISTS {NOTIFICATIONS_POLICY} ON notifications")
    op.execute(f"DROP POLICY IF EXISTS {DELIVERIES_POLICY} ON notification_deliveries")
    for table in GROUP_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS app_current_user_id()")
