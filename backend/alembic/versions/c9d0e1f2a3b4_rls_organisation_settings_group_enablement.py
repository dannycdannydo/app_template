"""Production enablement of the RLS organisation-settings table group (P3, group 4a).

Plan P3 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). Group 4 of ``docs/rls-rollout.md`` §3 is "jobs and organisation
settings". The plan's implementation review split it into two bounded work
units: **4a** (this migration) enables the two direct organisation-owned
configuration tables ``organisation_features`` and
``organisation_ai_settings``; **4b** enables ``jobs``/``job_attempts`` together
with the non-bypass outbox-coordinator mechanism those tables require.

It follows the production pattern established for groups 0-3
(``d2e3f4a5b6c7``, ``e3f4a5b6c7d8``, ``f5a6b7c8d9e0``, ``b8c9d0e1f2a3``). The
shared restricted ``app_runtime`` role and the ``app_current_tenant_id()``
helper are owned by the P2 prototype (``c1d2e3f4a5b6``); this migration only:

- grants its tables/sequences to the restricted runtime role (idempotent; the
  rollout principle requires every group migration to grant its own tables);
- installs the canonical ``<table>_organisation_isolation`` policy with
  matching ``USING`` and ``WITH CHECK`` on each table; and
- ``ENABLE``s and ``FORCE``s row-level security on each table.

Absent, empty or malformed context resolves to ``NULL`` in
``app_current_tenant_id()`` and therefore matches no row and authorises no
write; it never means unrestricted access.

Access paths. Both tables are organisation-owned but are managed from the
**platform plane** (``/api/v1/platform/feature-flags`` and
``/api/v1/platform/organisations/{id}/ai-settings``) as well as read from the
tenant plane (feature-flag enforcement in the records service, AI policy
enforcement in the AI service). The application layer remains the first
enforcement layer: every read keeps its ``organisation_id`` predicate. The
platform routes target exactly one organisation and, after the platform
permission dependency validates the caller, bind that organisation's
transaction-local context before touching its settings row — an explicit,
per-organisation platform path, never a universal bypass (ADR-0022 decision 4).
The organisation-creation paths bind the newly created organisation before
writing its default settings row. Those bindings ship with this migration.

The downgrade removes exactly this group's policies, ``NO FORCE`` and
``DISABLE``s RLS, leaving earlier groups (including the shared
role/helper/grants owned by the prototype) intact. The DML grants are not
revoked because the group grants are idempotent additions the prototype's
``ALL TABLES`` grant already covered; no earlier revision is weakened.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Group 4a from ``docs/rls-rollout.md`` §3 after the implementation-review split.
GROUP_TABLES = (
    "organisation_features",
    "organisation_ai_settings",
)

#: The runtime role shared by every enabled table group (ADR-0022 decision 2).
RUNTIME_ROLE = "app_runtime"

#: Production policy naming convention: ``<table>_organisation_isolation``.
PRODUCTION_POLICY_SUFFIX = "_organisation_isolation"

#: Provenance recorded on the production policies for review and rollback.
POLICY_COMMENT = f"plan-p3-group4a:{revision}:organisation isolation (ADR-0022)"


def _production_policy(table: str) -> str:
    """Return the canonical production policy name for ``table``."""
    return f"{table}{PRODUCTION_POLICY_SUFFIX}"


def _policy_body(table: str, name: str) -> str:
    """Return one default-deny organisation-isolation policy statement."""
    return f"""
        CREATE POLICY {name} ON {table}
            FOR ALL
            USING (organisation_id = app_current_tenant_id())
            WITH CHECK (organisation_id = app_current_tenant_id())
    """


def upgrade() -> None:
    """Install the canonical production policies for the organisation-settings group."""
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}")
    for table in GROUP_TABLES:
        policy = _production_policy(table)
        # Idempotent if the policy already exists at this revision.
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(_policy_body(table, policy))
        op.execute(f"COMMENT ON POLICY {policy} ON {table} IS '{POLICY_COMMENT}'")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Remove exactly the organisation-settings group policies and RLS enforcement."""
    for table in GROUP_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_production_policy(table)} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
