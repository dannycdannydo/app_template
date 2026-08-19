"""Job API schemas (Scope §6.4/§6.5, blueprint §12, §18).

ORM models are never API request models. Jobs have no request body at all —
the durable row is written by the service (``schedule_job``), never by
a client — so this module is response shapes only. They are consumed by the
job endpoints added in Scope §6.5 (``GET /api/v1/jobs`` and
``GET /api/v1/jobs/{job_id}``); they ship here with the job foundation so the
polling contract they encode (status + progress, error surface, terminal
states) is reviewed against blueprint §18 once, not again with the router.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JobListItem(BaseModel):
    """A job in list contexts; the columns the files/jobs table shows."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    status: str
    progress: int
    attempt_count: int
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobDetail(JobListItem):
    """Full job detail: the polling payload (status + progress + error)."""

    input_reference: str
    result_reference: str | None
    error_code: str | None
    error_message: str | None


class JobListResponse(BaseModel):
    """The pagination envelope documented in API_CONVENTIONS.md (BP §12)."""

    items: list[JobListItem]
    page: int
    page_size: int
    total: int
