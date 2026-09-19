"""Checked-in tenant-isolation registry for every ORM table (plan P2).

The organisation boundary is only as strong as the application queries that
enforce it, so this module records, for every table in ``Base.metadata``, how
that table is isolated:

- :attr:`IsolationClass.ORGANISATION_OWNED` — rows carry an
  ``organisation_id`` and every query filters on it; a foreign row is
  indistinguishable from a missing one (404).
- :attr:`IsolationClass.USER_PRIVATE` — rows are organisation-owned *and*
  recipient-scoped, so a second predicate (``user_id``) narrows them further.
- :attr:`IsolationClass.INDIRECT` — rows have no ``organisation_id`` of their
  own and inherit the tenant boundary from a parent table (for example
  ``job_attempts`` via ``jobs``).
- :attr:`IsolationClass.GLOBAL`, :attr:`IsolationClass.PLATFORM` and
  :attr:`IsolationClass.OPERATIONAL` — rows that are deliberately outside the
  tenant boundary (identity, catalogue, cross-tenant platform administration
  and infrastructure ledgers).

``test_tenant_isolation_registry.py`` fails when a table is missing from the
registry, so adding a model forces its author to declare an isolation strategy
before the suite goes green (plan P2 "lightweight structural check"). The
real-PostgreSQL matrix in ``test_org_isolation_matrix_db.py`` then proves the
declared strategies against two tenants and two authorisation planes.

This is test-support, not production code: it is the human-reviewable inventory
the plan asks for, kept next to the tests that enforce it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

# Importing ``app.db.base`` registers every model module on ``Base.metadata``;
# the registry deliberately mirrors that single source of truth.
from app.db.base import Base


class IsolationClass(enum.StrEnum):
    """How a table relates to the organisation tenant boundary."""

    ORGANISATION_OWNED = "organisation_owned"
    USER_PRIVATE = "user_private"
    INDIRECT = "indirectly_organisation_owned"
    GLOBAL = "global"
    PLATFORM = "platform_only"
    OPERATIONAL = "operational"


@dataclass(frozen=True)
class TableIsolation:
    """One table's tenant-isolation classification.

    ``ownership_columns`` names the tenant key (and, for user-private rows, the
    recipient key). ``parent_table``/``parent_column`` name the foreign key an
    indirectly owned row uses to inherit its tenant. ``strategy`` is the
    one-line reason the entry is safe, or the explicit reason it is exempt.
    """

    table_name: str
    isolation_class: IsolationClass
    strategy: str
    ownership_columns: tuple[str, ...] = ()
    parent_table: str | None = None
    parent_column: str | None = None


TENANT_REGISTRY: tuple[TableIsolation, ...] = (
    # --- Identity and control plane (global, not tenant-owned) -------------
    TableIsolation(
        "organisations",
        IsolationClass.GLOBAL,
        "Tenant root itself; accessed from the platform plane or for context "
        "validation, never scoped by another organisation_id.",
    ),
    TableIsolation(
        "users",
        IsolationClass.GLOBAL,
        "Global WorkOS identity mapped one-to-one; tenant reach is granted only "
        "through organisation_memberships.",
    ),
    TableIsolation(
        "organisation_memberships",
        IsolationClass.ORGANISATION_OWNED,
        "An active membership is the sole source of validated tenant context; "
        "every context query filters on user_id + organisation_id and requires "
        "status='active'.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "invitations",
        IsolationClass.ORGANISATION_OWNED,
        "Invitations are sent for exactly one organisation and platform-admin "
        "listings filter on organisation_id; acceptance revalidates at WorkOS.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "membership_roles",
        IsolationClass.INDIRECT,
        "No organisation_id; tenant ownership is inherited from the parent "
        "membership, so every role query joins and filters on the parent's "
        "organisation_id.",
        parent_table="organisation_memberships",
        parent_column="membership_id",
    ),
    # --- Global role/permission catalogue ----------------------------------
    TableIsolation(
        "roles",
        IsolationClass.GLOBAL,
        "Global role catalogue shared by every organisation.",
    ),
    TableIsolation(
        "permissions",
        IsolationClass.GLOBAL,
        "Global permission catalogue shared by both authorisation planes.",
    ),
    TableIsolation(
        "role_permissions",
        IsolationClass.GLOBAL,
        "Global catalogue join between roles and permissions; not tenant data.",
    ),
    # --- Direct tenant data ------------------------------------------------
    TableIsolation(
        "records",
        IsolationClass.ORGANISATION_OWNED,
        "Organisation-scoped example entity; every read/write filters on "
        "organisation_id and a foreign record is a 404.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "record_revisions",
        IsolationClass.ORGANISATION_OWNED,
        "Append-only record history carrying organisation_id; history listings "
        "filter on it and never expose another tenant's revisions.",
        ownership_columns=("organisation_id",),
        parent_table="records",
        parent_column="record_id",
    ),
    TableIsolation(
        "files",
        IsolationClass.ORGANISATION_OWNED,
        "File metadata is organisation-scoped; list/detail/download/delete all "
        "filter on organisation_id before the row is touched.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "jobs",
        IsolationClass.ORGANISATION_OWNED,
        "Durable job rows are organisation-scoped; status polling filters on "
        "organisation_id so a foreign job is a 404.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "job_attempts",
        IsolationClass.ORGANISATION_OWNED,
        "Internal attempt ledger; the plan P3 group-4b migration added a "
        "denormalised non-null organisation_id (ADR-0022 decision 6) so a "
        "direct RLS policy applies without a join. The parent job_id foreign "
        "key is retained and every read still reaches an attempt through an "
        "organisation-scoped job.",
        ownership_columns=("organisation_id",),
        parent_table="jobs",
        parent_column="job_id",
    ),
    TableIsolation(
        "notifications",
        IsolationClass.USER_PRIVATE,
        "Organisation-owned and recipient-scoped: queries filter on "
        "organisation_id + user_id, so another user's row is a 404.",
        ownership_columns=("organisation_id", "user_id"),
    ),
    TableIsolation(
        "notification_deliveries",
        IsolationClass.INDIRECT,
        "No organisation_id or user_id; ownership (and recipient scope) is "
        "inherited from the parent notification.",
        parent_table="notifications",
        parent_column="notification_id",
    ),
    TableIsolation(
        "organisation_features",
        IsolationClass.ORGANISATION_OWNED,
        "Platform-controlled per-organisation flag override keyed by organisation_id.",
        ownership_columns=("organisation_id",),
    ),
    # --- AI tenant data ----------------------------------------------------
    TableIsolation(
        "organisation_ai_settings",
        IsolationClass.ORGANISATION_OWNED,
        "One AI policy row per organisation, looked up by organisation_id and "
        "defaulting to disabled when absent.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "ai_requests",
        IsolationClass.ORGANISATION_OWNED,
        "Organisation-scoped execution rows; result lookups filter on "
        "organisation_id + request_id so a foreign id is a 404.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "ai_outputs",
        IsolationClass.ORGANISATION_OWNED,
        "Organisation-scoped outputs with a composite FK to the parent request; "
        "reads filter on organisation_id.",
        ownership_columns=("organisation_id",),
        parent_table="ai_requests",
        parent_column="ai_request_id",
    ),
    TableIsolation(
        "ai_attachment_references",
        IsolationClass.ORGANISATION_OWNED,
        "Durable transfer references are organisation-scoped and read only "
        "through organisation-filtered queries.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "ai_scratch_uploads",
        IsolationClass.ORGANISATION_OWNED,
        "Transient scratch intents are organisation-scoped; completion resolves "
        "the upload id only within the caller's organisation.",
        ownership_columns=("organisation_id",),
    ),
    # --- Audit, outbox and operational ledgers -----------------------------
    TableIsolation(
        "audit_events",
        IsolationClass.OPERATIONAL,
        "Append-only audit ledger. organisation_id is nullable because "
        "platform/system events have no tenant; tenant listings filter on the "
        "non-null value and a null row is never treated as visible to a tenant.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "outbox_events",
        IsolationClass.OPERATIONAL,
        "Transactional outbox. organisation_id is nullable for global "
        "maintenance events; tenant job dispatches copy the validated "
        "organisation id and are never read by a client.",
        ownership_columns=("organisation_id",),
    ),
    TableIsolation(
        "maintenance_runs",
        IsolationClass.OPERATIONAL,
        "Global infrastructure ledger with no tenant payload and no organisation_id by design.",
    ),
    TableIsolation(
        "webhook_events",
        IsolationClass.OPERATIONAL,
        "Global provider-delivery dedup ledger; holds no tenant or identity "
        "data beyond the provider event id and type.",
    ),
    # --- Platform authorisation plane --------------------------------------
    TableIsolation(
        "platform_roles",
        IsolationClass.PLATFORM,
        "Cross-tenant platform role catalogue; grants platform authority, never "
        "tenant-data authority.",
    ),
    TableIsolation(
        "platform_role_permissions",
        IsolationClass.PLATFORM,
        "Platform role/permission grants; resolved by the platform plane only.",
    ),
    TableIsolation(
        "platform_memberships",
        IsolationClass.PLATFORM,
        "Links a user to the platform plane; platform authority does not grant "
        "organisation membership or tenant-row access.",
    ),
    TableIsolation(
        "bootstrap_states",
        IsolationClass.PLATFORM,
        "One-time platform bootstrap record; global platform provisioning state.",
    ),
)


def registry_by_table() -> dict[str, TableIsolation]:
    """Return the registry keyed by table name."""
    return {entry.table_name: entry for entry in TENANT_REGISTRY}


def tenant_owned_entries() -> tuple[TableIsolation, ...]:
    """Return the entries inside the tenant boundary (direct, private, indirect)."""
    owned = (
        IsolationClass.ORGANISATION_OWNED,
        IsolationClass.USER_PRIVATE,
        IsolationClass.INDIRECT,
    )
    return tuple(entry for entry in TENANT_REGISTRY if entry.isolation_class in owned)


def metadata_table_names() -> frozenset[str]:
    """Return every table name registered on the shared SQLAlchemy metadata."""
    return frozenset(Base.metadata.tables)


__all__ = [
    "TENANT_REGISTRY",
    "IsolationClass",
    "TableIsolation",
    "metadata_table_names",
    "registry_by_table",
    "tenant_owned_entries",
]
