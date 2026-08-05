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
