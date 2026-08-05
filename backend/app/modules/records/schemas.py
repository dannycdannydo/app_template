"""Record API schemas (Scope §6.5, blueprint §7, §12).

ORM models are never API request models. The create and update schemas are the
only client-supplied inputs; every other schema is an explicit response shape
whose server-controlled fields (identifiers, timestamps) can never come from a
request body. ``extra="forbid"`` rejects identity fields a client might try to
smuggle in — in particular an ``organisation_id``, which is always derived
from the validated ``X-Org-Id`` context instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# A generous ceiling on the free-text body: the column is unbounded Text, but
# the API still bounds what a single request can carry. The blueprint is
# silent on payload caps, so this is the template's choice; raise it in a
# release that legitimately needs longer bodies rather than silently unlimited.
BODY_MAX_LENGTH = 100_000


class RecordCreate(BaseModel):
    """Request payload for creating a record."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    body: str = Field(default="", max_length=BODY_MAX_LENGTH)


class RecordUpdate(BaseModel):
    """Request payload for updating a record (PATCH semantics: all optional)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    body: str | None = Field(default=None, max_length=BODY_MAX_LENGTH)


class RecordListItem(BaseModel):
    """A record in list contexts; summary fields only, never the body."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class RecordDetail(RecordListItem):
    """Full record detail, including the free-text body."""

    body: str


class RecordListResponse(BaseModel):
    """The pagination envelope documented in API_CONVENTIONS.md (BP §12)."""

    items: list[RecordListItem]
    page: int
    page_size: int
    total: int
