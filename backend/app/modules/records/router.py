"""Record endpoints (v0.2 Scope §6.5, blueprint §5, §6, §12).

The router stays thin: it parses the request body and query parameters,
resolves the caller's organisation membership through the shared dependency,
gates every route with ``require_permission`` (default deny), and delegates
to the service. The organisation id for every call comes from the resolved
membership — never from the request body (acceptance §5.4).

Permission map: list/get need ``records.read``; create ``records.create``;
update ``records.update``; delete ``records.delete``. A viewer therefore lists
and reads records but every write returns 403 (acceptance §5.5).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_permission
from app.modules.organisations.models import OrganisationMembership
from app.modules.records import service
from app.modules.records.schemas import (
    RecordCreate,
    RecordDetail,
    RecordListItem,
    RecordListResponse,
    RecordUpdate,
)

router = APIRouter(prefix="/api/v1", tags=["records"])


@router.get("/records", response_model=RecordListResponse)
async def list_records_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("records.read"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
) -> RecordListResponse:
    """List the caller's organisation's records, newest first, paginated."""
    records, total = await service.list_records(
        session,
        organisation_id=membership.organisation_id,
        page=page,
        page_size=page_size,
    )
    return RecordListResponse(
        items=[RecordListItem.model_validate(record) for record in records],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.post("/records", response_model=RecordDetail, status_code=201)
async def create_record_endpoint(
    payload: RecordCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("records.create"))],
) -> RecordDetail:
    """Create a record inside the caller's organisation."""
    record = await service.create_record(
        session,
        organisation_id=membership.organisation_id,
        title=payload.title,
        body=payload.body,
    )
    return RecordDetail.model_validate(record)


@router.get("/records/{record_id}", response_model=RecordDetail)
async def get_record_endpoint(
    record_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("records.read"))],
) -> RecordDetail:
    """Return one record; a record outside the organisation is a 404."""
    record = await service.get_record(
        session,
        organisation_id=membership.organisation_id,
        record_id=record_id,
    )
    return RecordDetail.model_validate(record)


@router.patch("/records/{record_id}", response_model=RecordDetail)
async def update_record_endpoint(
    record_id: uuid.UUID,
    payload: RecordUpdate,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("records.update"))],
) -> RecordDetail:
    """Partially update a record inside the caller's organisation."""
    record = await service.update_record(
        session,
        organisation_id=membership.organisation_id,
        record_id=record_id,
        title=payload.title,
        body=payload.body,
    )
    return RecordDetail.model_validate(record)


@router.delete("/records/{record_id}", status_code=204)
async def delete_record_endpoint(
    record_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("records.delete"))],
) -> None:
    """Delete a record inside the caller's organisation."""
    await service.delete_record(
        session,
        organisation_id=membership.organisation_id,
        record_id=record_id,
    )
