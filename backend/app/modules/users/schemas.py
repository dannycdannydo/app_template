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
    """An active or historic membership with its organisation's name and roles.

    ``roles`` is the role set this specific membership grants. It is the
    selected-organisation authority the frontend must use; the backend still
    enforces every permission from the validated ``X-Org-Id`` context.
    """

    id: uuid.UUID
    organisation_id: uuid.UUID
    organisation_name: str
    user_id: uuid.UUID
    status: MembershipStatus
    created_at: datetime
    roles: list[str]


class MeResponse(BaseModel):
    """The current user with their memberships, role codes and platform roles.

    ``memberships[].roles`` is the per-organisation authority. The top-level
    ``roles`` and ``platform_roles`` lists are retained for compatibility:
    ``roles`` is a union of role codes across every membership and must never
    be interpreted as authority for one selected organisation (Plan P10);
    ``platform_roles`` is empty for non-admins and the frontend uses it only to
    show or hide the Platform Admin Centre. UI awareness is cosmetic — the
    backend remains the enforcement point.
    """

    user: UserListItem
    memberships: list[MeMembershipListItem]
    roles: list[str]
    platform_roles: list[str]
