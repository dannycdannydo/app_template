"""Append-only audit event ORM model (blueprint §29, Scope §6.1, plan P8).

An audit event records one lifecycle action (``organisation.created``,
``record.deleted``, ``invitation.sent``, ...). The table is append-only by
construction: it deliberately does not mix in :class:`TimestampMixin` because
there is no ``updated_at`` column, there is no update or delete endpoint
anywhere in the API, and the P8 migration installs a database trigger that
rejects any UPDATE or DELETE on the table (blueprint §29 "append-only from the
application's point of view", now enforced at the database boundary).

``organisation_id`` and ``actor_user_id`` are nullable so platform-wide and
system events can be recorded without an org or actor context; ``metadata``
carries the request id at minimum.

Plan P8 removes the two foreign keys (previously ``ON DELETE SET NULL``). A
referential action is itself an UPDATE/DELETE, which the append-only trigger
must reject, and ``SET NULL`` silently erased the very actor/organisation
identity an audit trail exists to preserve. The columns are now opaque
non-foreign-key UUIDs: deleting a user or organisation leaves the event's
provenance intact. See ADR-0020 for the reviewed retention tradeoff.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import UuidV7, uuid7


class AuditEvent(Base):
    """One immutable row in the application's audit trail."""

    __tablename__ = "audit_events"
    __table_args__ = (
        # The filtered listing is the hot path, ordered newest-first; a
        # composite index serves both the org filter and the sort.
        Index("ix_audit_events_organisation_id_created_at", "organisation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    # Opaque IDs, not foreign keys: the append-only trigger forbids the
    # referential action a constraint would need, and a stable identity must
    # survive the deletion of the user/organisation it references (plan P8).
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(UuidV7, index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UuidV7, index=True)
    action: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(80), nullable=False)
    # ``metadata`` is a reserved attribute name in the SQLAlchemy declarative
    # API (it would shadow ``Base.metadata``), so the mapped attribute is
    # ``event_metadata`` while the database column keeps the blueprint §29
    # name ``metadata``.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        # Python-side default as well as the database default: under the RLS
        # operational-ledger policies (plan P4 group 6) an ``INSERT ...
        # RETURNING`` would require a SELECT policy on the new row, which an
        # append-only ledger deliberately does not grant to every writer (the
        # coordinator appends without a read path). Supplying the value client
        # side keeps the append working without granting that read.
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
