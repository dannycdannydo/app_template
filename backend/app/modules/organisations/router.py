"""Organisation endpoints (Scope §6.3, blueprint §5, §6).

The router stays thin: it parses the request body, resolves the authenticated
user through the shared dependency, and delegates to the service. Creating an
organisation is a bootstrap operation — the caller is not yet a member of any
organisation — so it requires only a Bearer token, not an ``X-Org-Id`` header.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db
from app.modules.organisations.schemas import OrganisationCreate, OrganisationResponse
from app.modules.organisations.service import create_organisation
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1", tags=["organisations"])


@router.post(
    "/organisations",
    response_model=OrganisationResponse,
    status_code=201,
)
async def create_organisation_endpoint(
    payload: OrganisationCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> OrganisationResponse:
    """Create an organisation; the creator becomes its owner."""
    organisation = await create_organisation(session, user, payload.name)
    return OrganisationResponse.model_validate(organisation)
