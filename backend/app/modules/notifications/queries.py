"""Reusable org+user-scoped notification queries (Scope §6.3, blueprint §20).

Every notification query filters on ``organisation_id`` and ``user_id`` first
— the notification's two isolation boundaries — so a notification that exists
for another organisation or another recipient is simply not matched and the
service surfaces it as a 404, never a leak. These statements are the single
source of that scoping, exactly like ``app.modules.records.queries`` for the
record tenant boundary. ``type`` is the only approved filter field (BP §12).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select

from app.modules.notifications.models import Notification


def user_notifications_statement(
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    type: str | None = None,
) -> Select[tuple[Notification]]:
    """Return the org+user-scoped select shared by every notification query.

    ``type`` is an approved filter field (BP §12): when given, only
    notifications of that type are matched.
    """
    statement = select(Notification).where(
        Notification.organisation_id == organisation_id,
        Notification.user_id == user_id,
    )
    if type is not None:
        statement = statement.where(Notification.type == type)
    return statement


def user_notifications_count_statement(
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    type: str | None = None,
) -> Select[tuple[int]]:
    """Return the org+user-scoped count for pagination envelopes."""
    statement = (
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.organisation_id == organisation_id,
            Notification.user_id == user_id,
        )
    )
    if type is not None:
        statement = statement.where(Notification.type == type)
    return statement


def unread_notifications_count_statement(
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Select[tuple[int]]:
    """Return the caller's unread notification count in the organisation.

    The composite ``(organisation_id, user_id, read_at)`` index serves exactly
    this filter: the org+user prefix narrows the scan and the ``read_at IS
    NULL`` condition is a range predicate on the index's trailing column.
    """
    return (
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.organisation_id == organisation_id,
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
    )
