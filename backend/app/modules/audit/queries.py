"""Reusable audit queries (blueprint §29, §12, Scope §6.1).

The listing statement carries the approved filter fields (organisation, actor,
action) so the service and the tests share one place where the filter columns
are named; anything not in this list is rejected by the API's query-parameter
validation before the service is reached (BP §12 "only allow approved filter
and sort fields").
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select

from app.modules.audit.models import AuditEvent


def audit_events_statement(
    *,
    organisation_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    action: str | None = None,
) -> Select[tuple[AuditEvent]]:
    """Return a statement selecting audit events matching the given filters.

    All filters are optional and combined with AND; a filter left as ``None``
    does not constrain the query.
    """
    statement = select(AuditEvent)
    if organisation_id is not None:
        statement = statement.where(AuditEvent.organisation_id == organisation_id)
    if actor_user_id is not None:
        statement = statement.where(AuditEvent.actor_user_id == actor_user_id)
    if action is not None:
        statement = statement.where(AuditEvent.action == action)
    return statement


def audit_events_count_statement(
    *,
    organisation_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    action: str | None = None,
) -> Select[tuple[int]]:
    """Return a statement counting the events the filtered query would return."""
    return select(func.count()).select_from(
        audit_events_statement(
            organisation_id=organisation_id,
            actor_user_id=actor_user_id,
            action=action,
        ).subquery()
    )
