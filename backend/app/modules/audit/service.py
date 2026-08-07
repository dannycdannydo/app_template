"""Audit event services (blueprint §29, Scope §6.1).

``record_event`` is the only write path into the audit log: an insert-only
append that never updates or deletes a row, called by the mutating services
that own the surrounding transaction (BP §11 — the service layer owns
transaction boundaries, so this function flushes but never commits). The
request id bound by the request middleware is merged into ``metadata`` so
every event can be traced back to the request that caused it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import current_request_id
from app.modules.audit.models import AuditEvent
from app.modules.audit.queries import audit_events_count_statement, audit_events_statement

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# Actions written by the v0.2/v0.3 foundation services; the rest of the
# catalogue (invitation.*, membership.*, platform.*) joins in later release
# work units.
ACTION_ORGANISATION_CREATED = "organisation.created"
ACTION_RECORD_CREATED = "record.created"
ACTION_RECORD_UPDATED = "record.updated"
ACTION_RECORD_DELETED = "record.deleted"

# The one-time platform bootstrap grant (Scope §6.4, acceptance §5.5): written
# exactly once, in the same transaction as the platform membership, so the
# audit trail always mirrors the grant.
ACTION_PLATFORM_BOOTSTRAP_GRANTED = "platform.bootstrap_granted"

# Invitation lifecycle (Scope §6.5, blueprint §29 examples ``user.invited``):
# sent at the platform invite endpoint, revoked at the platform revoke
# endpoint, accepted at login-time linking — when the membership grant that
# follows it is written, ``membership.role_changed`` (blueprint §29) records
# the intended role assignment.
ACTION_INVITATION_SENT = "invitation.sent"
ACTION_INVITATION_REVOKED = "invitation.revoked"
ACTION_INVITATION_ACCEPTED = "invitation.accepted"
ACTION_MEMBERSHIP_ROLE_CHANGED = "membership.role_changed"

# Membership administration (Scope §6.6, design plan §5): role assignment and
# removal both record ``membership.role_changed`` with the role and direction
# in metadata; suspension and reactivation are distinct actions; removal
# records ``membership.removed`` alongside the ``invitation.revoked`` rows for
# any pending invitations it cleaned up.
ACTION_MEMBERSHIP_SUSPENDED = "membership.suspended"
ACTION_MEMBERSHIP_REACTIVATED = "membership.reactivated"
ACTION_MEMBERSHIP_REMOVED = "membership.removed"

# Feature-flag management (Scope §6.7, blueprint §29 / design plan §3.2):
# written whenever a platform administrator sets an organisation's override
# for a known flag, with the feature key, organisation and new state in the
# metadata.
ACTION_FEATURE_FLAG_CHANGED = "feature_flag.changed"


async def record_event(
    session: AsyncSession,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    organisation_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Insert one append-only audit row and return it.

    The caller's request id is always stamped into ``metadata`` unless the
    caller supplied one explicitly; ``metadata`` itself is optional and
    defaults to an empty object.
    """
    event_metadata = dict(metadata or {})
    event_metadata.setdefault("request_id", current_request_id())
    event = AuditEvent(
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata=event_metadata,
    )
    session.add(event)
    await session.flush()
    return event


async def list_audit_events(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    organisation_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    action: str | None = None,
) -> tuple[list[AuditEvent], int]:
    """Return one page of audit events plus the total under the given filters.

    Newest first, ties broken by id so paging is stable. ``page`` and
    ``page_size`` are validated by the router's query parameters before the
    service is reached; the service still clamps defensively.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total = await session.scalar(
        audit_events_count_statement(
            organisation_id=organisation_id,
            actor_user_id=actor_user_id,
            action=action,
        )
    )
    rows = await session.scalars(
        audit_events_statement(
            organisation_id=organisation_id,
            actor_user_id=actor_user_id,
            action=action,
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.all()), total or 0
