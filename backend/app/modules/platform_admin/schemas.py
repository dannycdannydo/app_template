"""Platform administration API schemas (blueprint §7, §12, Scope §6.3/§6.6).

The organisation create schema and the two membership mutation payloads are
the only user-supplied inputs; every other shape is a response. Server-
controlled fields — the internal ids, timestamps, the WorkOS mapping, the
membership status and role codes — appear only in response schemas and are
rejected from request bodies (``extra="forbid"``), so a client can never claim
or overwrite a mapping or set a lifecycle status itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.organisations.models import MembershipStatus


class PlatformOrganisationCreate(BaseModel):
    """Request payload for creating an organisation on the platform plane.

    Only the name is client-supplied; the internal id, timestamps and the
    WorkOS mapping are all server-controlled, so any attempt to smuggle them
    in is rejected outright.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


class PlatformOrganisationUpdate(BaseModel):
    """Request payload for editing an organisation's name on the platform plane.

    Mirrors ``PlatformOrganisationCreate``: only the name is client-supplied
    and every server-controlled field is rejected. The WorkOS mapping is never
    editable through the API (it is written only by the services), so the
    response to a successful edit carries the unchanged mapping.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


class PlatformOrganisationResponse(BaseModel):
    """Response payload for an organisation on the platform plane."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    workos_organisation_id: str | None
    created_at: datetime
    updated_at: datetime


class PlatformMembershipRoleAssign(BaseModel):
    """Request payload for assigning one organisation role to a membership."""

    model_config = ConfigDict(extra="forbid")

    role_code: str = Field(min_length=1, max_length=64)


class PlatformMembershipStatusUpdate(BaseModel):
    """Request payload for suspending or reactivating a membership.

    Only the two platform-settable statuses are accepted; ``invited`` and
    ``left`` are lifecycle states owned by the invite and removal flows, so a
    client can never set them directly.
    """

    model_config = ConfigDict(extra="forbid")

    status: MembershipStatus

    @field_validator("status")
    @classmethod
    def _status_must_be_platform_settable(cls, value: MembershipStatus) -> MembershipStatus:
        if value not in (MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED):
            raise ValueError("status must be 'active' or 'suspended'")
        return value


class PlatformMembershipListItem(BaseModel):
    """One membership in a platform listing or mutation response.

    Carries the member's name and email and the membership's role codes so the
    admin centre can render a memberships table without further round trips;
    ``roles`` is ordered by code for stable display.
    """

    id: uuid.UUID
    organisation_id: uuid.UUID
    user_id: uuid.UUID
    user_name: str
    user_email: str
    status: MembershipStatus
    roles: list[str]
    created_at: datetime
    updated_at: datetime


class PlatformMembershipListResponse(BaseModel):
    """The pagination envelope for the membership listing."""

    items: list[PlatformMembershipListItem]
    page: int
    page_size: int
    total: int


class PlatformOrganisationListResponse(BaseModel):
    """The pagination envelope for the platform organisations listing."""

    items: list[PlatformOrganisationResponse]
    page: int
    page_size: int
    total: int
