"""Durable job services and the bounded retry policy (Scope §6.4, blueprint §18).

The job service is the single owner of the durable ``jobs`` table:

- :func:`create_and_enqueue` is the only way a job is created: it writes the
  durable ``queued`` row first, then enqueues the Dramatiq task, in one
  transaction (BP §18 flow, BP §11 — record-then-enqueue; the transactional
  outbox is deferred post-v1, so the narrow commit-after-enqueue window is
  self-healed by the retry policy, see below).
- :func:`mark_running`, :func:`update_progress`, :func:`succeed` and
  :func:`fail` are the helpers the worker tasks call; each owns its own
  transaction (BP §11) and refuses to move a terminal state (acceptance §5.7:
  terminal states are never re-run).

The retry policy: transient errors are retried up to ``MAX_ATTEMPTS``;
permanent validation errors raise :class:`JobPermanentError`, which tasks
declare in their ``throws`` so the Retries middleware never retries them (and
the task marks the durable row ``failed`` itself). When transient retries are
exhausted, the Retries middleware sends the ``mark_job_failed_after_retries``
actor (``app.modules.jobs.tasks``), which records the failure on the durable
row so a job never sits in ``running`` forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from dramatiq import Actor
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ErrorDetail, NotFoundError, ValidationError
from app.db.conventions import uuid7
from app.modules.audit.service import (
    ACTION_JOB_FAILED,
    ACTION_JOB_SUCCEEDED,
    record_event,
)
from app.modules.jobs.models import Job, JobStatus
from app.modules.jobs.queries import org_jobs_count_statement, org_scoped_jobs_statement

# The pagination envelope contract shared with the files module (BP §12):
# ``?page=1&page_size=50`` with the ``{items, page, page_size, total}`` body,
# page_size clamped to ``MAX_PAGE_SIZE``.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# Bounded retry policy (blueprint §18: retry transient errors, do not retry
# permanent validation errors indefinitely). ``MAX_ATTEMPTS`` is the total
# number of attempts (first run plus ``max_retries`` retries); the retry
# middleware applies exponential backoff starting at ``RETRY_MIN_BACKOFF_MS``.
MAX_ATTEMPTS = 3
RETRY_MIN_BACKOFF_MS = 1000

# The actor the Retries middleware messages when a job's transient retries are
# exhausted, and the durable record it writes. The constant is the single
# source of truth: ``retry_policy()`` names it and the task module declares it
# under this name.
MARK_FAILED_AFTER_RETRIES_ACTOR = "mark_job_failed_after_retries"
ERROR_CODE_RETRIES_EXHAUSTED = "job_retries_exhausted"
ERROR_MESSAGE_RETRIES_EXHAUSTED = "The job exhausted its allowed attempts."


class JobPermanentError(Exception):
    """Raised by a task for a failure retries cannot fix.

    Tasks declare this exception in their ``throws`` (via
    :func:`retry_policy`), so the Retries middleware fails the message without
    retrying it. The task must mark the durable job ``failed`` itself (via
    :func:`fail`) before raising, so the durable row and the message agree.
    """


def retry_policy() -> dict[str, Any]:
    """Return the standard Dramatiq actor options encoding the retry policy.

    Every job task spreads these into its ``@dramatiq.actor`` declaration::

        @dramatiq.actor(queue_name="documents", **jobs_service.retry_policy())
        async def process_file(job_id: str) -> None:
            ...

    ``max_retries`` bounds the total attempts to ``MAX_ATTEMPTS``; ``throws``
    declares :class:`JobPermanentError` as never-retried; the exhausted
    message lands on the ``mark_job_failed_after_retries`` actor so the durable
    row records the failure.
    """
    return {
        "max_retries": MAX_ATTEMPTS - 1,
        "min_backoff": RETRY_MIN_BACKOFF_MS,
        "throws": (JobPermanentError,),
        "on_retry_exhausted": MARK_FAILED_AFTER_RETRIES_ACTOR,
    }


def _not_found() -> NotFoundError:
    return NotFoundError(
        code="job_not_found",
        message="The job could not be found.",
    )


def _terminal(status: JobStatus) -> bool:
    return status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


async def _get_job(session: AsyncSession, *, job_id: uuid.UUID) -> Job:
    """Return one job by id for the worker-side helpers.

    The worker knows only the job id it was messaged with; the org-scoped
    lookup is a query-level concern of the API endpoints (Scope §6.5), where
    the caller's organisation filters the statement so a foreign job is a 404.
    """
    job = await session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise _not_found()
    return job


async def get_job_for_task(session: AsyncSession, *, job_id: uuid.UUID) -> Job:
    """Return the durable row a worker task operates on (worker-side read).

    Public wrapper over the worker-side lookup (the task modules call it);
    like ``_get_job`` it is deliberately not org-scoped, because the worker
    knows only the job id it was messaged with.
    """
    return await _get_job(session, job_id=job_id)


def is_terminal(status: JobStatus) -> bool:
    """Return whether ``status`` is a terminal state that is never re-run."""
    return _terminal(status)


async def get_job(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    job_id: uuid.UUID,
) -> Job:
    """Return one job; a job outside the organisation is a 404 (Scope §6.5).

    The org-scoped filter is the isolation boundary: a job id that exists in
    another organisation simply does not match, so cross-organisation reads
    are indistinguishable from missing rows (acceptance §5.7).
    """
    job = await session.scalar(
        select(Job).where(Job.organisation_id == organisation_id, Job.id == job_id)
    )
    if job is None:
        raise _not_found()
    return job


async def list_jobs(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    page: int,
    page_size: int,
    status: JobStatus | None = None,
    job_type: str | None = None,
) -> tuple[list[Job], int]:
    """Return one page of the caller's organisation's jobs plus the total.

    Newest first, ties broken by id so paging is stable (the same ordering as
    files and records). ``status`` and ``job_type`` are the only approved
    filter fields (BP §12); the router validates the query parameters and the
    service still clamps page/page_size defensively.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total = await session.scalar(
        org_jobs_count_statement(organisation_id, status=status, job_type=job_type)
    )
    rows = await session.scalars(
        org_scoped_jobs_statement(organisation_id, status=status, job_type=job_type)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.all()), total or 0


