"""Platform administration API schemas (blueprint §7, §12, Scope §6.3).

The organisation create schema is the only user-supplied input in this work
unit; every other shape is a response. The ``workos_organisation_id`` mapping
is server-controlled: it appears only in response schemas and is rejected from
request bodies (``extra="forbid"``), so a client can never claim or overwrite
a mapping (design plan §9 item 6).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PlatformOrganisationCreate(BaseModel):
    """Request payload for creating an organisation on the platform plane.

    Only the name is client-supplied; the internal id, timestamps and the
    WorkOS mapping are all server-controlled, so any attempt to smuggle them
    in is rejected outright.
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
