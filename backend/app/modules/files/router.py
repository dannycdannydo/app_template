"""File endpoints (Scope §6.3, blueprint §5, §6, §12, §17).

The router stays thin: it parses request bodies and query parameters, resolves
the caller's organisation membership through the shared dependency, gates every
route with ``require_permission`` (default deny), and delegates to the service.
The organisation id for every call comes from the resolved membership — never
from a request body, and never from the object key (the client only ever
submits the file id; the key is server-generated).

Permission map: list/detail/download-url need ``documents.read``; intent and
completion ``documents.upload``; delete ``documents.delete``. A viewer
therefore lists and reads files (and can get download links) but every write
returns 403.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_permission
from app.modules.files import service
from app.modules.files.models import FileStatus
from app.modules.files.schemas import (
    FileCompleteRequest,
    FileCompleteResponse,
    FileDetail,
    FileDownloadUrlResponse,
    FileListItem,
    FileListResponse,
    FileUploadIntent,
    FileUploadIntentResponse,
)
from app.modules.organisations.models import OrganisationMembership

router = APIRouter(prefix="/api/v1", tags=["files"])


@router.post("/files", response_model=FileUploadIntentResponse, status_code=201)
async def create_upload_intent_endpoint(
    payload: FileUploadIntent,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.upload"))],
) -> FileUploadIntentResponse:
    """Start a direct upload: validate, create the pending record, sign a URL."""
    file, signed_url = await service.create_upload_intent(
        session,
        organisation_id=membership.organisation_id,
        original_filename=payload.original_filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        actor_user_id=membership.user_id,
    )
    return FileUploadIntentResponse(
        file_id=file.id,
        upload_url=signed_url.url,
        expires_at=signed_url.expires_at,
    )


@router.post("/files/{file_id}/complete", response_model=FileCompleteResponse)
async def complete_upload_endpoint(
    file_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.upload"))],
    payload: FileCompleteRequest | None = None,
) -> FileCompleteResponse:
    """Verify the stored object and mark the file uploaded.

    The request body is entirely optional (the only field is ``checksum``), so
    a client may POST with no body at all. On verification failure the file is
    marked ``failed`` and the request is rejected with 422 so the client knows
    the upload was not accepted.
    """
    file, processing_job_id = await service.complete_upload(
        session,
        organisation_id=membership.organisation_id,
        file_id=file_id,
        checksum=payload.checksum if payload is not None else None,
        actor_user_id=membership.user_id,
    )
    response = FileCompleteResponse.model_validate(file)
    response.processing_job_id = processing_job_id
    return response


@router.get("/files", response_model=FileListResponse)
async def list_files_endpoint(
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.read"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)] = service.DEFAULT_PAGE_SIZE,
    status: Annotated[FileStatus | None, Query()] = None,
) -> FileListResponse:
    """List the caller's organisation's files, newest first, paginated."""
    files, total = await service.list_files(
        session,
        organisation_id=membership.organisation_id,
        page=page,
        page_size=page_size,
        status=status,
    )
    return FileListResponse(
        items=[FileListItem.model_validate(file) for file in files],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/files/{file_id}", response_model=FileDetail)
async def get_file_endpoint(
    file_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.read"))],
) -> FileDetail:
    """Return one file; a file outside the organisation is a 404."""
    file = await service.get_file(
        session,
        organisation_id=membership.organisation_id,
        file_id=file_id,
    )
    return FileDetail.model_validate(file)


@router.get("/files/{file_id}/download-url", response_model=FileDownloadUrlResponse)
async def create_download_url_endpoint(
    file_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.read"))],
) -> FileDownloadUrlResponse:
    """Return a short-lived signed GET URL for one stored object."""
    signed_url = await service.create_download_url(
        session,
        organisation_id=membership.organisation_id,
        file_id=file_id,
    )
    return FileDownloadUrlResponse(
        download_url=signed_url.url,
        expires_at=signed_url.expires_at,
    )


@router.delete("/files/{file_id}", status_code=204)
async def delete_file_endpoint(
    file_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.delete"))],
) -> None:
    """Soft-delete a file: remove the object, mark the row deleted, audit."""
    await service.delete_file(
        session,
        organisation_id=membership.organisation_id,
        file_id=file_id,
        actor_user_id=membership.user_id,
    )
