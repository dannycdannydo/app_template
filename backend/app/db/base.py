"""Declarative base for all ORM models (blueprint §7, §10).

Every model subclasses :class:`Base` and nothing else. The naming convention
defined in ``app.db.conventions`` is attached here so all tables share
deterministic constraint names, and Alembic autogenerate compares against
``Base.metadata``.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from app.db.conventions import NAMING_CONVENTION


class Base(DeclarativeBase):
    """Root class for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Model modules are imported here, after Base is defined, so their tables are
# registered on Base.metadata for Alembic autogenerate. They cannot be imported
# at the top of this file: every model subclasses Base.
import app.modules.audit.models  # pyright: ignore[reportUnusedImport]
import app.modules.invitations.models  # pyright: ignore[reportUnusedImport]
import app.modules.organisations.models  # pyright: ignore[reportUnusedImport]
import app.modules.permissions.models  # pyright: ignore[reportUnusedImport]
import app.modules.platform_admin.models  # pyright: ignore[reportUnusedImport]
import app.modules.records.models  # pyright: ignore[reportUnusedImport]
import app.modules.users.models  # pyright: ignore[reportUnusedImport]
