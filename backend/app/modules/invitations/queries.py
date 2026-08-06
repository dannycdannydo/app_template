"""Reusable invitation queries (Scope §6.5, blueprint §12).

The listing statement carries the only approved filter field (organisation) so
the service and the tests share one place where the filter columns are named;
the platform listing endpoint is cross-org by design (Scope §6.2) and takes
its organisation from the path, so the WHERE clause is always fixed.

The pending-invitation statement drives login-time linking: it selects only
invitations that can still grant (``sent``, not yet expired) for one email
(case-insensitive), so a revoked, accepted, expired or mismatched invitation
never reaches the grant step (acceptance §5.6).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, func, select

from app.modules.invitations.models import Invitation, InvitationStatus


def invitations_statement(*, organisation_id: uuid.UUID) -> Select[tuple[Invitation]]:
    """Return a statement selecting the invitations of one organisation."""
    return select(Invitation).where(Invitation.organisation_id == organisation_id)


def invitations_count_statement(*, organisation_id: uuid.UUID) -> Select[tuple[int]]:
    """Return a statement counting the invitations of one organisation."""
    return select(func.count()).select_from(
        invitations_statement(organisation_id=organisation_id).subquery()
    )


def pending_invitations_statement(email: str) -> Select[tuple[Invitation]]:
    """Return a statement selecting grantable invitations for one email.

    ``sent`` invitations whose expiry has not passed, matched case-insensitively
    against the (normalised) email of the authenticated user. The Python-side
    re-check in the linking service guards the same conditions again because
    between this SELECT and the INSERT another transaction (e.g. a webhook
    refresh, Scope §6.8) may have revoked the invitation.
    """
    return (
        select(Invitation)
        .where(
            Invitation.status == InvitationStatus.SENT,
            Invitation.expires_at > datetime.now(UTC),
            func.lower(Invitation.email) == email.strip().lower(),
        )
        .order_by(Invitation.created_at)
    )
