"""File API schemas (Scope §6.3, blueprint §12, §17, §30).

ORM models are never API request models. The upload intent is the only
client-supplied input; ``extra="forbid"`` rejects any attempt to smuggle in
``object_key`` or ``storage_provider`` — both are server-generated at intent
time (acceptance §5.3). The completion payload is deliberately small (only an
optional ``checksum``), because the browser cannot be trusted to name the
object or the provider; the server resolves the file record from the path.
Every other schema is an explicit response shape whose server-controlled
fields can never come from a request body.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Filenames are bounded like record titles; content types are validated against
# ``STORAGE_ALLOWED_CONTENT_TYPES`` in the service (which needs settings).
ORIGINAL_FILENAME_MAX_LENGTH = 255
CONTENT_TYPE_MAX_LENGTH = 255


class FileUploadIntent(BaseModel):
    """Request payload for the upload-intent step (direct upload flow)."""

    model_config = ConfigDict(extra="forbid")

    original_filename: str = Field(min_length=1, max_length=ORIGINAL_FILENAME_MAX_LENGTH)
    content_type: str = Field(min_length=1, max_length=CONTENT_TYPE_MAX_LENGTH)
    size_bytes: int = Field(gt=0)


class FileUploadIntentResponse(BaseModel):
    """The signed PUT URL the browser uploads to directly (Scope §6.3)."""

    file_id: uuid.UUID
    upload_url: str
    expires_at: datetime


class FileCompleteRequest(BaseModel):
    """Request payload for the upload-completion step.

    ``checksum`` is optional: when the client can supply one (e.g. a digest it
    computed while reading the bytes), the service compares it for equality
    with the provider's checksum. The checksum is opaque — the service never
    interprets its format, only compares (Scope §6.3).
    """

    model_config = ConfigDict(extra="forbid")

    checksum: str | None = Field(default=None, min_length=1, max_length=255)


class FileListItem(BaseModel):
    """A file in list contexts; summary fields only."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    status: str
    created_by_user_id: uuid.UUID | None
    created_at: datetime


class FileDetail(FileListItem):
    """Full file detail, including the checksum when one is known."""

    checksum: str | None
    updated_at: datetime


class FileCompleteResponse(FileDetail):
    """The upload-completion response: the uploaded file plus the job to poll.

    ``processing_job_id`` is the durable job the client polls via
    ``GET /api/v1/jobs/{job_id}``. It is optional because the job foundation
    (Scope §6.4/§6.5) lands after this subsection: once the file-processing job
    exists, the completion step enqueues it and returns its id here.
    """

    processing_job_id: uuid.UUID | None = None
    # Added for consumers that need to pass the server-owned object reference
    # to another backend capability. The router always populates it; the
    # default is required here because the ORM object itself has no such API
    # field during ``model_validate``.
    storage_reference: str | None = None


class FileListResponse(BaseModel):
    """The pagination envelope documented in API_CONVENTIONS.md (BP §12)."""

    items: list[FileListItem]
    page: int
    page_size: int
    total: int


class FileDownloadUrlResponse(BaseModel):
    """A short-lived signed GET URL for one stored object."""

    download_url: str
    expires_at: datetime
