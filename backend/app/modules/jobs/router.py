"""Job endpoints (Scope §6.5, blueprint §12, §18).

The router stays thin: it parses query parameters, resolves the caller's
organisation membership through the shared dependency, gates every route with
``require_permission`` (default deny), and delegates to the service. Jobs have
no request body at all — the durable row is written by the service
(``schedule_job``), never by a client.

Permission map: list and detail need ``documents.read`` (the files module is
the only job producer in v0.5, so the job endpoints reuse its gate; a generic
``jobs.*`` permission is deferred until a second producer appears, rule of
three). The organisation id for every call comes from the resolved membership,
never from a request body or path: a job from another organisation is a 404
(acceptance §5.7), indistinguishable from a missing row.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_permission
from app.modules.jobs import service
from app.modules.jobs.models import JobStatus
from app.modules.jobs.schemas import JobDetail, JobListItem, JobListResponse
from app.modules.organisations.models import OrganisationMembership

router = APIRouter(prefix="/api/v1", tags=["jobs"])


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.read"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
    status: Annotated[JobStatus | None, Query()] = None,
    job_type: Annotated[str | None, Query(max_length=80)] = None,
) -> JobListResponse:
    """List the caller's organisation's jobs, newest first, paginated."""
    jobs, total = await service.list_jobs(
        session,
        organisation_id=membership.organisation_id,
        page=page,
        page_size=page_size,
        status=status,
        job_type=job_type,
    )
    return JobListResponse(
        items=[JobListItem.model_validate(job) for job in jobs],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job_endpoint(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.read"))],
) -> JobDetail:
    """Return one job's status and progress; a foreign job is a 404."""
    job = await service.get_job(
        session,
        organisation_id=membership.organisation_id,
        job_id=job_id,
    )
    return JobDetail.model_validate(job)
