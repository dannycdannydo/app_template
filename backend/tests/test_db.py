"""Tests for database conventions and the Alembic baseline (blueprint §7, §10).

Convention checks are pure Python and run everywhere; the migration smoke test
needs a reachable PostgreSQL and is skipped otherwise, so the default unit-test
run has no infrastructure dependencies.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, ForeignKey, String, Table, UniqueConstraint, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7

BACKEND_ROOT = Path(__file__).resolve().parents[1]


class _Parent(Base):
    __tablename__ = "parents"

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(80), unique=True)


class _Child(Base, TimestampMixin):
    __tablename__ = "children"

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    parent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("parents.id"), index=True)


def test_uuid7_is_a_version_7_uuid() -> None:
    value = uuid7()
    assert isinstance(value, uuid.UUID)
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_embeds_a_monotonic_millisecond_timestamp() -> None:
    earlier = uuid7()
    later = uuid7()
    # The 48-bit millisecond timestamp sits in the high bits; random data only
    # breaks ties within the same millisecond, so the timestamp never regresses.
    assert later.int >> 80 >= earlier.int >> 80
    assert earlier != later


def _table_of(model: type[Base]) -> Table:
    """Return the mapped :class:`Table` with precise typing for introspection."""
    return cast(Table, model.__table__)


def test_timestamp_mixin_adds_timezone_aware_columns() -> None:
    created = _table_of(_Child).c.created_at
    updated = _table_of(_Child).c.updated_at
    for column in (created, updated):
        assert isinstance(column.type, DateTime)
        assert column.type.timezone is True
        assert not column.nullable


def test_constraint_naming_convention_is_applied() -> None:
    child_table = _table_of(_Child)
    parent_table = _table_of(_Parent)
    assert child_table.primary_key.name == "pk_children"
    unique = next(c for c in parent_table.constraints if isinstance(c, UniqueConstraint))
    assert unique.name == "uq_parents_name"
    fk_constraint = next(iter(child_table.foreign_key_constraints))
    assert fk_constraint.name == "fk_children_parent_id_parents"
    index = next(iter(child_table.indexes))
    assert index.name == "ix_children_parent_id"


def _database_reachable(database_url: str) -> bool:
    """Probe the configured database with a short async engine connect."""

    async def _probe() -> bool:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(_probe())


def test_baseline_migration_applies_to_a_fresh_database() -> None:
    """Upgrade to head against a fresh database, then downgrade back to base.

    Requires a reachable PostgreSQL as configured by ``DATABASE_URL``; skipped
    otherwise.
    """
    import os

    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    command.downgrade(config, "base")
