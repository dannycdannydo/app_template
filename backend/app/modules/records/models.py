"""Record ORM models (v0.2 Scope §6.5, plan P8, blueprint §7, §10, §11, §12).

A record is the tenant-scoped example entity: every row hangs off exactly one
organisation, and every query in the module filters on ``organisation_id``
first so a record from another organisation is simply not found (404), never
visible. The organisation id always comes from the validated request context
(``X-Org-Id`` via ``get_current_membership``), never from a request body.

v0.2 records were last-write-wins and audited only by an action-only
``record.updated`` event; plan P8 makes the representative record
collaboratively editable:

- ``Record.version`` is the integer optimistic-concurrency field the blueprint
  §10 prescribes: every update/deletes compares the caller's version and a
  stale write is a 409 instead of silently overwriting a later writer.
- :class:`RecordRevision` is the bounded immutable history. One snapshot row
  per accepted create/update/delete holds the record's bounded API fields
  (``title``/``body``), so approved changes can be reconstructed without
  copying record content into the generic ``audit_events.metadata`` payload.

Both tables are append-only for revisions via a database trigger, and the
revision ledger deliberately keeps ``record_id``/``actor_user_id`` as opaque,
non-foreign-key UUIDs: a hard-deleted record or user must not erase the
provenance that a change happened (plan P8 reviewed retention decision). Tenant
cleanup is by ``organisation_id`` filter/query, not by cascade, for the same
reason: the append-only trigger forbids the automatic mutation a cascade would
require. See ADR-0020.

There is deliberately no ORM relationship to :class:`Organisation`: the
module never needs to load an organisation from a record, and relationship
loading must be deliberate (BP §7). The database-level ``ON DELETE CASCADE``
keeps the tenant boundary clean if an organisation is ever removed.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
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
        CheckConstraint("version > 0", name="positive_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Optimistic concurrency (BP §10): starts at 1 and increments on every
    # accepted update. The API never accepts it from a create body.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )


class RecordRevisionAction(enum.StrEnum):
    """The closed set of changes an immutable record revision can record."""

    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


def _revision_action_values(enum_class: type[RecordRevisionAction]) -> list[str]:
    """Return the values the action column stores, not the enum names."""
    return [member.value for member in enum_class]


class RecordRevision(Base):
    """One immutable snapshot of a record's bounded business fields.

    A revision is written inside the same transaction as the record change and
    its audit event. ``version`` is the record version the snapshot represents
    (the deleted record's final version for a ``deleted`` action). There is
    deliberately no ``updated_at``: a revision row is never modified, and the
    database rejects UPDATE/DELETE with the append-only trigger installed by
    the P8 migration. ``record_id`` and ``actor_user_id`` are opaque UUIDs, not
    foreign keys, so deleting a record or user preserves the history.
    """

    __tablename__ = "record_revisions"
    __table_args__ = (
        # Reconstructing one record's history scans by record id, oldest-first.
        Index("ix_record_revisions_record_id_created_at", "record_id", "created_at"),
        # The tenant-scoped history listing filters by organisation, newest-first.
        Index("ix_record_revisions_organisation_id_created_at", "organisation_id", "created_at"),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint(
            "action IN ('created', 'updated', 'deleted')",
            name="record_revision_action",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    record_id: Mapped[uuid.UUID] = mapped_column(UuidV7, nullable=False)
    organisation_id: Mapped[uuid.UUID] = mapped_column(UuidV7, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[RecordRevisionAction] = mapped_column(
        Enum(
            RecordRevisionAction,
            name="record_revision_action",
            native_enum=False,
            length=16,
            values_callable=_revision_action_values,
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Deliberately not a foreign key: actor identity survives user deletion.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UuidV7, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
