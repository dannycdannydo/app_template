"""Reusable org-scoped job queries (Scope §6.5, blueprint §12).

Every jobs query filters on ``organisation_id`` first, so a job outside the
caller's organisation is simply not matched: cross-organisation access is
indistinguishable from a missing row (404, never a leak). The service layer
consumes these statements with its own ordering, paging and transaction
boundaries.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select

from app.modules.jobs.models import Job, JobStatus


def org_scoped_jobs_statement(
    organisation_id: uuid.UUID,
    *,
    status: JobStatus | None = None,
    job_type: str | None = None,
) -> Select[tuple[Job]]:
    """Return the org-scoped select shared by every jobs query.

    ``status`` and ``job_type`` are the only approved filter fields (BP §12);
    the router validates the values before the service is reached.
    """
    statement = select(Job).where(Job.organisation_id == organisation_id)
    if status is not None:
        statement = statement.where(Job.status == status)
    if job_type is not None:
        statement = statement.where(Job.job_type == job_type)
    return statement


def org_jobs_count_statement(
    organisation_id: uuid.UUID,
    *,
    status: JobStatus | None = None,
    job_type: str | None = None,
) -> Select[tuple[int]]:
    """Return the org-scoped count for pagination envelopes."""
    return select(func.count()).select_from(
        org_scoped_jobs_statement(
            organisation_id,
            status=status,
            job_type=job_type,
        ).subquery()
    )
