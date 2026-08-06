"""Audit event API schemas (blueprint §7, §12, Scope §6.1).

The audit listing is read-only: there are no request schemas because there is
no create, update or delete path in the API (append-only by construction).
Every shape here is an explicit response type following the standard
pagination envelope documented in API_CONVENTIONS.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditEventListItem(BaseModel):
    """One audit event in a list context."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organisation_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    action: str
    resource_type: str
    resource_id: str
    # The DB column and API field are both named ``metadata`` (blueprint §29);
    # the ORM maps it as ``event_metadata`` because ``metadata`` is reserved in
    # the declarative API, so attribute validation follows that name.
    metadata: dict[str, Any] = Field(validation_alias="event_metadata")
    created_at: datetime


class AuditEventListResponse(BaseModel):
    """The pagination envelope for the audit history listing."""

    items: list[AuditEventListItem]
    page: int
    page_size: int
    total: int
