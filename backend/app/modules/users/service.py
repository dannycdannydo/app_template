"""User provisioning and identity queries (blueprint §8).

Provisioning maps a validated WorkOS identity to exactly one internal user
row. It commits eagerly so a later session for the same WorkOS user reuses the
row, and treats a unique-constraint violation as a lost race against a
concurrent first login.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ServiceUnavailableError
from app.core.security import UserProfileClient, ValidatedSession
from app.modules.organisations.models import OrganisationMembership
from app.modules.users.models import User


async def get_or_provision_user(
    session: AsyncSession,
    validated: ValidatedSession,
    profiles: UserProfileClient,
) -> User:
    """Return the internal user for a validated session, provisioning on first login."""
    user = await session.scalar(select(User).where(User.workos_user_id == validated.workos_user_id))
    if user is not None:
        return user

    profile = await profiles.get_profile(validated.workos_user_id)
    user = User(
        workos_user_id=validated.workos_user_id,
        email=profile.email,
        name=profile.name,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        user = await session.scalar(
            select(User).where(User.workos_user_id == validated.workos_user_id)
        )
        if user is None:
            raise ServiceUnavailableError(
                code="provisioning_failed",
                message="The user could not be provisioned. Please try again.",
            ) from None
    return user


async def get_me_payload(
    session: AsyncSession,
    user: User,
) -> tuple[list[OrganisationMembership], list[str]]:
    """Return the current user's memberships and role codes for the /me route.

    Roles are populated by the roles and permissions work unit (Scope §6.4);
    until the role model exists the role list is empty.
    """
    memberships = (
        await session.scalars(
            select(OrganisationMembership)
            .where(OrganisationMembership.user_id == user.id)
            .order_by(OrganisationMembership.created_at)
        )
    ).all()
    return list(memberships), []
