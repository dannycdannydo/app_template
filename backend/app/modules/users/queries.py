"""Reusable user identity queries (blueprint §9).

The user's organisation memberships, their distinct role codes across those
memberships, and the lookup of an internal user by WorkOS identity are shared
by the users service (the ``/me`` payload and provisioning). They live here so
the join over the role graph is named in one place, matching the sibling
``permissions`` and ``platform_admin`` query modules.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select

from app.modules.organisations.models import OrganisationMembership
from app.modules.permissions.models import MembershipRole, Role
from app.modules.users.models import User


def user_by_workos_id_statement(workos_user_id: str) -> Select[tuple[User]]:
    """Return a statement selecting the internal user for a WorkOS identity."""
    return select(User).where(User.workos_user_id == workos_user_id)


def memberships_for_user_statement(user_id: uuid.UUID) -> Select[tuple[OrganisationMembership]]:
    """Return a statement selecting a user's organisation memberships, oldest first."""
    return (
        select(OrganisationMembership)
        .where(OrganisationMembership.user_id == user_id)
        .order_by(OrganisationMembership.created_at)
    )


def role_codes_for_user_statement(user_id: uuid.UUID) -> Select[tuple[str]]:
    """Return a statement selecting a user's distinct role codes, ordered by code.

    Roles are resolved through ``membership_roles`` and the membership's owning
    user, so the join spans the role graph of every organisation the user
    belongs to. Duplicates across memberships are collapsed with ``distinct``.
    """
    return (
        select(Role.code)
        .join(MembershipRole, MembershipRole.role_id == Role.id)
        .join(
            OrganisationMembership,
            OrganisationMembership.id == MembershipRole.membership_id,
        )
        .where(OrganisationMembership.user_id == user_id)
        .distinct()
        .order_by(Role.code)
    )
