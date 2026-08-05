"""User endpoints (blueprint §5, §8).

The router stays thin: it assembles the /me response from the users service
and the shared auth dependencies.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db
from app.modules.organisations.schemas import MembershipListItem
from app.modules.users.models import User
from app.modules.users.schemas import MeResponse, UserListItem
from app.modules.users.service import get_me_payload

router = APIRouter(prefix="/api/v1", tags=["users"])


@router.get("/me", response_model=MeResponse)
async def me(
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> MeResponse:
    """Return the current user with their memberships and roles."""
    memberships, roles = await get_me_payload(session, user)
    return MeResponse(
        user=UserListItem.model_validate(user),
        memberships=[MembershipListItem.model_validate(m) for m in memberships],
        roles=roles,
    )
