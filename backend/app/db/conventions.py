"""Database conventions shared by every model (blueprint §7, §10).

Centralises the non-negotiables each table inherits:

- the constraint naming convention (deterministic, greppable names);
- timezone-aware UTC ``created_at`` / ``updated_at`` columns;
- a UUIDv7 primary key type and generator.

New models must not reimplement any of this; they subclass ``Base`` and pick up
the conventions through the pieces defined here.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

# Deterministic constraint names derived from table and column names instead of
# database-generated ones, so constraints are stable across environments and
# easy to find in errors and migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUIDv7 (RFC 9562) generated from the current time.

    Bit layout: 48-bit Unix-epoch millisecond timestamp, 4-bit version
    ``0b0111``, 12 random bits, 2-bit variant ``0b10``, and 62 further random
    bits. Values sort chronologically, keeping B-tree index writes sequential,
    while remaining opaque to external callers.
    """
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return uuid.UUID(int=value)


class UuidV7(Uuid[uuid.UUID]):
    """PostgreSQL ``UUID`` column that stores UUIDv7 values.

    Declare primary keys as::

        id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    """

    def __init__(self) -> None:
        super().__init__(as_uuid=True, native_uuid=True)


class TimestampMixin:
    """Adds timezone-aware ``created_at`` and ``updated_at`` columns.

    Both default to the database clock so rows always carry timestamps even
    when the application forgets to set them; ``updated_at`` is refreshed by
    the ORM on every UPDATE.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
