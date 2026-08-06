"""Invitation API schemas (blueprint §7, §12, Scope §6.5).

The invite request schema is the only user-supplied input in this work unit:
email plus the intended organisation role. Every server-controlled field —
the internal id, the WorkOS invitation id and expiry mirror, the actor, the
status, the timestamps — appears only in response shapes and is rejected from
request bodies (``extra="forbid"``), so a client can never claim a WorkOS
invitation id or set a lifecycle status itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.invitations.models import InvitationStatus


class InvitationCreate(BaseModel):
    """Request payload for inviting a user into an organisation."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    role_code: str = Field(min_length=1, max_length=64)


class InvitationListItem(BaseModel):
    """One invitation in a list context on the platform plane."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    email: str
    role_code: str
    workos_invitation_id: str | None
    invited_by_user_id: uuid.UUID
    status: InvitationStatus
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class InvitationListResponse(BaseModel):
    """The pagination envelope for the invitation listing."""

    items: list[InvitationListItem]
    page: int
    page_size: int
    total: int
