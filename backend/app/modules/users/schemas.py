"""User API schemas (blueprint §7).

ORM models are never API request models; the schemas here are the explicit
response shapes for user data. Users are provisioned from validated WorkOS
sessions, so there is deliberately no user-create request schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.modules.organisations.models import MembershipStatus


class UserListItem(BaseModel):
    """A user in list contexts; never the full identity record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    is_active: bool
    created_at: datetime


class MeMembershipListItem(BaseModel):
    """An active or historic membership, including its organisation's name."""

    id: uuid.UUID
    organisation_id: uuid.UUID
    organisation_name: str
    user_id: uuid.UUID
    status: MembershipStatus
    created_at: datetime


class MeResponse(BaseModel):
    """The current user with their memberships, role codes and platform roles.

    ``platform_roles`` is empty for non-admins; the frontend uses it only to
    show or hide the Platform Admin Centre (UI awareness is cosmetic — the
    backend remains the enforcement point).
    """

    user: UserListItem
    memberships: list[MeMembershipListItem]
    roles: list[str]
    platform_roles: list[str]