async def create_and_enqueue(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    job_type: str,
    input_reference: str,
    actor_user_id: uuid.UUID | None = None,
    task: Actor[Any, Any],
) -> Job:
    """Write the durable ``queued`` row, then enqueue the task (BP §18 flow).

    One transaction (BP §11): the durable row is flushed (so the job id exists
    and the row is part of the transaction), then the task is enqueued with
    that id, then the transaction commits. If enqueuing fails the whole
    transaction rolls back, so a failed enqueue never leaves an orphaned
    ``queued`` row. The theoretical commit-after-enqueue window (the worker
    picks up the message before the commit lands) is a transient failure on
    the worker side — the job row is not visible yet — which the bounded
    retry policy self-heals.
    """
    job = Job(
        id=uuid7(),
        organisation_id=organisation_id,
        job_type=job_type,
        status=JobStatus.QUEUED,
        progress=0,
        input_reference=input_reference,
        created_by_user_id=actor_user_id,
    )
    session.add(job)
    await session.flush()
    task.send(job_id=str(job.id))
    await session.commit()
    await session.refresh(job)
    return job


async def mark_running(session: AsyncSession, *, job_id: uuid.UUID) -> Job:
    """Transition a job to ``running`` at the start of a task attempt.

    Idempotent across retries: an already-``running`` row stays running, the
    attempt counter increments (one per attempt) and ``started_at`` is only
    set on the first attempt. A terminal job is never re-run (acceptance
    §5.7): a message that arrives after the job already finished raises a 409,
    which the task surfaces as a transient error.
    """
    job = await _get_job(session, job_id=job_id)
    if _terminal(job.status):
        raise ConflictError(
            code="job_in_terminal_state",
            message="A finished job cannot be run again.",
        )
    job.status = JobStatus.RUNNING
    job.attempt_count = job.attempt_count + 1
    if job.started_at is None:
        job.started_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(job)
    return job


async def update_progress(session: AsyncSession, *, job_id: uuid.UUID, progress: int) -> Job:
    """Record a task's progress (0-100) on a running job."""
    if not 0 <= progress <= 100:
        raise ValidationError(
            code="invalid_job_progress",
            message="Job progress must be between 0 and 100.",
            details=[
                ErrorDetail(
                    field="progress",
                    message="Progress is outside the 0-100 range.",
                )
            ],
        )
    job = await _get_job(session, job_id=job_id)
    if _terminal(job.status):
        raise ConflictError(
            code="job_in_terminal_state",
            message="A finished job cannot be updated.",
        )
    job.progress = progress
    await session.commit()
    await session.refresh(job)
    return job


async def succeed(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    result_reference: str | None = None,
) -> Job:
    """Transition a job to ``succeeded`` with progress 100, and audit it.

    Idempotent: calling it again on an already-``succeeded`` job is a no-op
    (a retried message that raced the original completion), so no second audit
    row is written. A job that already failed or was cancelled is a 409 —
    terminal states are never re-run.
    """
    job = await _get_job(session, job_id=job_id)
    if job.status == JobStatus.SUCCEEDED:
        return job
    if _terminal(job.status):
        raise ConflictError(
            code="job_in_terminal_state",
            message="A finished job cannot succeed.",
        )
    job.status = JobStatus.SUCCEEDED
    job.progress = 100
    job.completed_at = datetime.now(UTC)
    if result_reference is not None:
        job.result_reference = result_reference
    await record_event(
        session,
        organisation_id=job.organisation_id,
        actor_user_id=job.created_by_user_id,
        action=ACTION_JOB_SUCCEEDED,
        resource_type="job",
        resource_id=str(job.id),
        metadata={
            "job_type": job.job_type,
            "input_reference": job.input_reference,
            "result_reference": job.result_reference,
            "attempt_count": job.attempt_count,
        },
    )
    await session.commit()
    await session.refresh(job)
    return job


async def fail(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    error_code: str,
    error_message: str,
) -> Job:
    """Transition a job to ``failed`` with the error surface, and audit it.

    Called by tasks for permanent failures and by the retries-exhausted actor
    when transient retries ran out. Idempotent on an already-``failed`` job
    (so a re-delivered message cannot double-audit); a ``succeeded`` or
    ``cancelled`` job is a 409.
    """
    job = await _get_job(session, job_id=job_id)
    if job.status == JobStatus.FAILED:
        return job
    if _terminal(job.status):
        raise ConflictError(
            code="job_in_terminal_state",
            message="A finished job cannot fail.",
        )
    job.status = JobStatus.FAILED
    job.error_code = error_code
    job.error_message = error_message
    job.completed_at = datetime.now(UTC)
    await record_event(
        session,
        organisation_id=job.organisation_id,
        actor_user_id=job.created_by_user_id,
        action=ACTION_JOB_FAILED,
        resource_type="job",
        resource_id=str(job.id),
        metadata={
            "job_type": job.job_type,
            "input_reference": job.input_reference,
            "error_code": error_code,
            "error_message": error_message,
            "attempt_count": job.attempt_count,
        },
    )
    await session.commit()
    await session.refresh(job)
    return job
