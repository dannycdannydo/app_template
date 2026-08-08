"""Notification endpoints (Scope §6.3, blueprint §5, §6, §12, §20).

The router stays thin: it resolves the caller's membership through the shared
dependency, gates every route with ``require_permission`` (default deny), and
delegates to the service. The organisation id and the recipient user id for
every call come from the resolved membership and authenticated user — never
from the request body or path (acceptance §5.4).

Permission map: list, unread-count and mark-read need ``notifications.read``;
test-send needs ``notifications.manage`` (the manager bundle holds both, a
member holds read only, a viewer holds neither — acceptance §5.5). Every
endpoint declares an explicit response schema (BP §12).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db, require_permission
from app.modules.notifications import service
from app.modules.notifications.schemas import (
    NotificationListItem,
    NotificationListResponse,
    UnreadCountResponse,
)
from app.modules.organisations.models import OrganisationMembership
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1", tags=["notifications"])


@router.get("/notifications", response_model=NotificationListResponse)
async def list_notifications_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[
        OrganisationMembership, Depends(require_permission("notifications.read"))
    ],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
    type: Annotated[str | None, Query(max_length=80)] = None,
) -> NotificationListResponse:
    """List the caller's own notifications in the organisation, newest first.

    ``type`` is the only approved filter field (BP §12); the envelope carries
    the caller's ``unread_count`` so one request refreshes both the list and
    the header badge (acceptance §5.5).
    """
    notifications, total, unread_count = await service.list_notifications(
        session,
        organisation_id=membership.organisation_id,
        user_id=membership.user_id,
        page=page,
        page_size=page_size,
        type=type,
    )
    return NotificationListResponse(
        items=[NotificationListItem.model_validate(notification) for notification in notifications],
        page=page,
        page_size=page_size,
        total=total,
        unread_count=unread_count,
    )


@router.get("/notifications/unread-count", response_model=UnreadCountResponse)
async def unread_count_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[
        OrganisationMembership, Depends(require_permission("notifications.read"))
    ],
) -> UnreadCountResponse:
    """Return the caller's unread notification count in the organisation."""
    count = await service.unread_count(
        session,
        organisation_id=membership.organisation_id,
        user_id=membership.user_id,
    )
    return UnreadCountResponse(unread_count=count)


@router.patch("/notifications/{notification_id}/read", response_model=NotificationListItem)
async def mark_read_endpoint(
    notification_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[
        OrganisationMembership, Depends(require_permission("notifications.read"))
    ],
) -> NotificationListItem:
    """Mark one of the caller's notifications read.

    A notification that does not exist, belongs to another organisation or
    belongs to another user is a 404 (the org+user scoped lookup is the
    isolation boundary, acceptance §5.5).
    """
    notification = await service.mark_read(
        session,
        organisation_id=membership.organisation_id,
        user_id=membership.user_id,
        notification_id=notification_id,
    )
    return NotificationListItem.model_validate(notification)


@router.post("/notifications/test", response_model=NotificationListItem, status_code=201)
async def send_test_notification_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[
        OrganisationMembership, Depends(require_permission("notifications.manage"))
    ],
    user: Annotated[User, Depends(get_current_user)],
) -> NotificationListItem:
    """Create a test in-app notification for the caller and enqueue its email.

    The notification content is server-owned (fixed test copy, no request
    body); the email delivery is enqueued as a durable ``notification.email``
    job addressed to the caller's verified email. The ``notification.test_sent``
    audit event is written in the same transaction (acceptance §5.5).
    """
    notification, _delivery, _job = await service.send_test_notification(
        session,
        organisation_id=membership.organisation_id,
        user_id=membership.user_id,
        recipient_email=user.email,
        actor_user_id=user.id,
    )
    return NotificationListItem.model_validate(notification)
