"""Production enablement of the RLS ``records`` table group (plan P3, group 0).

Plan P3 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). The P2 migration ``c1d2e3f4a5b6`` proved the design on a removable
prototype. This migration promotes the ``records`` group (group 0 of
``docs/rls-rollout.md`` §3) into the permanent, production-owned policy chain:

- it re-asserts the restricted ``app_runtime`` role and the
  ``app_current_tenant_id()`` helper idempotently. The prototype migration owns
  them today; re-asserting them is future squash/removal preparation so that a
  later chain could make this revision carry the role, but this revision is not
  standalone while its ``down_revision`` still points at the prototype and its
  downgrade restores prototype-owned state;
- it installs canonical ``<table>_organisation_isolation`` policies on
  ``records`` and ``record_revisions`` with matching ``USING`` and ``WITH
  CHECK``, replacing the prototype-named policies;
- it ``ENABLE``s and ``FORCE``s row-level security on both tables.

Absent, empty or malformed context still resolves to ``NULL`` in
``app_current_tenant_id()`` and therefore matches no row and authorises no
write; it never means unrestricted access. The upgrade is idempotent, and its
adoption branch refuses to proceed if the pre-existing runtime role can reach a
privileged role, so it is safe whether it follows the prototype or a
prototype-free chain.

The downgrade removes production policy only, restoring the prototype-named
policy so the revision below (``c1d2e3f4a5b6``) is left exactly as it was. The
shared ``app_runtime`` role, helper and grants are owned by that earlier
revision and are intentionally left in place.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Group 0 from ``docs/rls-rollout.md`` §3.
GROUP_TABLES = ("records", "record_revisions")

#: The runtime role shared by every enabled table group (ADR-0022 decision 2).
RUNTIME_ROLE = "app_runtime"

#: Production policy naming convention: ``<table>_organisation_isolation``.
#: Distinct from the prototype's ``<table>_tenant_isolation`` so the group's
#: ownership is unambiguous in ``pg_policies``.
PRODUCTION_POLICY_SUFFIX = "_organisation_isolation"

#: Prototype policy names replaced by this migration.
PROTOTYPE_POLICY_SUFFIX = "_tenant_isolation"

#: Provenance recorded on the production policies for review and rollback.
POLICY_COMMENT = f"plan-p3-group0:{revision}:organisation isolation (ADR-0022)"

# Identical fail-closed helper to the prototype: an absent, empty or
# non-UUID setting resolves to NULL rather than raising, so every "no usable
# context" case returns no rows. ``CREATE OR REPLACE`` keeps this migration
# self-contained without changing the prototype-owned definition.
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

# Provision (or safely adopt) the restricted runtime role. The prototype
# migration normally creates the role; re-asserting it here is future
# squash/removal preparation so a later chain can let this revision own it.
# Ownership of the role is deliberately *not* re-marked, so the prototype
# downgrade keeps dropping the role it created.
#
# A pre-existing role is adopted only after forcing the safe attributes and
# breaking every direct membership whose role can transitively ``SET ROLE`` to
# a superuser, a ``BYPASSRLS`` role or a protected-table owner. PostgreSQL
# resolves ``SET ROLE`` through indirect membership chains, so revoking only
# the directly-granted privileged roles would leave
# ``app_runtime -> bridge_role -> owner`` usable. If a usable path survives
# (for example a grant this migration may not revoke), the migration fails
# closed rather than adopting an escalatable role.
_PROVISION_RUNTIME_ROLE = f"""
DO $$
DECLARE
    entry_role name;
    residual name;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
        CREATE ROLE {RUNTIME_ROLE}
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    ELSE
        ALTER ROLE {RUNTIME_ROLE}
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        IF EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_roles r ON r.oid = c.relowner
            WHERE r.rolname = '{RUNTIME_ROLE}'
              AND c.relname IN ('records', 'record_revisions')
        ) THEN
            RAISE EXCEPTION
                'pre-existing role {RUNTIME_ROLE} owns a protected table; '
                'refusing to treat it as the restricted runtime role';
        END IF;
        FOR entry_role IN
            WITH RECURSIVE reachable(entry, roleid) AS (
                SELECT m.roleid, m.roleid
                FROM pg_auth_members m
                JOIN pg_roles member ON member.oid = m.member
                WHERE member.rolname = '{RUNTIME_ROLE}'
                UNION
                SELECT reachable.entry, m.roleid
                FROM reachable
                JOIN pg_auth_members m ON m.member = reachable.roleid
            )
            SELECT DISTINCT entry.rolname
            FROM reachable
            JOIN pg_roles granted ON granted.oid = reachable.roleid
            JOIN pg_roles entry ON entry.oid = reachable.entry
            WHERE granted.rolsuper
               OR granted.rolbypassrls
               OR EXISTS (
                    SELECT 1 FROM pg_class c
                    WHERE c.relowner = granted.oid
                      AND c.relname IN ('records', 'record_revisions')
               )
        LOOP
            EXECUTE format('REVOKE %I FROM {RUNTIME_ROLE}', entry_role);
        END LOOP;
        FOR residual IN
            WITH RECURSIVE reachable(roleid) AS (
                SELECT m.roleid
                FROM pg_auth_members m
                JOIN pg_roles member ON member.oid = m.member
                WHERE member.rolname = '{RUNTIME_ROLE}'
                UNION
                SELECT m.roleid
                FROM reachable
                JOIN pg_auth_members m ON m.member = reachable.roleid
            )
            SELECT DISTINCT granted.rolname
            FROM reachable
            JOIN pg_roles granted ON granted.oid = reachable.roleid
            WHERE granted.rolsuper
               OR granted.rolbypassrls
               OR EXISTS (
                    SELECT 1 FROM pg_class c
                    WHERE c.relowner = granted.oid
                      AND c.relname IN ('records', 'record_revisions')
               )
        LOOP
            RAISE EXCEPTION
                'app_runtime can still SET ROLE to privileged role %; '
                'refusing to install the runtime policy', residual;
        END LOOP;
    END IF;
