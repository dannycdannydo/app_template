"""Platform administration endpoints (Scope §6.3/§6.6, blueprint §5, §6, §12).

The router stays thin: it resolves the caller through the dedicated platform
permission dependency (Scope §6.2 — a separate authorisation plane that never
consults ``X-Org-Id``) and delegates to the service, which owns the
transaction, the WorkOS calls and the audit writes. The WorkOS organisation
mapping and invitation revocation happen inside the service transactions, so
no router code touches WorkOS directly. The organisation and membership ids
come from the path (validated as UUIDs by FastAPI), never from a request
body; the request schemas are ``extra="forbid"`` so server-controlled fields
cannot be smuggled in.
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
from app.modules.platform_admin import service
from app.modules.platform_admin.schemas import (
    PlatformMembershipListItem,
    PlatformMembershipListResponse,
    PlatformMembershipRoleAssign,
    PlatformMembershipStatusUpdate,
    PlatformOrganisationCreate,
    PlatformOrganisationResponse,
)
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])


def _membership_item(detail: service.MembershipDetail) -> PlatformMembershipListItem:
    """Assemble the response shape from a service detail (pure mapping)."""
    return PlatformMembershipListItem(
        id=detail.membership.id,
        organisation_id=detail.membership.organisation_id,
        user_id=detail.membership.user_id,
        user_name=detail.user_name,
        user_email=detail.user_email,
        status=detail.membership.status,
        roles=detail.roles,
        created_at=detail.membership.created_at,
        updated_at=detail.membership.updated_at,
    )


@router.post(
    "/organisations",
    response_model=PlatformOrganisationResponse,
    status_code=201,
)
async def create_platform_organisation_endpoint(
    payload: PlatformOrganisationCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    workos: Annotated[WorkOSOrganizationsProvider, Depends(get_workos_organizations_client)],
) -> PlatformOrganisationResponse:
    """Create an organisation with its WorkOS mapping, then return it.

    The caller must be a platform administrator; the organisation is created
    without any membership because platform admins administer organisations
    they do not belong to. The response includes the server-assigned
    ``workos_organisation_id`` mapping.
    """
    organisation = await service.create_platform_organisation(
        session,
        actor=_user,
        name=payload.name,
        workos=workos,
    )
    return PlatformOrganisationResponse.model_validate(organisation)


@router.get(
    "/organisations/{organisation_id}/memberships",
    response_model=PlatformMembershipListResponse,
)
async def list_memberships_endpoint(
    organisation_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
) -> PlatformMembershipListResponse:
    """List an organisation's memberships with user context and roles.

    Newest first, paginated; the caller must be a platform administrator and
    the organisation id comes from the path, never from a request body.
    """
    details, total = await service.list_memberships(
        session,
        organisation_id=organisation_id,
        page=page,
        page_size=page_size,
    )
    return PlatformMembershipListResponse(
        items=[_membership_item(detail) for detail in details],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post(
    "/organisations/{organisation_id}/memberships/{membership_id}/roles",
    response_model=PlatformMembershipListItem,
)
async def assign_membership_role_endpoint(
    organisation_id: uuid.UUID,
    membership_id: uuid.UUID,
    payload: PlatformMembershipRoleAssign,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
) -> PlatformMembershipListItem:
    """Assign one organisation role to a membership (audited)."""
    detail = await service.assign_role(
        session,
        actor=user,
        organisation_id=organisation_id,
        membership_id=membership_id,
        role_code=payload.role_code,
    )
    return _membership_item(detail)


@router.delete(
    "/organisations/{organisation_id}/memberships/{membership_id}/roles/{role_code}",
    response_model=PlatformMembershipListItem,
)
async def remove_membership_role_endpoint(
    organisation_id: uuid.UUID,
    membership_id: uuid.UUID,
    role_code: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
) -> PlatformMembershipListItem:
    """Remove one organisation role from a membership (audited)."""
    detail = await service.remove_role(
        session,
        actor=user,
        organisation_id=organisation_id,
        membership_id=membership_id,
        role_code=role_code,
    )
    return _membership_item(detail)


@router.patch(
    "/organisations/{organisation_id}/memberships/{membership_id}/status",
    response_model=PlatformMembershipListItem,
)
async def set_membership_status_endpoint(
    organisation_id: uuid.UUID,
    membership_id: uuid.UUID,
    payload: PlatformMembershipStatusUpdate,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    workos_invitations: Annotated[
        WorkOSInvitationsProvider, Depends(get_workos_invitations_client)
    ],
) -> PlatformMembershipListItem:
    """Suspend or reactivate a membership; suspension revokes its invitations."""
    detail = await service.set_membership_status(
        session,
        actor=user,
        organisation_id=organisation_id,
        membership_id=membership_id,
        status=payload.status,
        workos_invitations=workos_invitations,
    )
    return _membership_item(detail)


@router.delete(
    "/organisations/{organisation_id}/memberships/{membership_id}",
    response_model=PlatformMembershipListItem,
)
async def remove_membership_endpoint(
    organisation_id: uuid.UUID,
    membership_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    workos_invitations: Annotated[
        WorkOSInvitationsProvider, Depends(get_workos_invitations_client)
    ],
) -> PlatformMembershipListItem:
    """Remove a membership and revoke its pending invitations (audited)."""
    detail = await service.remove_membership(
        session,
        actor=user,
        organisation_id=organisation_id,
        membership_id=membership_id,
        workos_invitations=workos_invitations,
    )
    return _membership_item(detail)
