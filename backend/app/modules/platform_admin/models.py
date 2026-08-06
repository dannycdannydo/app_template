"""Platform authorisation plane ORM models (Scope §6.2, blueprint §9).

The platform plane mirrors the organisation plane exactly so it is enforced by
the same machinery rather than a flag: ``platform_roles`` are permission
bundles, ``platform_role_permissions`` grants the ``platform.*`` codes from the
shared ``permissions`` catalogue, and ``platform_memberships`` links a user to
a platform role. A user is a platform administrator exactly when a platform
membership row exists for them. The unique constraint on
``(user_id, platform_role_id)`` is the invariant that a user can hold each
platform role at most once, mirroring the org-plane membership invariant.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7


class PlatformRole(Base, TimestampMixin):
    """A named permission bundle a platform membership can hold."""

    __tablename__ = "platform_roles"

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class PlatformRolePermission(Base, TimestampMixin):
    """Grants one platform permission to one platform role."""

    __tablename__ = "platform_role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "platform_role_id",
            "permission_id",
            name="uq_platform_role_permissions_platform_role_id_permission_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    platform_role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_roles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )


class PlatformMembership(Base, TimestampMixin):
    """Links one user to one platform role; the platform-plane analogue of a
    membership."""

    __tablename__ = "platform_memberships"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "platform_role_id",
            name="uq_platform_memberships_user_id_platform_role_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    platform_role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_roles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
