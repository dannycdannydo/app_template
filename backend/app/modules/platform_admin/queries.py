"""Reusable platform-plane queries (Scope §6.2/§6.6, blueprint §9).

The platform permission check is one join over the platform role graph of a
user; it is shared by the ``require_platform_permission`` dependency and the
service tests, so it lives here rather than being inlined in either. The
default-deny rule is identical to the org plane: a permission code not granted
to any of the user's platform roles is denied.

The membership statements (Scope §6.6) are the platform listing's approved
filter — one organisation, newest first — shared by the service and the tests
so the filter column is named in exactly one place.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.organisations.models import Organisation, OrganisationMembership
from app.modules.permissions.models import MembershipRole, Permission, Role
from app.modules.platform_admin.models import (
    PlatformMembership,
    PlatformRole,
    PlatformRolePermission,
)
from app.modules.users.models import User

#: Stable namespace for the transaction-scoped advisory lock that serialises
#: every change to the set of active platform administrators (plan P7). A
#: single global key is deliberate: the invariant ("at least one enabled
#: principal holds platform_admin") is global, so revocation and webhook
#: deactivation must contend on the same lock rather than on individual rows.
PLATFORM_ADMIN_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"platform_admin.active_principal").digest()[:8], "big", signed=True
)


async def acquire_platform_admin_lock(session: AsyncSession) -> None:
    """Take the transaction-scoped advisory lock for the active-admin invariant.

    The lock is ``pg_advisory_xact_lock``, so it is released automatically when
    the caller's transaction commits or rolls back; callers must hold it across
    the read-compute-write of any change that can add or remove an active
    platform administrator (grant, revoke, webhook deactivation, break-glass
    recovery). Every such path takes it *before* any row lock so the lock
    order is consistent and cannot deadlock.
    """
    connection = await session.connection()
    await connection.execute(select(func.pg_advisory_xact_lock(PLATFORM_ADMIN_LOCK_KEY)))


def active_platform_admins_statement(
    *,
    role_id: uuid.UUID,
    exclude_user_id: uuid.UUID | None = None,
) -> Select[tuple[int]]:
    """Return a statement counting enabled users holding the platform role.

    An administrator is a recovery principal only while their ``users`` row is
    active: a disabled user cannot log in, so counting their membership would
    let a revoke remove the last usable administrator (plan P7). Optionally
    excludes one user so a caller can ask how many *other* active admins remain.
    """
    statement = (
        select(func.count())
        .select_from(PlatformMembership)
        .join(User, User.id == PlatformMembership.user_id)
        .where(
            PlatformMembership.platform_role_id == role_id,
            User.is_active.is_(True),
        )
    )
    if exclude_user_id is not None:
        statement = statement.where(PlatformMembership.user_id != exclude_user_id)
    return statement


async def count_active_platform_admins(
    session: AsyncSession,
    *,
    role_id: uuid.UUID,
    exclude_user_id: uuid.UUID | None = None,
) -> int:
    """Return the number of enabled users holding the platform role."""
    return (
        await session.scalar(
            active_platform_admins_statement(role_id=role_id, exclude_user_id=exclude_user_id)
        )
    ) or 0


async def platform_permission_codes_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> set[str]:
    """Return every platform permission code granted to a user's platform roles.

    The user's platform roles are resolved through ``platform_memberships`` and
    their bundles through ``platform_role_permissions``; a user with no
    platform memberships grants nothing (default deny).
    """
    rows = await session.scalars(
        select(Permission.code)
        .join(
            PlatformRolePermission,
            PlatformRolePermission.permission_id == Permission.id,
        )
        .join(
            PlatformMembership,
            PlatformMembership.platform_role_id == PlatformRolePermission.platform_role_id,
        )
        .where(PlatformMembership.user_id == user_id)
    )
    return set(rows.all())


def platform_role_codes_statement(user_id: uuid.UUID) -> Select[tuple[str]]:
    """Return a statement selecting a user's distinct platform role codes.

    Ordered by code for stable output; used by the /me payload so the frontend
    can gate Platform Admin Centre visibility on ``platform_roles``.
    """
    return (
        select(PlatformRole.code)
        .join(
            PlatformMembership,
            PlatformMembership.platform_role_id == PlatformRole.id,
        )
        .where(PlatformMembership.user_id == user_id)
        .distinct()
        .order_by(PlatformRole.code)
    )


def memberships_statement(*, organisation_id: uuid.UUID) -> Select[tuple[OrganisationMembership]]:
    """Return a statement selecting the memberships of one organisation."""
    return select(OrganisationMembership).where(
        OrganisationMembership.organisation_id == organisation_id
    )


def memberships_count_statement(*, organisation_id: uuid.UUID) -> Select[tuple[int]]:
    """Return a statement counting the memberships of one organisation."""
    return select(func.count()).select_from(
        memberships_statement(organisation_id=organisation_id).subquery()
    )


def users_for_ids_statement(user_ids: set[uuid.UUID]) -> Select[tuple[User]]:
    """Return the users needed to render one page of memberships."""
    return select(User).where(User.id.in_(user_ids))


def membership_roles_for_membership_ids_statement(
    membership_ids: set[uuid.UUID],
) -> Select[tuple[MembershipRole]]:
    """Return all role grants for one page of organisation memberships."""
    return select(MembershipRole).where(MembershipRole.membership_id.in_(membership_ids))


def roles_for_ids_statement(role_ids: set[uuid.UUID]) -> Select[tuple[Role]]:
    """Return the role catalogue rows referenced by one membership page."""
    return select(Role).where(Role.id.in_(role_ids))


def organisations_statement() -> Select[tuple[Organisation]]:
    """Return a statement selecting every organisation, newest first.

    The platform organisations listing (Scope §6.9) has no filter: it is the
    admin centre's catalogue over the whole tenant fleet, so the statement is
    a plain ordered select and the router's approved query parameters (page,
    page_size) are the only knobs.
    """
    return select(Organisation)


def organisations_count_statement() -> Select[tuple[int]]:
    """Return a statement counting every organisation."""
    return select(func.count()).select_from(organisations_statement().subquery())
