"""Platform feature-flag endpoints (blueprint §27, Scope §6.7).

The router stays thin: both routes resolve the caller through the dedicated
platform permission dependency (Scope §6.2 — a separate authorisation plane
that never consults ``X-Org-Id``) and delegate to the service, which owns the
transaction, the override upsert and the audit write. The feature key comes
from the path; the organisation id comes from the request body for the PUT
because the platform plane has no organisation header — the caller is a
platform administrator administering organisations they do not belong to. The
request schema is ``extra="forbid"`` so server-controlled fields cannot be
smuggled in.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_platform_permission
from app.modules.feature_flags import service
from app.modules.feature_flags.schemas import (
    PlatformFeatureFlagItem,
    PlatformFeatureFlagListResponse,
    PlatformFeatureFlagUpdate,
)
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])


def _item(state: service.FeatureFlagState) -> PlatformFeatureFlagItem:
    """Assemble the response shape from a service state (pure mapping)."""
    return PlatformFeatureFlagItem(
        feature_key=state.definition.key,
        name=state.definition.name,
        description=state.definition.description,
        default_enabled=state.definition.default_enabled,
        enabled=state.enabled,
        overridden=state.overridden,
        configuration_json=state.configuration_json,
    )


@router.get("/feature-flags", response_model=PlatformFeatureFlagListResponse)
async def list_feature_flags_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    organisation_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PlatformFeatureFlagListResponse:
    """List the feature-flag catalogue, optionally merged with one org's overrides.

    Without ``organisation_id`` every flag is shown at its catalogue default;
    with one, each entry carries the organisation's effective state. The
    caller must be a platform administrator (Scope §6.2).
    """
    states = await service.list_feature_flags(session, organisation_id=organisation_id)
    return PlatformFeatureFlagListResponse(items=[_item(state) for state in states])


@router.put(
    "/feature-flags/{feature_key}",
    response_model=PlatformFeatureFlagItem,
)
async def set_feature_flag_endpoint(
    feature_key: str,
    payload: PlatformFeatureFlagUpdate,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
) -> PlatformFeatureFlagItem:
    """Set an organisation's override for one feature flag (audited)."""
    state = await service.set_feature_flag(
        session,
        actor=user,
        feature_key=feature_key,
        organisation_id=payload.organisation_id,
        enabled=payload.enabled,
        configuration_json=payload.configuration_json,
    )
    return _item(state)
