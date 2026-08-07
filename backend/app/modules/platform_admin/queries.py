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

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.organisations.models import Organisation, OrganisationMembership
from app.modules.permissions.models import Permission
from app.modules.platform_admin.models import (
    PlatformMembership,
    PlatformRole,
    PlatformRolePermission,
)


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
