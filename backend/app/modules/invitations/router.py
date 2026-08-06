"""Platform invitation endpoints (Scope §6.5, blueprint §5, §6, §12).

The router stays thin: it resolves the caller through the dedicated platform
permission dependency (Scope §6.2 — a separate authorisation plane that never
consults ``X-Org-Id``) and delegates to the service, which owns the
transaction, the WorkOS delivery and the audit writes. The organisation id
comes from the path (validated as a UUID by FastAPI), never from a request
body; the invite schema itself is ``extra="forbid"`` so server-controlled
fields cannot be smuggled in.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_platform_permission
from app.integrations.workos.invitations import (
    WorkOSInvitationsProvider,
    get_workos_invitations_client,
)
from app.integrations.workos.organizations import (
    WorkOSOrganizationsProvider,
    get_workos_organizations_client,
)
from app.modules.invitations import service
from app.modules.invitations.schemas import (
    InvitationCreate,
    InvitationListItem,
    InvitationListResponse,
)
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])


@router.post(
    "/organisations/{organisation_id}/invitations",
    response_model=InvitationListItem,
    status_code=201,
)
async def invite_user_endpoint(
    organisation_id: uuid.UUID,
    payload: InvitationCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    workos_invitations: Annotated[
        WorkOSInvitationsProvider, Depends(get_workos_invitations_client)
    ],
    workos_organisations: Annotated[
        WorkOSOrganizationsProvider, Depends(get_workos_organizations_client)
    ],
) -> InvitationListItem:
    """Invite a user into an organisation through the WorkOS Invitation API.

    The caller must be a platform administrator. No membership row is created
    here; the invitee gains their membership when they accept and sign in
    (login-time linking, acceptance §5.6).
    """
    invitation = await service.invite_user(
        session,
        actor=_user,
        organisation_id=organisation_id,
        email=payload.email,
        role_code=payload.role_code,
        workos_invitations=workos_invitations,
        workos_organisations=workos_organisations,
    )
    return InvitationListItem.model_validate(invitation)


@router.get(
    "/organisations/{organisation_id}/invitations",
    response_model=InvitationListResponse,
)
async def list_invitations_endpoint(
    organisation_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
) -> InvitationListResponse:
    """List an organisation's invitations, newest first, paginated."""
    invitations, total = await service.list_invitations(
        session,
        organisation_id=organisation_id,
        page=page,
        page_size=page_size,
    )
    return InvitationListResponse(
        items=[InvitationListItem.model_validate(invitation) for invitation in invitations],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.delete(
    "/organisations/{organisation_id}/invitations/{invitation_id}",
    response_model=InvitationListItem,
)
async def revoke_invitation_endpoint(
    organisation_id: uuid.UUID,
    invitation_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    workos: Annotated[WorkOSInvitationsProvider, Depends(get_workos_invitations_client)],
) -> InvitationListItem:
    """Revoke a pending invitation at WorkOS and locally (audited)."""
    invitation = await service.revoke_invitation(
        session,
        actor=_user,
        organisation_id=organisation_id,
        invitation_id=invitation_id,
        workos=workos,
    )
    return InvitationListItem.model_validate(invitation)
