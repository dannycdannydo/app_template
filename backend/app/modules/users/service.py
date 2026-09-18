"""User provisioning and identity queries (blueprint §8).

Provisioning maps a validated WorkOS identity to exactly one internal user
row. It commits eagerly so a later session for the same WorkOS user reuses the
row, and treats a unique-constraint violation as a lost race against a
concurrent first login.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

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
    roles_by_membership_for_user_statement,
    user_by_workos_id_statement,
)


async def get_or_provision_user(
    session: AsyncSession,
    validated: ValidatedSession,
    profiles: UserProfileClient,
) -> User:
    """Return the internal user for a validated session, provisioning on first login."""
    user = await session.scalar(user_by_workos_id_statement(validated.workos_user_id))
    profile = await profiles.get_profile(validated.workos_user_id)
    if user is not None:
        # WorkOS is the identity source of truth. Refresh a changed verified
        # email before invitation linking runs, otherwise a pending invitation
        # to the new address can never match this existing internal user.
        if profile.email_verified and (user.email != profile.email or user.name != profile.name):
            user.email = profile.email
            user.name = profile.name
            await session.commit()
        return user

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


@dataclass(frozen=True)
class MeMembership:
    """One membership and the role codes it grants for its organisation."""

    membership: OrganisationMembership
    organisation_name: str
    roles: list[str]


@dataclass(frozen=True)
class MePayload:
    """Assembled ``/me`` data with per-membership authority and compatibility roles."""

    memberships: list[MeMembership]
    roles: list[str]
    platform_roles: list[str]


async def get_me_payload(
    session: AsyncSession,
    user: User,
) -> MePayload:
    """Return the current user's memberships, per-membership roles and platform roles.

    Memberships are an explicit ``(membership, organisation_name)`` projection
    so callers never rely on the ``organisation`` relationship being loaded.
    ``MeMembership.roles`` is the role set the membership actually grants, which
    is the selected-organisation authority the frontend must use. The top-level
    ``roles`` union is retained for backward compatibility only; it spans every
    membership and must never be treated as authority for one organisation
    (Plan P10). Platform roles are the distinct codes of the user's
    platform memberships (empty for non-admins). A user with no roles yields
    empty lists.
    """
    membership_rows = (await session.execute(memberships_for_user_statement(user.id))).all()
    roles_by_membership: dict[uuid.UUID, list[str]] = {}
    for membership_id, role_code in (
        await session.execute(roles_by_membership_for_user_statement(user.id))
    ).all():
        roles_by_membership.setdefault(membership_id, []).append(role_code)
    roles = (await session.scalars(role_codes_for_user_statement(user.id))).all()
    platform_roles = (await session.scalars(platform_role_codes_statement(user.id))).all()
    return MePayload(
        memberships=[
            MeMembership(
                membership=membership,
                organisation_name=organisation_name,
                roles=roles_by_membership.get(membership.id, []),
            )
            for membership, organisation_name in membership_rows
        ],
        roles=list(roles),
        platform_roles=list(platform_roles),
    )
