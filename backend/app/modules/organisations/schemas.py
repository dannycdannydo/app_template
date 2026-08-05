"""Organisation and membership API schemas (blueprint §7, §9).

The organisation create schema is the only user-supplied input; every other
schema is a response shape. Server-controlled fields (identifiers, timestamps,
the creator's membership) are never accepted from request bodies.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.organisations.models import MembershipStatus


class OrganisationCreate(BaseModel):
    """Request payload for creating an organisation.

    Only the name is client-supplied. Unknown fields — including any identity
    fields a client might try to smuggle in, such as an owner id — are
    rejected outright so server-controlled values can never come from a
    request body.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


class OrganisationResponse(BaseModel):
    """Response payload for an organisation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class MembershipListItem(BaseModel):
    """One membership in a list context, e.g. the current user's memberships."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    user_id: uuid.UUID
    status: MembershipStatus
    created_at: datetime


class MembershipResponse(BaseModel):
    """Full membership detail including server-controlled timestamps."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID
    user_id: uuid.UUID
    status: MembershipStatus
    created_at: datetime
    updated_at: datetime
