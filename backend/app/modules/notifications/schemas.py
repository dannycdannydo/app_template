"""Notification API schemas (Scope §6.3, blueprint §7, §12).

ORM models are never API request models. There are no client-supplied
notification inputs in this release: the test-send endpoint
(``POST /api/v1/notifications/test``) takes no request body because the
notification content is server-owned — a test notification carries the
template's fixed text, and future producers (file events, Scope §6.4) build
their own content server-side. Every schema here is therefore an explicit
response shape; ``organisation_id`` and ``user_id`` never appear because both
are always derived from the validated request context.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class NotificationListItem(BaseModel):
    """One notification in list/detail contexts (the full row is public).

    ``read_at`` is ``None`` until the recipient marks the notification read;
    the ``type`` carries the dotted event name and the optional
    ``resource_type``/``resource_id`` link the notification to its subject.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    title: str
    body: str
    resource_type: str | None
    resource_id: str | None
    read_at: datetime | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    """The pagination envelope (BP §12) plus the caller's unread count.

    The ``unread_count`` sits on the envelope so a single list request can
    refresh both the list and the header badge; it is scoped to the same
    caller/org pair as the items (acceptance §5.5).
    """

    items: list[NotificationListItem]
    page: int
    page_size: int
    total: int
    unread_count: int


class UnreadCountResponse(BaseModel):
    """The unread-count endpoint's explicit response shape."""

    unread_count: int


class MarkAllReadResponse(BaseModel):
    """The number of the caller's unread notifications marked read."""

    marked_count: int
