"""Append-only audit event ORM model (blueprint §29, Scope §6.1).

An audit event records one lifecycle action (``organisation.created``,
``record.deleted``, ``invitation.sent``, ...). The table is append-only by
construction: it deliberately does not mix in :class:`TimestampMixin` because
there is no ``updated_at`` column, and there is no update or delete endpoint
anywhere in the API. ``organisation_id`` and ``actor_user_id`` are nullable so
platform-wide and system events can be recorded without an org or actor
context; ``metadata`` carries the request id at minimum.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
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
    organisation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organisations.id", ondelete="SET NULL"), index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
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
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
