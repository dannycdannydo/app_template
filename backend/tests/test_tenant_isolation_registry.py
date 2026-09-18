"""Structural coverage tests for the tenant-isolation registry (plan P2).

These tests do not touch a database. They enforce that the checked-in registry
in ``tenant_isolation_registry.py`` stays a complete and truthful inventory of
the schema:

- every table registered on ``Base.metadata`` is classified;
- every entry declares an isolation strategy, and directly owned entries name
  real ownership columns;
- indirect entries point at a real parent table and parent column;
- every declared column exists in the SQLAlchemy metadata.

The effect is the plan's "lightweight structural check": adding a new model
without stating its isolation strategy fails the default suite, before any
real-database matrix run.
"""

from __future__ import annotations

from tests.tenant_isolation_registry import (
    TENANT_REGISTRY,
    IsolationClass,
    metadata_table_names,
    registry_by_table,
    tenant_owned_entries,
)

from app.db.base import Base


def test_registry_covers_every_registered_table() -> None:
    """Every table in the ORM metadata appears exactly once in the registry."""
    registry_names = [entry.table_name for entry in TENANT_REGISTRY]
    assert len(registry_names) == len(set(registry_names)), "registry table names must be unique"
    assert set(registry_names) == set(metadata_table_names()), (
        "every ORM table must be classified in the tenant-isolation registry; "
        "new models must declare an isolation strategy"
    )


def test_every_entry_declares_an_isolation_strategy() -> None:
    """A classification is only valid with an explicit, human-readable reason."""
    for entry in TENANT_REGISTRY:
        assert entry.strategy.strip(), f"{entry.table_name} must state its isolation strategy"


def test_owned_entries_declare_ownership_columns() -> None:
    """Direct tenant tables must name the organisation/recipient predicate."""
    for entry in tenant_owned_entries():
        if entry.isolation_class in (
            IsolationClass.ORGANISATION_OWNED,
            IsolationClass.USER_PRIVATE,
        ):
            assert entry.ownership_columns, (
                f"{entry.table_name} is tenant-owned and must name its ownership columns"
            )
    # A user-private row is narrowed by a recipient key as well as the tenant.
    notifications = registry_by_table()["notifications"]
    assert notifications.isolation_class is IsolationClass.USER_PRIVATE
    assert "organisation_id" in notifications.ownership_columns
    assert "user_id" in notifications.ownership_columns


def test_indirect_entries_reference_a_known_parent() -> None:
    """Indirect tables must name a real parent table and a real foreign key.

    Naming a parent table/column in the registry is not enough: the declared
    link must be an actual foreign key to that parent's table, so registry
    drift (renaming a parent or pointing the column elsewhere) fails the
    structural suite instead of leaving the tenant relationship as an
    unchecked comment.
    """
    tables = Base.metadata.tables
    for entry in tenant_owned_entries():
        if entry.isolation_class is not IsolationClass.INDIRECT:
            continue
        assert entry.parent_table is not None, f"{entry.table_name} must name its parent table"
        assert entry.parent_column is not None, f"{entry.table_name} must name its parent column"
        assert entry.parent_table in tables, (
            f"{entry.table_name} references unknown parent table {entry.parent_table!r}"
        )
        assert entry.parent_column in tables[entry.table_name].columns, (
            f"{entry.table_name}.{entry.parent_column} must be a real column"
        )
        parent_refs = tables[entry.table_name].columns[entry.parent_column].foreign_keys
        assert any(fk.column.table.name == entry.parent_table for fk in parent_refs), (
            f"{entry.table_name}.{entry.parent_column} must have a foreign key to "
            f"{entry.parent_table}, not merely name it in the registry"
        )


def test_declared_ownership_columns_exist_in_metadata() -> None:
    """Registry columns must match the real schema so drift fails loudly."""
    tables = Base.metadata.tables
    for entry in TENANT_REGISTRY:
        assert entry.table_name in tables, f"{entry.table_name} is not a registered table"
        for column in entry.ownership_columns:
            assert column in tables[entry.table_name].columns, (
                f"{entry.table_name}.{column} is declared in the registry but absent from the model"
            )


def test_registry_covers_every_current_tenant_owned_table() -> None:
    """Pin the current tenant-owned set so a silent removal fails review too."""
    expected_owned = {
        "organisation_memberships",
        "invitations",
        "membership_roles",
        "records",
        "record_revisions",
        "files",
        "jobs",
        "job_attempts",
        "notifications",
        "notification_deliveries",
        "organisation_features",
        "organisation_ai_settings",
        "ai_requests",
        "ai_outputs",
        "ai_attachment_references",
        "ai_scratch_uploads",
    }
    assert {entry.table_name for entry in tenant_owned_entries()} == expected_owned
