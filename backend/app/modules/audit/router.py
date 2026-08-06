"""Audit history endpoints (blueprint §12, §29, Scope §6.1).

The router stays thin: it parses the approved filter query parameters,
resolves the caller as a platform administrator through the dedicated platform
permission dependency (Scope §6.2 — a separate authorisation plane that never
consults ``X-Org-Id``), and delegates to the service. There is no write
endpoint here because the audit log is append-only by construction.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_platform_permission
from app.modules.audit import service
from app.modules.audit.schemas import AuditEventListItem, AuditEventListResponse
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])


@router.get("/audit-events", response_model=AuditEventListResponse)
async def list_audit_events_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_platform_permission("platform.admin"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
    organisation_id: Annotated[uuid.UUID | None, Query()] = None,
    actor_user_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
) -> AuditEventListResponse:
    """List the audit history, filterable by organisation, actor and action."""
    events, total = await service.list_audit_events(
        session,
        page=page,
        page_size=page_size,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=action,
    )
    return AuditEventListResponse(
        items=[AuditEventListItem.model_validate(event) for event in events],
        page=page,
        page_size=page_size,
        total=total,
    )
