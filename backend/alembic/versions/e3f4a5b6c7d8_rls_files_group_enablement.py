"""Production enablement of the RLS ``files`` table group (plan P3, group 1).

Plan P3 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). This is the second table group of ``docs/rls-rollout.md`` §3: the
direct tenant ``files`` metadata table.

It follows the production pattern established for group 0
(``d2e3f4a5b6c7``). The shared restricted ``app_runtime`` role and the
``app_current_tenant_id()`` helper are owned by the P2 prototype
(``c1d2e3f4a5b6``) and are deliberately not re-created here; this migration
only:

- grants the ``files`` table to the restricted runtime role (idempotent; the
  prototype's ``ALL TABLES`` grant already covered it, but the rollout
  principle requires every group migration to grant its own tables);
- installs the canonical ``files_organisation_isolation`` policy with matching
  ``USING`` and ``WITH CHECK``; and
- ``ENABLE``s and ``FORCE``s row-level security on ``files``.

Absent, empty or malformed context resolves to ``NULL`` in
``app_current_tenant_id()`` and therefore matches no file row and authorises
no write; it never means unrestricted access.

The application layer remains the first enforcement layer: the files service
keeps its ``organisation_id`` predicate and a foreign file stays a ``404``.
The worker path binds the organisation context from the durable ``jobs`` row
via the files service before it reads the protected row, so processing still
works once the policy is enforced.

The downgrade removes exactly this group's policy, ``NO FORCE`` and
``DISABLE``s RLS, leaving earlier groups (including group 0 and the shared
role/helper/grants owned by the prototype) intact. The ``files`` DML grant is
not revoked because it predates this revision (group 0 granted all tables
present at that time), so no grant this revision added is removed.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Group 1 from ``docs/rls-rollout.md`` §3.
GROUP_TABLES = ("files",)

#: The runtime role shared by every enabled table group (ADR-0022 decision 2).
RUNTIME_ROLE = "app_runtime"

#: Production policy naming convention: ``<table>_organisation_isolation``.
PRODUCTION_POLICY_SUFFIX = "_organisation_isolation"

#: Provenance recorded on the production policies for review and rollback.
POLICY_COMMENT = f"plan-p3-group1:{revision}:organisation isolation (ADR-0022)"


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
    """Install the canonical production policy for the files group."""
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
    """Remove exactly the files-group production policy and RLS enforcement."""
    for table in GROUP_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_production_policy(table)} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
