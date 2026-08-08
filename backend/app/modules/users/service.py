"""User provisioning and identity queries (blueprint §8).

Provisioning maps a validated WorkOS identity to exactly one internal user
row. It commits eagerly so a later session for the same WorkOS user reuses the
row, and treats a unique-constraint violation as a lost race against a
concurrent first login.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ServiceUnavailableError
from app.core.security import UserProfileClient, ValidatedSession
from app.modules.organisations.models import OrganisationMembership
from app.modules.platform_admin.queries import platform_role_codes_statement
from app.modules.users.models import User
from app.modules.users.queries import (
    memberships_for_user_statement,
    role_codes_for_user_statement,
    user_by_workos_id_statement,
)


async def get_or_provision_user(
    session: AsyncSession,
    validated: ValidatedSession,
    profiles: UserProfileClient,
) -> User:
    """Return the internal user for a validated session, provisioning on first login."""
    user = await session.scalar(user_by_workos_id_statement(validated.workos_user_id))
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
        user = await session.scalar(user_by_workos_id_statement(validated.workos_user_id))
        if user is None:
            raise ServiceUnavailableError(
                code="provisioning_failed",
                message="The user could not be provisioned. Please try again.",
            ) from None
    return user


async def get_me_payload(
    session: AsyncSession,
    user: User,
) -> tuple[list[OrganisationMembership], list[str], list[str]]:
    """Return the current user's memberships, role codes and platform role codes.

    Roles are the distinct role codes across all of the user's memberships,
    ordered by code; platform roles are the distinct codes of the user's
    platform memberships (empty for non-admins). A user with no roles yields
    empty lists.
    """
    memberships = (await session.scalars(memberships_for_user_statement(user.id))).all()
    roles = (await session.scalars(role_codes_for_user_statement(user.id))).all()
    platform_roles = (await session.scalars(platform_role_codes_statement(user.id))).all()
    return list(memberships), list(roles), list(platform_roles)
