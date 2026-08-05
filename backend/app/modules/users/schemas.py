"""User API schemas (blueprint §7).

ORM models are never API request models; the schemas here are the explicit
response shapes for user data. Users are provisioned from validated WorkOS
sessions, so there is deliberately no user-create request schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.modules.organisations.schemas import MembershipListItem


class UserListItem(BaseModel):
    """A user in list contexts; never the full identity record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    is_active: bool
    created_at: datetime


class MeResponse(BaseModel):
    """The current user with their memberships and role codes."""

    user: UserListItem
    memberships: list[MembershipListItem]
    roles: list[str]
