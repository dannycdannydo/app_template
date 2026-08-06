"""Role-assignment service (v0.2 Scope §6.4, blueprint §9).

Assigning and removing roles on a membership is the mechanism by which
``owner``/``administrator`` members manage who can do what inside an
organisation. The permission gate (``users.manage_roles``) is applied by the
router/dependency layer; the service is transactional and validates that the
role code and the membership exist before mutating anything.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.modules.organisations.models import OrganisationMembership
from app.modules.permissions.models import MembershipRole, Role


async def list_membership_roles(
    session: AsyncSession,
    membership_id: uuid.UUID,
) -> list[Role]:
    """Return the roles currently assigned to a membership, by role code."""
    return list(
        await session.scalars(
            select(Role)
            .join(MembershipRole, MembershipRole.role_id == Role.id)
            .where(MembershipRole.membership_id == membership_id)
            .order_by(Role.code)
        )
    )


async def assign_role(
    session: AsyncSession,
    *,
    membership_id: uuid.UUID,
    role_code: str,
) -> None:
    """Assign a role to a membership; the role and membership must exist.

    An unknown role or membership is a 404. A duplicate assignment conflicts
    with the unique pair constraint and maps to a 409; the membership check
    here guarantees an ``IntegrityError`` from the insert cannot be a foreign
    key violation, so the mapping stays unambiguous.
    """
    role = await session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise NotFoundError(
            code="role_not_found",
            message="The role does not exist.",
        )
    membership = await session.scalar(
        select(OrganisationMembership).where(OrganisationMembership.id == membership_id)
    )
    if membership is None:
        raise NotFoundError(
            code="membership_not_found",
            message="The membership does not exist.",
        )
    session.add(MembershipRole(membership_id=membership_id, role_id=role.id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError(
            code="role_already_assigned",
            message="The member already holds this role.",
        ) from None


async def remove_role(
    session: AsyncSession,
    *,
    membership_id: uuid.UUID,
    role_code: str,
) -> None:
    """Remove a role from a membership; missing role or assignment are 404."""
    role = await session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise NotFoundError(
            code="role_not_found",
            message="The role does not exist.",
        )
    link = await session.scalar(
        select(MembershipRole).where(
            MembershipRole.membership_id == membership_id,
            MembershipRole.role_id == role.id,
        )
    )
    if link is None:
        raise NotFoundError(
            code="role_not_assigned",
            message="The member does not hold this role.",
        )
    await session.delete(link)
    await session.commit()
