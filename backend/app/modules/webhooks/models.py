"""Processed WorkOS webhook delivery ledger (plan P1, v0.4 Scope §6.8, BP §30).

The webhook consumer is best-effort and never authoritative for grants
(login-time reconciliation owns that), but duplicate delivery must still be a
deterministic no-op rather than re-running a refresh. Persisting every verified
provider event id with a uniqueness constraint makes a redelivery of the same
event fail the insert and stop before any handler runs, so a WorkOS retry can
never re-apply a mutation or write a second audit row.

This is an append-only-in-practice ledger: rows are written once, there is no
update or delete path, and it deliberately holds only the provider event id,
type and receipt time. No webhook payload, token, signature or secret is ever
stored here (BP §28, §30).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import UuidV7, uuid7


class WebhookEvent(Base):
    """One signature-verified WorkOS webhook event id already processed."""

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_webhook_events_event_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
