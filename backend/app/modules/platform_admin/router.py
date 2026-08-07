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
    PlatformAdminGrant,
    PlatformAdminListItem,
    PlatformAdminListResponse,
    PlatformMembershipListItem,
    PlatformMembershipListResponse,
    PlatformMembershipRoleAssign,
    PlatformMembershipStatusUpdate,
    PlatformOrganisationCreate,
    PlatformOrganisationListResponse,
    PlatformOrganisationResponse,
    PlatformOrganisationUpdate,
    PlatformUserListItem,
    PlatformUserListResponse,
)
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])


def _platform_admin_item(detail: service.PlatformAdminDetail) -> PlatformAdminListItem:
    return PlatformAdminListItem(
        id=detail.membership.id,
        user_id=detail.membership.user_id,
        user_name=detail.user_name,
        user_email=detail.user_email,
        role_code=detail.role_code,
        created_at=detail.membership.created_at,
        updated_at=detail.membership.updated_at,
    )


@router.get("/admins", response_model=PlatformAdminListResponse)
async def list_platform_admins_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
) -> PlatformAdminListResponse:
    """List the explicit administrators of the dedicated platform plane."""
    admins, total = await service.list_platform_admins(session, page=page, page_size=page_size)
    return PlatformAdminListResponse(
        items=[_platform_admin_item(admin) for admin in admins],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post("/admins", response_model=PlatformAdminListItem, status_code=201)
async def grant_platform_admin_endpoint(
    payload: PlatformAdminGrant,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
) -> PlatformAdminListItem:
    """Grant platform_admin to an existing enabled user (audited)."""
    return _platform_admin_item(
        await service.grant_platform_admin(session, actor=user, user_id=payload.user_id)
    )


@router.get("/users", response_model=PlatformUserListResponse)
async def list_platform_users_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
    search: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
) -> PlatformUserListResponse:
    """List enabled users by readable identity for platform-role assignment."""
    users, total = await service.list_enabled_users(
        session, page=page, page_size=page_size, search=search
    )
    return PlatformUserListResponse(
        items=[
            PlatformUserListItem(id=user.id, name=user.name, email=user.email) for user in users
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.delete("/admins/{platform_membership_id}", response_model=PlatformAdminListItem)
async def revoke_platform_admin_endpoint(
    platform_membership_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
) -> PlatformAdminListItem:
    """Revoke platform_admin without allowing the final admin to be removed."""
    return _platform_admin_item(
        await service.revoke_platform_admin(
            session, actor=user, platform_membership_id=platform_membership_id
        )
    )


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
    "/organisations",
    response_model=PlatformOrganisationListResponse,
)
async def list_platform_organisations_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
) -> PlatformOrganisationListResponse:
    """List every organisation, newest first, paginated.

    The admin centre's catalogue over the whole tenant fleet. The caller must
    be a platform administrator; the pagination envelope matches the other
    platform listings (blueprint §12).
    """
    organisations, total = await service.list_organisations(
        session,
        page=page,
        page_size=page_size,
    )
    return PlatformOrganisationListResponse(
        items=[
            PlatformOrganisationResponse.model_validate(organisation)
            for organisation in organisations
        ],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get(
    "/organisations/{organisation_id}",
    response_model=PlatformOrganisationResponse,
)
async def get_platform_organisation_endpoint(
    organisation_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
) -> PlatformOrganisationResponse:
    """View one organisation, including its WorkOS mapping."""
    organisation = await service.get_organisation(
        session,
        organisation_id=organisation_id,
    )
    return PlatformOrganisationResponse.model_validate(organisation)


@router.patch(
    "/organisations/{organisation_id}",
    response_model=PlatformOrganisationResponse,
)
async def update_platform_organisation_endpoint(
    organisation_id: uuid.UUID,
    payload: PlatformOrganisationUpdate,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
) -> PlatformOrganisationResponse:
    """Rename one organisation (audited)."""
    organisation = await service.update_organisation(
        session,
        actor=user,
        organisation_id=organisation_id,
        name=payload.name,
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
