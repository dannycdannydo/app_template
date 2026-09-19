"""Production enablement of the RLS AI-data table group (plan P3, group 3).

Plan P3 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). This is the fourth table group of ``docs/rls-rollout.md`` §3: the
direct organisation-owned AI tables ``ai_requests``, ``ai_outputs``,
``ai_attachment_references`` and ``ai_scratch_uploads``.

It follows the production pattern established for group 0
(``d2e3f4a5b6c7``) and group 1 (``e3f4a5b6c7d8``). The shared restricted
``app_runtime`` role and the ``app_current_tenant_id()`` helper are owned by
the P2 prototype (``c1d2e3f4a5b6``) and are deliberately not re-created here;
this migration only:

- grants its tables/sequences to the restricted runtime role (idempotent; the
  rollout principle requires every group migration to grant its own tables);
- installs the canonical ``<table>_organisation_isolation`` policy with
  matching ``USING`` and ``WITH CHECK`` on each table; and
- ``ENABLE``s and ``FORCE``s row-level security on each table.

Absent, empty or malformed context resolves to ``NULL`` in
``app_current_tenant_id()`` and therefore matches no row and authorises no
write; it never means unrestricted access.

The application layer remains the first enforcement layer: every AI query keeps
its ``organisation_id`` predicate and a foreign request/reference/scratch row
stays a ``404``. Three global cross-tenant sweeps (stale-reservation
reconciliation and output/scratch retention, scratch-intent expiry, and
provider-file reference reconciliation) ran on the restricted runtime role
without tenant context. Because the plan forbids a universal bypass
(ADR-0022 decision 4), this group ships alongside a code change that makes
those sweeps **iterate the global, unprotected ``organisations`` table and bind
each tenant's transaction-local context** before touching its protected AI
rows. The ``ai.execute`` worker likewise binds the durable job's organisation
(ADR-0022 decision 3) and the persistence port and reference store rebind after
each of their own commits.

The downgrade removes exactly this group's policies, ``NO FORCE`` and
``DISABLE``s RLS, leaving earlier groups (including the shared
role/helper/grants owned by the prototype) intact. The DML grants are not
revoked because the group grants are idempotent additions the prototype's
``ALL TABLES`` grant already covered; no earlier revision is weakened.

Revision ID: b8c9d0e1f2a3
Revises: f5a6b7c8d9e0
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "f5a6b7c8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Group 3 from ``docs/rls-rollout.md`` §3.
GROUP_TABLES = (
    "ai_requests",
    "ai_outputs",
    "ai_attachment_references",
    "ai_scratch_uploads",
)

#: The runtime role shared by every enabled table group (ADR-0022 decision 2).
RUNTIME_ROLE = "app_runtime"

#: Production policy naming convention: ``<table>_organisation_isolation``.
PRODUCTION_POLICY_SUFFIX = "_organisation_isolation"

#: Provenance recorded on the production policies for review and rollback.
POLICY_COMMENT = f"plan-p3-group3:{revision}:organisation isolation (ADR-0022)"


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
    """Install the canonical production policies for the AI-data group."""
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
    """Remove exactly the AI-data group policies and RLS enforcement."""
    for table in GROUP_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_production_policy(table)} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
