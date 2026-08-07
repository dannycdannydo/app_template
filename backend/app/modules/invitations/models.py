"""Invitation ORM model (Scope §6.5, blueprint §8, §9, §29).

The ``invitations`` table is the application's own record of every invite —
the source of truth for the invite lifecycle and the audit trail, independent
of WorkOS delivery (design plan §2.3). WorkOS owns email delivery; the
application owns the local row, the acceptance-time membership grant and the
audit events.

No membership row is created at invite time. A membership is created only at
acceptance, by the login-time linking service (``link_invitation_on_login``),
so ``invitations`` is deliberately decoupled from ``organisation_memberships``
until the invitee actually signs in (acceptance §5.6). ``role_code`` stores the
intended organisation role as a string, validated against ``roles.code`` at
invite time and resolved to the role row at acceptance, mirroring how the
org-plane membership grants roles.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7


class InvitationStatus(enum.StrEnum):
    """Lifecycle state of an invitation (WorkOS states mirrored locally)."""

    SENT = "sent"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


def _invitation_status_values(enum_class: type[InvitationStatus]) -> list[str]:
    """Return the values a status column stores, not the enum names."""
    return [member.value for member in enum_class]


class Invitation(Base, TimestampMixin):
    """One invitation to join an organisation, delivered by WorkOS."""

    __tablename__ = "invitations"
    __table_args__ = (
        # The WorkOS invitation id is unique once assigned; PostgreSQL treats
        # NULLs as distinct, so a row that never reached WorkOS (or predates
        # the mapping) can still hold NULL while sent rows are unique.
        UniqueConstraint("workos_invitation_id", name="uq_invitations_workos_invitation_id"),
        CheckConstraint(
            "status IN ('sent', 'accepted', 'revoked', 'expired')",
            name="invitation_status",
        ),
        Index(
            "uq_invitations_pending_organisation_email",
            "organisation_id",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("status = 'sent'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    # The intended organisation role, validated against ``roles.code`` at invite
    # time and resolved to the role row at acceptance.
    role_code: Mapped[str] = mapped_column(String(64), nullable=False)
    workos_invitation_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, default=None
    )
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[InvitationStatus] = mapped_column(
        Enum(
            InvitationStatus,
            name="invitation_status",
            native_enum=False,
            length=16,
            # Persist the enum values ("sent", ...) so rows match the check
            # constraint and server default; SQLAlchemy defaults to names
            # ("SENT") for Python enums, which the constraint rejects.
            values_callable=_invitation_status_values,
        ),
        nullable=False,
        default=InvitationStatus.SENT,
        server_default=InvitationStatus.SENT.value,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @property
    def is_expired(self) -> bool:
        """True once the mirrored WorkOS expiry has passed."""
        return self.expires_at <= datetime.now(UTC)