END
$$;
"""


def _production_policy(table: str) -> str:
    """Return the canonical production policy name for ``table``."""
    return f"{table}{PRODUCTION_POLICY_SUFFIX}"


def _prototype_policy(table: str) -> str:
    """Return the P2 prototype policy name for ``table``."""
    return f"{table}{PROTOTYPE_POLICY_SUFFIX}"


def _policy_body(table: str, name: str) -> str:
    """Return one default-deny organisation-isolation policy statement."""
    return f"""
        CREATE POLICY {name} ON {table}
            FOR ALL
            USING (organisation_id = app_current_tenant_id())
            WITH CHECK (organisation_id = app_current_tenant_id())
    """


def upgrade() -> None:
    """Install the canonical production policies for the records group."""
    op.execute(_PROVISION_RUNTIME_ROLE)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(_CURRENT_TENANT_FUNCTION)
    for table in GROUP_TABLES:
        # Replace the prototype policy if this migration follows P2, and be
        # idempotent if the production policy already exists.
        op.execute(f"DROP POLICY IF EXISTS {_prototype_policy(table)} ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {_production_policy(table)} ON {table}")
        op.execute(_policy_body(table, _production_policy(table)))
        op.execute(
            f"COMMENT ON POLICY {_production_policy(table)} ON {table} IS '{POLICY_COMMENT}'"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Remove the production policies, restoring the prototype's group state."""
    for table in GROUP_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_production_policy(table)} ON {table}")
        # Re-instate the prototype-named policy the revision below owns, so a
        # downgrade to ``c1d2e3f4a5b6`` leaves that revision intact. The
        # shared role, helper and grants are owned there and are not touched.
        op.execute(f"DROP POLICY IF EXISTS {_prototype_policy(table)} ON {table}")
        op.execute(_policy_body(table, _prototype_policy(table)))
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
