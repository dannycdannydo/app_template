"""User ORM model (blueprint §7, §9, §10).

An internal user is the application-side record mapped from a validated WorkOS
identity. The application stores the WorkOS user identifier, never passwords;
the ``users`` table deliberately has no password column.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7

if TYPE_CHECKING:
    from app.modules.organisations.models import OrganisationMembership


class User(Base, TimestampMixin):
    """An internal user mapped one-to-one to a WorkOS identity."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    workos_user_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    memberships: Mapped[list[OrganisationMembership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
