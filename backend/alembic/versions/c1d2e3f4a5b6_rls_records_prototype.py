"""Install the P2 RLS prototype role and policies on records tables.

Plan P2 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). This migration is deliberately a removable prototype:

- it adds a dedicated non-owner, non-superuser, non-``BYPASSRLS`` runtime role
  (``app_runtime``) and its privileges;
- it ``ENABLE``s and ``FORCE``s row-level security on ``records`` and
  ``record_revisions``;
- it installs default-deny ``FOR ALL`` policies whose ``USING`` and ``WITH
  CHECK`` both require the transaction-local ``app.organisation_id`` setting to
  match the row's ``organisation_id``.

Absent, empty or malformed context resolves to ``NULL`` in
``app_current_tenant_id()`` and therefore matches no row and authorises no
write; it never means unrestricted access. Production enablement is a
separate, later, human-reviewed migration; this one is reversed cleanly by its
downgrade.

The role is created ``NOLOGIN`` so no credential is embedded in a migration.
A deployment (or the focused P2 test) separately grants a login credential to
the role; ``DATABASE_URL`` remains the schema-owner/migration credential while
``DATABASE_RUNTIME_URL`` points the application at ``app_runtime``.

Two-stage provisioning is handled explicitly rather than assumed. If
``app_runtime`` already exists (for example a deployment pre-provisioned it
with a login credential), the migration adopts it only after forcing the safe
attributes and removing any membership that could escalate to a privileged
role, and it records **ownership** of the role with a revision comment only
when it creates the role itself. The downgrade then drops the role only when
this migration created it: an adopted, deployment-managed role and its
out-of-band credential are left in place (with the prototype grants, policies
and function removed).

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The illustrative prototype runtime role from ADR-0022 decision 2.
PROTOTYPE_RUNTIME_ROLE = "app_runtime"

#: The tables this prototype protects with enforced RLS.
PROTOTYPE_TABLES = ("records", "record_revisions")

#: Role-comment marker that records "this migration created the role". The
#: downgrade drops the role only when the marker is present, so a role that a
#: deployment pre-provisioned (with its own login credential) is never dropped.
_ROLE_OWNERSHIP_MARKER = f"alembic:{revision}"

# ``current_setting(name, true)`` returns NULL when the setting was never set.
# An empty string (set by ``clear_organisation_context``) or a malformed value
# resolves to NULL here rather than raising, so every "no usable context" case
# fails closed to "no rows" instead of failing open.
_CURRENT_TENANT_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_tenant_id() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE
    raw text;
BEGIN
    raw := current_setting('app.organisation_id', true);
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

#: Provision (or safely adopt) the restricted runtime role.
#:
#: - absent: create it ``NOLOGIN`` with the safe attribute set and mark this
#:   migration as its owner;
#: - present: force the safe attributes, refuse if it owns a protected table,
#:   and revoke every membership that could let it ``SET ROLE`` to a
#:   superuser, a ``BYPASSRLS`` role or a protected-table owner. The role is
#:   *not* marked as migration-owned, so the downgrade leaves it and its
#:   credential in place.
_PROVISION_RUNTIME_ROLE = f"""
DO $$
DECLARE
    privileged_role name;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PROTOTYPE_RUNTIME_ROLE}') THEN
        CREATE ROLE {PROTOTYPE_RUNTIME_ROLE}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        COMMENT ON ROLE {PROTOTYPE_RUNTIME_ROLE} IS '{_ROLE_OWNERSHIP_MARKER}';
    ELSE
        ALTER ROLE {PROTOTYPE_RUNTIME_ROLE}
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        IF EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_roles r ON r.oid = c.relowner
            WHERE r.rolname = '{PROTOTYPE_RUNTIME_ROLE}'
              AND c.relname IN ('records', 'record_revisions')
        ) THEN
            RAISE EXCEPTION
                'pre-existing role {PROTOTYPE_RUNTIME_ROLE} owns a protected table; '
                'refusing to treat it as the restricted runtime role';
        END IF;
        FOR privileged_role IN
            SELECT DISTINCT r.rolname
            FROM pg_roles r
            WHERE r.rolname <> '{PROTOTYPE_RUNTIME_ROLE}'
              AND (
                    r.rolsuper
                 OR r.rolbypassrls
                 OR EXISTS (
                    SELECT 1 FROM pg_class c
                    WHERE c.relowner = r.oid
                      AND c.relname IN ('records', 'record_revisions')
                 )
              )
        LOOP
            EXECUTE format('REVOKE %I FROM {PROTOTYPE_RUNTIME_ROLE}', privileged_role);
        END LOOP;
    END IF;
END
$$;
"""

#: Drop the runtime role only when this migration created it.
_DROP_OWNED_RUNTIME_ROLE = f"""
DO $$
BEGIN
    IF (
        SELECT shobj_description(oid, 'pg_authid')
        FROM pg_roles
        WHERE rolname = '{PROTOTYPE_RUNTIME_ROLE}'
    ) = '{_ROLE_OWNERSHIP_MARKER}' THEN
        DROP ROLE {PROTOTYPE_RUNTIME_ROLE};
    END IF;
END
$$;
"""


def upgrade() -> None:
    """Add the prototype runtime role, its grants, RLS and both policies."""
    op.execute(_PROVISION_RUNTIME_ROLE)
    # The runtime role needs ordinary DML on the schema so the application can
    # function; RLS is what constrains the protected tables. Only the current
    # tables exist at this head revision, so ``ALL TABLES`` is complete for the
    # prototype. Tables added by later table-group rollouts must join the
    # grants in their own migration (P3/P4).
    op.execute(f"GRANT USAGE ON SCHEMA public TO {PROTOTYPE_RUNTIME_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        f"TO {PROTOTYPE_RUNTIME_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {PROTOTYPE_RUNTIME_ROLE}")
    op.execute(_CURRENT_TENANT_FUNCTION)
    for table in PROTOTYPE_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
                FOR ALL
                USING (organisation_id = app_current_tenant_id())
                WITH CHECK (organisation_id = app_current_tenant_id())
            """
        )


def downgrade() -> None:
    """Remove the policies, RLS enforcement, grants and an owned prototype role."""
    for table in PROTOTYPE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant_id()")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {PROTOTYPE_RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {PROTOTYPE_RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {PROTOTYPE_RUNTIME_ROLE}")
    op.execute(_DROP_OWNED_RUNTIME_ROLE)
