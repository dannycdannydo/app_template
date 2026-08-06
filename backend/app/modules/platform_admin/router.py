"""Platform administration endpoints (Scope §6.3, blueprint §5, §6, §12).

The router stays thin: it resolves the caller through the dedicated platform
permission dependency (Scope §6.2 — a separate authorisation plane that never
consults ``X-Org-Id``) and delegates to the service, which owns the
transaction and the audit writes. The WorkOS organisation mapping happens
inside the service transaction, so no router code touches WorkOS directly.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_platform_permission
from app.integrations.workos.organizations import (
    WorkOSOrganizationsProvider,
    get_workos_organizations_client,
)
from app.modules.platform_admin import service
from app.modules.platform_admin.schemas import (
    PlatformOrganisationCreate,
    PlatformOrganisationResponse,
)
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])


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
