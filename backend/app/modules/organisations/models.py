"""Organisation and membership ORM models (blueprint §7, §9, §10).

An organisation is the primary data-isolation and security boundary. A
membership links a user to an organisation with a status; the unique
constraint on ``(user_id, organisation_id)`` is the invariant that a user can
hold at most one membership per organisation.
"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7

if TYPE_CHECKING:
    from app.modules.users.models import User


class MembershipStatus(enum.StrEnum):
    """Lifecycle state of a user's membership in an organisation."""

    ACTIVE = "active"
    INVITED = "invited"
    SUSPENDED = "suspended"
    LEFT = "left"


class Organisation(Base, TimestampMixin):
    """A tenant boundary; all tenant-scoped data hangs off this table."""

    __tablename__ = "organisations"

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    memberships: Mapped[list[OrganisationMembership]] = relationship(
        back_populates="organisation", cascade="all, delete-orphan"
    )


class OrganisationMembership(Base, TimestampMixin):
    """A user's membership in an organisation with a lifecycle status."""

    __tablename__ = "organisation_memberships"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "organisation_id", name="uq_organisation_memberships_user_id_organisation_id"
        ),
        CheckConstraint(
            "status IN ('active', 'invited', 'suspended', 'left')",
            name="membership_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id"), index=True, nullable=False
    )
    status: Mapped[MembershipStatus] = mapped_column(
        Enum(MembershipStatus, name="membership_status", native_enum=False, length=16),
        nullable=False,
        default=MembershipStatus.ACTIVE,
        server_default=MembershipStatus.ACTIVE.value,
    )

    user: Mapped[User] = relationship(back_populates="memberships")
    organisation: Mapped[Organisation] = relationship(back_populates="memberships")
