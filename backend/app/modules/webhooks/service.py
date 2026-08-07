"""Best-effort WorkOS webhook consumer (Scope §6.8, design plan §9.9).

The consumer refreshes local state from verified WorkOS deliveries but never
grants anything: the login-time reconciliation (Scope §6.5) is the single
authoritative grant path, so a revoked or expired invitation stays ungranted
and a delivery that never arrives (or a webhook outage) never blocks a
legitimate invitation. Two best-effort refreshes exist:

- ``invitation.revoked`` mirrors a WorkOS-side revocation onto the local
  ``invitations`` row (``sent`` → ``revoked``) and audits it; an already
  terminal invitation is left untouched.
- ``user.updated`` refreshes the local email used for invitation matching.
- ``user.deleted`` deactivates the internal user defensively — their WorkOS
  sessions are already gone, and the deactivated flag additionally blocks any
  still-valid cached session (BP §8 "disabled users must be blocked").

``invitation.created`` / ``invitation.accepted`` and ``user.created`` are
deliberate no-ops: the local invitation row is created authoritatively at
invite time and the local ``accepted`` status is written only by the grant
path (flipping it on a webhook would silently prevent the grant). Unknown
event types are tolerated and ignored (acceptance §5.9).
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import (
    ACTION_INVITATION_REVOKED,
    ACTION_USER_DEACTIVATED,
    record_event,
)
from app.modules.invitations.models import Invitation, InvitationStatus
from app.modules.users.models import User
from app.modules.webhooks.schemas import (
    InvitationEventData,
    UserEventData,
    WorkOSWebhookEvent,
)

logger = structlog.get_logger()


def _lenient_data[T: BaseModel](event: WorkOSWebhookEvent, model: type[T]) -> T | None:
    """Validate a delivery's ``data`` object; a malformed one is a no-op.

    The consumer is best-effort by design, so an otherwise-verified delivery
    with an unusable payload is tolerated (returned as None) instead of being
    rejected — the login-time reconciliation stays authoritative regardless.
    """
    try:
        return model.model_validate(event.data)
    except ValidationError:
        return None


async def process_webhook_event(session: AsyncSession, event: WorkOSWebhookEvent) -> bool:
    """Apply one verified delivery's best-effort refresh; True if state changed.

    Every handled mutation commits inside the service (BP §11 — services own
    transaction boundaries). A delivery that changes nothing (unknown event,
    missing identifier, already-terminal state, unknown local row) is a no-op
    and still acknowledged by the endpoint.
    """
    if event.event == "invitation.revoked":
        return await _refresh_revoked_invitation(session, event)
    if event.event == "user.deleted":
        return await _deactivate_deleted_user(session, event)
    if event.event == "user.updated":
        return await _refresh_updated_user(session, event)
    logger.info(
        "webhook_event_ignored",
        event_type=event.event,
        workos_event_id=event.id,
    )
    return False


async def _refresh_revoked_invitation(session: AsyncSession, event: WorkOSWebhookEvent) -> bool:
    """Mirror a WorkOS-side revocation onto the local invitation row.

    Matched by ``workos_invitation_id`` (the stable WorkOS identifier stored at
    invite time). Only a ``sent`` row is flipped — accepted, revoked or expired
    are terminal and never rewritten by a webhook. The revocation is audited
    with a null actor (system-driven) and the WorkOS event id in the metadata
    so the trail says *why* the invitation was revoked.
    """
    data = _lenient_data(event, InvitationEventData)
    if data is None or data.id is None:
        return False
    invitation = await session.scalar(
        select(Invitation).where(Invitation.workos_invitation_id == data.id)
    )
    if invitation is None or invitation.status is not InvitationStatus.SENT:
        return False

    invitation.status = InvitationStatus.REVOKED
    await record_event(
        session,
        organisation_id=invitation.organisation_id,
        action=ACTION_INVITATION_REVOKED,
        resource_type="invitation",
        resource_id=str(invitation.id),
        metadata={"source": "webhook", "workos_event_id": event.id},
    )
    await session.commit()
    logger.info("webhook_invitation_revoked", invitation_id=str(invitation.id))
    return True


async def _deactivate_deleted_user(session: AsyncSession, event: WorkOSWebhookEvent) -> bool:
    """Deactivate the internal user whose WorkOS account was deleted.

    Matched by ``workos_user_id``. Already-inactive or unknown users are left
    untouched (idempotent redeliveries change nothing). The deactivation is
    audited with a null actor and marked ``source: webhook`` so platform
    administrators can distinguish it from an admin action.
    """
    data = _lenient_data(event, UserEventData)
    if data is None or data.id is None:
        return False
    user = await session.scalar(select(User).where(User.workos_user_id == data.id))
    if user is None or not user.is_active:
        return False

    user.is_active = False
    await record_event(
        session,
        action=ACTION_USER_DEACTIVATED,
        resource_type="user",
        resource_id=str(user.id),
        metadata={
            "source": "webhook",
            "workos_event_id": event.id,
            "workos_user_id": user.workos_user_id,
        },
    )
    await session.commit()
    logger.warning("webhook_user_deactivated", user_id=str(user.id))
    return True


async def _refresh_updated_user(session: AsyncSession, event: WorkOSWebhookEvent) -> bool:
    """Refresh a changed WorkOS email so login-time invitation matching stays correct."""
    data = _lenient_data(event, UserEventData)
    if data is None or not data.id or not data.email:
        return False
    user = await session.scalar(select(User).where(User.workos_user_id == data.id))
    if user is None or user.email.strip().lower() == data.email.strip().lower():
        return False
    user.email = data.email.strip().lower()
    await session.commit()
    logger.info("webhook_user_email_refreshed", user_id=str(user.id))
    return True
