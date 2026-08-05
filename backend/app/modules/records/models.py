"""Record ORM model (Scope §6.5, blueprint §7, §10, §12).

A record is the tenant-scoped example entity: every row hangs off exactly one
organisation, and every query in the module filters on ``organisation_id``
first so a record from another organisation is simply not found (404), never
visible. The organisation id always comes from the validated request context
(``X-Org-Id`` via ``get_current_membership``), never from a request body.

There is deliberately no ORM relationship to :class:`Organisation`: the
module never needs to load an organisation from a record, and relationship
loading must be deliberate (BP §7). The database-level ``ON DELETE CASCADE``
keeps the tenant boundary clean if an organisation is ever removed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7


class Record(Base, TimestampMixin):
    """An organisation-scoped note; the v0.2 example tenant-scoped entity."""

    __tablename__ = "records"
    __table_args__ = (
        # The org-scoped list is the hot path, ordered newest-first; a composite
        # index serves both the filter and the sort. The single-column index
        # declared on the column below would otherwise be redundant for it.
        Index("ix_records_organisation_id_created_at", "organisation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
