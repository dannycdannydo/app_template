"""Platform AI-settings endpoints (v0.7 Scope §6.5, BP §9, §12, §27).

The router stays thin: both routes resolve the caller through the dedicated
platform permission dependency (BP §9 — a separate authorisation plane that
never consults ``X-Org-Id``) and delegate to the service, which owns the
transaction, the registry validation and the audit write. The organisation id
comes from the path, never from the body; the request schema is
``extra="forbid"`` so server-controlled fields cannot be smuggled in. There is
no organisation-plane AI settings surface in v0.7: the management API is
platform-gated (v0.7 Scope §6.5) and request-time enforcement happens inside
``AIService``, never in a router or UI.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.persistence import service
from app.ai.persistence.models import OrganisationAISettings
from app.ai.persistence.schemas import (
    PlatformOrganisationAISettingsResponse,
    PlatformOrganisationAISettingsUpdate,
)
from app.api.dependencies import get_db, require_platform_permission
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/platform/organisations", tags=["platform"])


def _item(settings_row: OrganisationAISettings) -> PlatformOrganisationAISettingsResponse:
    """Assemble the response shape from the ORM row (pure mapping)."""
    return PlatformOrganisationAISettingsResponse(
        organisation_id=settings_row.organisation_id,
        enabled=settings_row.enabled,
        allowed_provider_ids=list(settings_row.allowed_provider_ids),
        allowed_model_ids=list(settings_row.allowed_model_ids),
        provider_override=settings_row.provider_override,
        model_override=settings_row.model_override,
        monthly_budget=settings_row.monthly_budget,
        retention_policy_days=settings_row.retention_policy_days,
        updated_by_user_id=settings_row.updated_by_user_id,
        created_at=settings_row.created_at,
        updated_at=settings_row.updated_at,
    )


@router.get(
    "/{organisation_id}/ai-settings",
    response_model=PlatformOrganisationAISettingsResponse,
)
async def get_ai_settings_endpoint(
    organisation_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
) -> PlatformOrganisationAISettingsResponse:
    """View one organisation's AI policy (platform-gated, not org-scoped)."""
    settings_row = await service.get_ai_settings(session, organisation_id=organisation_id)
    return _item(settings_row)


@router.put(
    "/{organisation_id}/ai-settings",
    response_model=PlatformOrganisationAISettingsResponse,
)
async def update_ai_settings_endpoint(
    organisation_id: uuid.UUID,
    payload: PlatformOrganisationAISettingsUpdate,
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
) -> PlatformOrganisationAISettingsResponse:
    """Replace one organisation's AI policy (validated, audited)."""
    settings_row = await service.update_ai_settings(
        session,
        actor=user,
        organisation_id=organisation_id,
        enabled=payload.enabled,
        allowed_provider_ids=payload.allowed_provider_ids,
        allowed_model_ids=payload.allowed_model_ids,
        provider_override=payload.provider_override,
        model_override=payload.model_override,
        monthly_budget=payload.monthly_budget,
        retention_policy_days=payload.retention_policy_days,
    )
    return _item(settings_row)
