"""Reusable permission queries (blueprint §9, v0.2 Scope §6.4).

The default-deny permission check is one join over the role graph of a
membership; it is shared by the ``require_permission`` dependency and the
service tests, so it lives here rather than being inlined in either.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.permissions.models import MembershipRole, Permission, RolePermission


async def permission_codes_for_membership(
    session: AsyncSession,
    membership_id: uuid.UUID,
) -> set[str]:
    """Return every permission code granted to a membership's roles.

    The membership's roles are resolved through ``membership_roles`` and their
    bundles through ``role_permissions``; a membership with no roles grants
    nothing (default deny).
    """
    rows = await session.scalars(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(MembershipRole, MembershipRole.role_id == RolePermission.role_id)
        .where(MembershipRole.membership_id == membership_id)
    )
    return set(rows.all())
