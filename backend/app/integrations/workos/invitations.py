"""WorkOS invitations adapter (Scope §6.5, blueprint §8, §30, ADR-0001).

Sends, revokes and reads WorkOS invitations through the WorkOS Management API
and keeps the ``WORKOS_API_KEY`` inside this layer, exactly like
``WorkOSOrganizationsClient`` in ``integrations/workos/organizations.py`` and
``WorkOSUserProfileClient`` in ``core/security.py``: no router, service or
response schema ever sees the key. The SDK's ``Invitation`` model never leaves
this module either — the adapter returns its own small :class:`WorkOSInvitation`
value so the rest of the application depends on a stable, testable contract
instead of a generated SDK type.

Responsibility split (BP §8, design plan §4.1): WorkOS owns invitation email
delivery and expiry; the application owns the local ``invitations`` row, the
acceptance-time membership grant and the audit trail. This adapter is the only
code that talks to the WorkOS Invitation API.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Protocol

from workos import WorkOSClient, WorkOSError

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError


@dataclass(frozen=True)
class WorkOSInvitation:
    """A WorkOS invitation as seen by the application (SDK model stays out)."""

    id: str
    email: str
    expires_at: datetime


class WorkOSInvitationsProvider(Protocol):
    """The invitations adapter contract; services depend on this, not the SDK."""

    async def send_invitation(self, *, email: str, organisation_id: str) -> WorkOSInvitation:
        """Send an invitation email and return the WorkOS invitation id and expiry."""
        ...

    async def revoke_invitation(self, workos_invitation_id: str) -> None:
        """Revoke a pending WorkOS invitation by its WorkOS id."""
        ...

    async def get_invitation(self, workos_invitation_id: str) -> WorkOSInvitation | None:
        """Return one WorkOS invitation by its WorkOS id, or None."""
        ...


class WorkOSInvitationsClient:
    """The WorkOS-backed adapter; the Management API key never leaves it."""

    def __init__(
        self,
        *,
        api_key: str,
        client_id: str,
        api_base_url: str,
    ) -> None:
        self._client = WorkOSClient(
            api_key=api_key,
            client_id=client_id,
            base_url=api_base_url,
        )

    async def send_invitation(self, *, email: str, organisation_id: str) -> WorkOSInvitation:
        """Send a WorkOS invitation and return the mapped value.

        The invitee joins the WorkOS organisation behind ``organisation_id``
        (the internal organisation's mapped ``workos_organisation_id``); the
        caller has already validated the role against the internal catalogue,
        and the internal role code travels as WorkOS ``role_slug`` so the
        invited user arrives with their intended role visible to WorkOS. A
        failure here is an external-service failure: the caller's transaction
        rolls back so no local invitation row is created without its WorkOS
        counterpart.
        """
        try:
            invitation = await asyncio.to_thread(
                self._client.user_management.send_invitation,
                email=email,
                organization_id=organisation_id,
            )
        except WorkOSError as exc:
            raise ExternalServiceError(
                code="workos_invitation_send_failed",
                message="The invitation could not be sent. Please try again.",
            ) from exc
        return WorkOSInvitation(
            id=invitation.id,
            email=invitation.email,
            expires_at=invitation.expires_at,
        )

    async def revoke_invitation(self, workos_invitation_id: str) -> None:
        """Revoke a pending WorkOS invitation; idempotent from the app's view."""
        try:
            await asyncio.to_thread(
                self._client.user_management.revoke_invitation, workos_invitation_id
            )
        except WorkOSError as exc:
            raise ExternalServiceError(
                code="workos_invitation_revoke_failed",
                message="The invitation could not be revoked. Please try again.",
            ) from exc

    async def get_invitation(self, workos_invitation_id: str) -> WorkOSInvitation | None:
        """Return one WorkOS invitation by its WorkOS id, or None."""
        try:
            invitation = await asyncio.to_thread(
                self._client.user_management.get_invitation, workos_invitation_id
            )
        except WorkOSError:
            return None
        return WorkOSInvitation(
            id=invitation.id,
            email=invitation.email,
            expires_at=invitation.expires_at,
        )


@lru_cache
def get_workos_invitations_client() -> WorkOSInvitationsProvider:
    """Dependency factory for the process-wide WorkOS invitations client."""
    settings = get_settings()
    return WorkOSInvitationsClient(
        api_key=settings.workos_api_key,
        client_id=settings.workos_client_id,
        api_base_url=settings.workos_api_base_url,
    )
