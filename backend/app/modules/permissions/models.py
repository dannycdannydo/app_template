"""Role, permission and membership-role ORM models (blueprint §9, §10).

Roles are permission bundles attached to memberships through the
``membership_roles`` join table. The five default roles come from blueprint §9
(``owner``, ``administrator``, ``manager``, ``member``, ``viewer``); they are
seeded by the data migration that creates these tables so a freshly migrated
database always has the ``owner`` role the organisation service assigns to a
creator. The permission catalogue (``permissions``) and the per-role grants
(``role_permissions``) are seeded by the data migration that creates those
tables, from the catalogue in ``app.modules.permissions.constants``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7

# The stable code of the role assigned to an organisation's creator. Kept in
# sync with the seeded row in the roles migration and with the default role set
# from blueprint §9.
OWNER_ROLE_CODE = "owner"


class Role(Base, TimestampMixin):
    """A named permission bundle a membership can hold."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Permission(Base, TimestampMixin):
    """A named action a role can be granted; the atomic unit of authorisation.

    The catalogue is seeded by the data migration from
    :mod:`app.modules.permissions.constants`; enforcement is default deny, so a
    permission code not granted to any of the caller's roles is denied.
    """

    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class RolePermission(Base, TimestampMixin):
    """Grants one permission to one role, building the role's bundle."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "role_id", "permission_id", name="uq_role_permissions_role_id_permission_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )


class MembershipRole(Base, TimestampMixin):
    """Links one membership to one role; a membership can hold many roles."""

    __tablename__ = "membership_roles"
    __table_args__ = (
        UniqueConstraint(
            "membership_id", "role_id", name="uq_membership_roles_membership_id_role_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    membership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisation_memberships.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
