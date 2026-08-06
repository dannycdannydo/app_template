"""Organisation creation service (v0.2 Scope §6.3, BP §9, §11).

Creation is one transaction: the organisation, the creator's active membership
and the owner role assignment either all succeed or all fail. The creator
comes from the validated auth context and the organisation id is generated
server-side; the request body only supplies the name, so identity fields are
never taken from client input.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import APIError
from app.modules.audit.service import ACTION_ORGANISATION_CREATED, record_event
from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.permissions.models import OWNER_ROLE_CODE, MembershipRole, Role
from app.modules.users.models import User


async def create_organisation(
    session: AsyncSession,
    creator: User,
    name: str,
) -> Organisation:
    """Create an organisation and make the creator its owner (transactional).

    The owner role row comes from the seed data migration; its absence means
    the migrations are out of order, which is a server-side failure rather
    than a client mistake. The audit row commits inside the same transaction,
    so a failed creation leaves no orphan audit trail either.
    """
    owner_role = await session.scalar(select(Role).where(Role.code == OWNER_ROLE_CODE))
    if owner_role is None:
        raise APIError(
            code="role_seed_missing",
            message="The default roles are not seeded.",
        )

    organisation = Organisation(name=name)
    session.add(organisation)
    await session.flush()

    membership = OrganisationMembership(
        user_id=creator.id,
        organisation_id=organisation.id,
        status=MembershipStatus.ACTIVE,
    )
    session.add(membership)
    await session.flush()

    session.add(MembershipRole(membership_id=membership.id, role_id=owner_role.id))
    await record_event(
        session,
        organisation_id=organisation.id,
        actor_user_id=creator.id,
        action=ACTION_ORGANISATION_CREATED,
        resource_type="organisation",
        resource_id=str(organisation.id),
    )
    await session.commit()
    await session.refresh(organisation)
    return organisation
