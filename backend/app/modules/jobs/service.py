"""Durable job services and the bounded retry policy (Scope §6.4, blueprint §18).

The job service is the single owner of the durable ``jobs`` table:

- :func:`schedule_job` is the only way a durable job is created (plan P3): it
  writes the ``queued`` row and its ``job.dispatch_requested`` outbox event in
  one transaction, sets the event id as the job's dispatch identity and
  commits once. No producer publishes to the broker: the coordinator (plan P3)
  turns the durable event into the reference-only Dramatiq message (blueprint
  §19 — Redis executes, PostgreSQL provides durability).
- :func:`claim_dispatch` is the atomic worker-side claim (durable delivery
  plan P2): it transitions ``queued`` -> ``running`` (or takes over an expired
  lease), assigns a dispatch identity to a legacy row on first claim,
  increments ``attempt_count``, sets the execution lease and rotates the
  attempt-distinguishing ``owner_token``. A duplicate whose lease is still
  live is *deferred*, never executed concurrently.
- :func:`settle_retryable_failure` closes a transient attempt and atomically
  writes its delayed next dispatch or terminal exhaustion;
  :func:`update_progress`, :func:`succeed` and
  :func:`fail` are the owner-checked mutation helpers the worker tasks call
  through the shared execution wrapper (``app.modules.jobs.execution``). Every
  mutation verifies the owner token captured at claim time, so an
  expired/stale attempt can never overwrite a newer owner — including after an
  expired-lease takeover, which rotates the token — and terminal settlement
  clears the lease.
- :func:`verify_ownership` is the reusable worker-side ownership guard (plan
  P2): domain services and worker tasks pass a :class:`JobOwnership` value and
  it locks the job and its open attempt in the caller's transaction, raising
  :class:`StaleDispatchError` when a superseded attempt tries a consequential
  mutation. :func:`enforce_attempt_ceiling_locked` and
  :func:`close_running_attempt_abandoned` support bounded coordinator recovery.
- :func:`settle_after_retries_exhausted` remains only as a rolling-deployment
  bridge for older broker messages; PostgreSQL owns all new retry decisions.

The retry policy: transient errors are retried up to ``MAX_ATTEMPTS``;
permanent validation errors raise :class:`JobPermanentError`, which tasks
declare in their ``throws`` so the Retries middleware never retries them (and
the task marks the durable row ``failed`` itself). The standard actor time
limit (600,000 ms by default) is part of the shared policy, and the execution
lease exceeds it by at least 60 seconds (validated at startup).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, ErrorDetail, NotFoundError, ValidationError
from app.db.conventions import uuid7
from app.modules.audit.service import (
    ACTION_JOB_FAILED,
    ACTION_JOB_SUCCEEDED,
    record_event,
)
from app.modules.jobs.models import Job, JobAttempt, JobAttemptStatus, JobStatus
from app.modules.jobs.queries import (
    org_jobs_count_statement,
    org_scoped_jobs_statement,
    running_attempt_statement,
)
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus
from app.modules.outbox.service import create_dispatch_event, retry_dispatch_key
from app.observability.metrics import JOBS_ENQUEUED_TOTAL, JOBS_FAILED_TOTAL, JOBS_SUCCEEDED_TOTAL

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
ERROR_CODE_DISPATCH_INVALID = "job_dispatch_invalid"
ERROR_CODE_TRANSIENT = "job_transient_failure"

# Allow-listed exhaustion hooks keyed by job type. When retries for a job
# type are exhausted, the generic finalizer calls the registered hook (if
# any) after settling the job failed, so domain state (e.g. a notification
# delivery row) can be finalized consistently in one transaction.
_EXHAUSTION_HOOKS: dict[str, Any] = {}


def register_exhaustion_hook(job_type: str, fn: Any) -> None:
    """Register an exhaustion finalization hook for a job type.

    Only allow-listed job types may register a hook; an unknown type raises.
    """
    _EXHAUSTION_HOOKS[job_type] = fn


def get_exhaustion_hook(job_type: str) -> Any | None:
    return _EXHAUSTION_HOOKS.get(job_type)


class JobPermanentError(Exception):
    """Raised by a task for a failure retries cannot fix.

    Tasks declare this exception in their ``throws`` (via
    :func:`retry_policy`), so the Retries middleware fails the message without
    retrying it. The task must mark the durable job ``failed`` itself (via
    :func:`fail`) before raising, so the durable row and the message agree.
    """


class DispatchDeferredError(Exception):
    """Raised by the execution wrapper when another live attempt owns the job.

    A duplicate message that finds a non-expired execution lease on the job
    row raises this transient error (it is deliberately *not* in the retry
    policy's ``throws``), so the Retries middleware retries it. The
    retries-exhausted finalizer refuses to settle a dispatch that still has a
    live lease, so a deferred duplicate can never fail a job a live attempt is
    working on.
    """

    def __init__(self, job_id: uuid.UUID, deferred_until: datetime) -> None:
        super().__init__(f"job {job_id} is leased until {deferred_until.isoformat()}")
        self.job_id = job_id
        self.deferred_until = deferred_until


class StaleDispatchError(Exception):
    """Raised when an attempt no longer owns the job's current dispatch.

    Worker mutations (progress, success, failure, release) verify the owner
    token captured at claim time against the job row. A mismatch means the
    attempt was superseded (a takeover or retry rotated the token, or a newer
    dispatch replaced it) or the row reached a terminal state under a
    different owner; the execution wrapper catches this and treats the message
    as a no-op, so a stale attempt cannot update progress, succeed, fail or
    release over a newer owner.
    """

    def __init__(
        self,
        job_id: uuid.UUID,
        current_owner_token: uuid.UUID | None,
        captured_owner_token: uuid.UUID | None,
    ):
        super().__init__(
            f"attempt owned by token {captured_owner_token} no longer owns job {job_id} "
            f"(current owner token: {current_owner_token})"
        )
        self.job_id = job_id
        self.current_owner_token = current_owner_token
        self.captured_owner_token = captured_owner_token


class ClaimOutcome(StrEnum):
    """Outcome of an atomic worker-side claim (durable delivery plan P2)."""

    CLAIMED = "claimed"
    DEFERRED = "deferred"
    EXHAUSTED = "exhausted"
    STALE = "stale"


def _retry_delay(attempt_number: int) -> timedelta:
    """Bound the durable retry delay; Redis middleware is only assistance."""
    return timedelta(milliseconds=min(RETRY_MIN_BACKOFF_MS * 2 ** (attempt_number - 1), 60_000))


async def _running_attempt(session: AsyncSession, job: Job) -> JobAttempt | None:
    if job.owner_token is None:
        return None
    return await session.scalar(
        running_attempt_statement(job_id=job.id, owner_token=job.owner_token)
    )


def _close_attempt(
    attempt: JobAttempt | None,
    *,
    status: JobAttemptStatus,
    now: datetime,
    error_code: str | None = None,
) -> None:
    if attempt is None:
        return  # Legacy pre-migration owner; rolling deployment compatibility.
    attempt.status = status
    attempt.completed_at = now
    attempt.error_code = error_code


async def _run_exhaustion_hook(session: AsyncSession, job: Job, *, failure_code: str) -> None:
    hook = get_exhaustion_hook(job.job_type)
    if hook is not None:
        await hook(session, job_id=job.id, failure_code=failure_code)


@dataclass(frozen=True)
class ClaimResult:
    """The outcome of :func:`claim_dispatch`.

    ``job`` is the durable row (``CLAIMED``: the fresh owner; ``EXHAUSTED``:
    the claim that reached the global limit; ``STALE``: a previously terminal
    row; ``DEFERRED``: the leased or not-yet-due row). ``dispatch_id`` is the
    dispatch the caller now owns (``CLAIMED`` only); ``owner_token`` is the
    attempt-distinguishing credential rotated for this claim (``CLAIMED``
    only); ``deferred_until`` is the live lease bound a deferred duplicate
    must wait past; ``taken_over`` is true when an expired lease was claimed
    by a new attempt.
    """

    outcome: ClaimOutcome
    job: Job | None = None
    dispatch_id: uuid.UUID | None = None
    owner_token: uuid.UUID | None = None
    deferred_until: datetime | None = None
    taken_over: bool = False


def retry_policy() -> dict[str, Any]:
    """Return the standard Dramatiq actor options encoding the retry policy.

    Every job task spreads these into its ``@dramatiq.actor`` declaration::

        @dramatiq.actor(queue_name="documents", **jobs_service.retry_policy())
        async def process_file(job_id: str) -> None:
            ...

    ``max_retries`` and the exhausted callback remain transport/rolling-release
    assistance. PostgreSQL's attempt ledger is the global bound and durable
    terminal boundary. ``throws`` prevents retries of permanent errors;
    ``time_limit`` is the standard task limit, which the lease exceeds.
    """
    return {
        "max_retries": MAX_ATTEMPTS - 1,
        "min_backoff": RETRY_MIN_BACKOFF_MS,
        "throws": (JobPermanentError,),
        "on_retry_exhausted": MARK_FAILED_AFTER_RETRIES_ACTOR,
        "time_limit": get_settings().job_task_time_limit_ms,
    }


def _execution_lease_seconds() -> int:
    """Return the configured execution-lease duration in seconds."""
    return get_settings().job_execution_lease_seconds


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

    ``populate_existing`` forces the live row to overwrite any identity-mapped
    copy the calling session still holds, so the ownership checks compare the
    database's current token — not a cached snapshot captured before a
    cross-session takeover (plan P2, AC5).
    """
    job = await session.scalar(
        select(Job).where(Job.id == job_id).execution_options(populate_existing=True)
    )
    if job is None:
        raise _not_found()
    return job


async def _get_job_locked(session: AsyncSession, *, job_id: uuid.UUID) -> Job:
    """Return one job row locked for an ownership mutation.

    ``FOR UPDATE`` serialises concurrent mutations of the same row: two
    attempts racing to claim, settle or release the same job cannot interleave
    their read-modify-write cycles (plan P2 atomicity). ``populate_existing``
    additionally ensures the locked read repopulates any stale identity-mapped
    ``Job`` the caller still references, so the subsequent ownership checks see
    a takeover that happened in another session (plan P2, AC5).
    """
    job = await session.scalar(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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


def _verify_owner(job: Job, owner_token: uuid.UUID | None) -> None:
    """Raise :class:`StaleDispatchError` when an attempt is not the current owner.

    ``owner_token=None`` covers legacy settlement of a row that has never been
    claimed (pre-claim permanent failures from old releases); once a row
    carries an ownership credential, every mutation must name it.
    """
    if job.owner_token is None:
        return
    if job.owner_token != owner_token:
        raise StaleDispatchError(job.id, job.owner_token, owner_token)


@dataclass(frozen=True)
class JobOwnership:
    """The attempt credential a worker captured at claim time (plan P2).

    Domain services and the shared execution wrapper pass this value — never a
    detached ORM ``Job`` — so every consequential worker mutation re-locks and
    re-verifies the owning job in its own transaction before it commits.
    """

    job_id: uuid.UUID
    owner_token: uuid.UUID
    organisation_id: uuid.UUID


async def verify_ownership(
    session: AsyncSession,
    ownership: JobOwnership,
    *,
    lock: bool = True,
) -> Job:
    """Verify (and by default lock) the owning job and its open attempt.

    Locks the job row ``FOR UPDATE`` and confirms the captured owner token is
    still current, the job is ``running`` and exactly one matching attempt is
    open, so a caller can commit a consequential domain mutation in the same
    transaction without a superseded attempt ever landing it (plan P2, AC5).
    Raises :class:`StaleDispatchError` for a stale, terminal, re-leased or
    differently-owned job, which the execution wrapper acknowledges as a
    no-op/abandoned attempt rather than a retryable failure.

    ``lock=False`` performs the same checks without the row lock: it is the
    immediate pre-provider-call revalidation, where holding a row lock across a
    network call would be undesirable.
    """
    if lock:
        job = await _get_job_locked(session, job_id=ownership.job_id)
    else:
        job = await _get_job(session, job_id=ownership.job_id)
    if job.organisation_id != ownership.organisation_id:
        raise StaleDispatchError(job.id, job.owner_token, ownership.owner_token)
    if job.status != JobStatus.RUNNING or job.owner_token is None:
        raise StaleDispatchError(job.id, job.owner_token, ownership.owner_token)
    if job.owner_token != ownership.owner_token:
        raise StaleDispatchError(job.id, job.owner_token, ownership.owner_token)
    if await _running_attempt(session, job) is None:
        raise StaleDispatchError(job.id, job.owner_token, ownership.owner_token)
    return job


async def enforce_attempt_ceiling_locked(session: AsyncSession, job: Job) -> bool:
    """Fail a locked job terminally once it reaches the global attempt ceiling.

    Reconciliation calls this before granting a replacement dispatch, so a job
    that already consumed every allowed attempt settles failed in PostgreSQL
    instead of receiving a fresh nominal retry budget (plan P2, AC3). Returns
    ``True`` when the job was settled exhausted; the caller owns the commit.
    """
    if job.attempt_count < MAX_ATTEMPTS:
        return False
    await _fail_locked(
        session,
        job,
        error_code=ERROR_CODE_RETRIES_EXHAUSTED,
        error_message=ERROR_MESSAGE_RETRIES_EXHAUSTED,
        attempt_status=JobAttemptStatus.EXHAUSTED,
    )
    await _run_exhaustion_hook(session, job, failure_code=ERROR_CODE_RETRIES_EXHAUSTED)
    return True


async def close_running_attempt_abandoned(
    session: AsyncSession, job: Job, *, now: datetime
) -> None:
    """Close the owning attempt as ``abandoned`` under a locked job.

    Expired-running recovery (plan P2) calls this in the same transaction that
    rotates the dispatch/owner boundary, so the dead attempt is durably closed
    without touching the newer owner.
    """
    attempt = await _running_attempt(session, job)
    _close_attempt(attempt, status=JobAttemptStatus.ABANDONED, now=now)


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


async def find_job_by_input_reference(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    job_type: str,
    input_reference: str,
) -> Job | None:
    """Return the newest durable job for one org/type/input triple, if any.

    Plan P6 completion replay: a replayed completion must return the same
    processing job rather than schedule a duplicate, so the transition is
    idempotent under parallel or retried calls.
    """
    return await session.scalar(
        select(Job)
        .where(
            Job.organisation_id == organisation_id,
            Job.job_type == job_type,
            Job.input_reference == input_reference,
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(1)
    )


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


async def schedule_job(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    job_type: str,
    input_reference: str,
    actor_user_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    commit: bool = True,
) -> Job:
    """Write the durable ``queued`` row and its dispatch event in one transaction.

    The transactional scheduling boundary (durable delivery plan P3, blueprint
    §19): the job row and its ``job.dispatch_requested`` outbox event are
    added to ``session`` and committed together, so a rollback leaves neither
    row and an unavailable Redis never prevents the API from committing and
    returning the durable queued job. The outbox event id becomes the job's
    dispatch identity, and the event carries only the job id (reference-only
    message boundary). The coordinator (plan P3) is the only production
    component that turns that event into a Dramatiq message; no producer calls
    an actor's ``send()``.

    ``job_id`` optionally supplies a pre-generated id so a caller can add
    companion rows (e.g. a pre-schedule ``ai_requests`` linkage row) to the
    same session before this call; the single commit then makes both rows
    atomic (v0.7 Scope §5.8).

    ``commit=False`` leaves the row and its outbox event pending on the
    caller's session so a caller that must make a wider change atomic (plan P6:
    file completion transition, audit, job and dispatch in one transaction)
    owns the single commit. The caller then refreshes/returns the same job.
    """
    # The dispatch event id is the job's delivery identity: it is generated
    # first so both the job row and the outbox row agree on the dispatch.
    dispatch_event_id = uuid7()
    job = Job(
        id=job_id or uuid7(),
        organisation_id=organisation_id,
        job_type=job_type,
        status=JobStatus.QUEUED,
        progress=0,
        input_reference=input_reference,
        created_by_user_id=actor_user_id,
        dispatch_id=dispatch_event_id,
    )
    session.add(job)
    await session.flush()
    await create_dispatch_event(
        session,
        organisation_id=organisation_id,
        job_id=job.id,
        event_id=dispatch_event_id,
    )
    if commit:
        await session.commit()
        await session.refresh(job)
        # The metric is a committed-success signal: increment only after the
        # job's transaction has durably committed, so a later rollback can
        # never report a job that was never enqueued (plan P6 should-fix).
        record_job_enqueued(job)
    return job


def record_job_enqueued(job: Job) -> None:
    """Record one durably-committed job enqueue on the low-cardinality metric.

    Callers that compose :func:`schedule_job` with ``commit=False`` into a
    wider transaction (the plan P6 atomic file completion) invoke this *after*
    their own commit, so the counter reflects durable enqueues only.
    """
    JOBS_ENQUEUED_TOTAL.labels(job_type=job.job_type).inc()


async def claim_dispatch(session: AsyncSession, *, job_id: uuid.UUID) -> ClaimResult:
    """Atomically claim the next execution attempt of ``job_id`` (plan P2).

    ``FOR UPDATE`` makes the claim atomic under concurrency: a duplicate that
    arrives while the first claim's transaction is still open blocks on the
    row lock, then observes the fresh lease and returns ``DEFERRED``.

    - ``queued`` with a not-yet-due current event: the result is ``DEFERRED``.
      A duplicate already in transport may wait for that PostgreSQL due time
      and claim it; the later coordinator publication is an at-least-once
      duplicate constrained by the same ownership and global attempt limit.
    - due ``queued`` -> ``running``: the attempt claims the dispatch, increments
      ``attempt_count``, records ``started_at`` on the first attempt, rotates
      the attempt-distinguishing ``owner_token`` and sets the execution lease.
      A legacy row with no dispatch identity receives one atomically on this
      first claim (old one-argument broker messages keep working).
    - ``running`` with a non-expired lease: another live attempt owns the
      dispatch; the result is ``DEFERRED`` with the lease bound.
    - ``running`` with an expired lease: the dead attempt is taken over
      (``taken_over=True``); the ``owner_token`` is rotated so the dead
      attempt's captured credential is superseded and cannot mutate over the
      new owner.
    - global attempt limit reached: the claim atomically fails the job and
      returns ``EXHAUSTED``.
    - terminal: the message is a ``STALE`` no-op; terminal states are never
      re-run (acceptance §5.7).
    """
    job = await _get_job_locked(session, job_id=job_id)
    if _terminal(job.status):
        return ClaimResult(outcome=ClaimOutcome.STALE, job=job)
    now = datetime.now(UTC)
    if (
        job.status == JobStatus.RUNNING
        and job.execution_lease_expires_at is not None
        and job.execution_lease_expires_at > now
    ):
        return ClaimResult(
            outcome=ClaimOutcome.DEFERRED,
            job=job,
            deferred_until=job.execution_lease_expires_at,
        )
    if job.status == JobStatus.QUEUED and job.dispatch_id is not None:
        current_event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.id == job.dispatch_id)
        )
        if (
            current_event is not None
            and current_event.status == OutboxEventStatus.PENDING
            and current_event.available_at > now
        ):
            return ClaimResult(
                outcome=ClaimOutcome.DEFERRED,
                job=job,
                deferred_until=current_event.available_at,
            )
    taken_over = job.status == JobStatus.RUNNING
    if job.attempt_count >= MAX_ATTEMPTS:
        # A lost broker message or a restarted coordinator cannot reset the
        # budget: the locked PostgreSQL count is the global ceiling.
        await _fail_locked(
            session,
            job,
            error_code=ERROR_CODE_RETRIES_EXHAUSTED,
            error_message=ERROR_MESSAGE_RETRIES_EXHAUSTED,
            attempt_status=JobAttemptStatus.EXHAUSTED,
        )
        await _run_exhaustion_hook(session, job, failure_code=ERROR_CODE_RETRIES_EXHAUSTED)
        await session.commit()
        JOBS_FAILED_TOTAL.labels(job_type=job.job_type).inc()
        return ClaimResult(outcome=ClaimOutcome.EXHAUSTED, job=job)
    if taken_over:
        old_attempt = await _running_attempt(session, job)
        _close_attempt(old_attempt, status=JobAttemptStatus.ABANDONED, now=now)
    if job.dispatch_id is None:
        # Legacy row (published before the outbox cutover, or by an earlier
        # release): assign a dispatch identity on the first claim so
        # ownership checks apply from here on.
        job.dispatch_id = uuid7()
    # Rotate the attempt-distinguishing ownership token on every claim. An
    # expired-lease takeover may retain the dispatch id, while a durably
    # scheduled retry has a new one; either way the old credential cannot
    # mutate over the new owner.
    job.owner_token = uuid7()
    job.status = JobStatus.RUNNING
    job.attempt_count = job.attempt_count + 1
    if job.started_at is None:
        job.started_at = now
    job.execution_lease_expires_at = now + timedelta(seconds=_execution_lease_seconds())
    session.add(
        JobAttempt(
            job_id=job.id,
            dispatch_id=job.dispatch_id,
            owner_token=job.owner_token,
            attempt_number=job.attempt_count,
            status=JobAttemptStatus.RUNNING,
            lease_expires_at=job.execution_lease_expires_at,
            started_at=now,
            taken_over=taken_over,
        )
    )
    await session.commit()
    await session.refresh(job)
    return ClaimResult(
        outcome=ClaimOutcome.CLAIMED,
        job=job,
        dispatch_id=job.dispatch_id,
        owner_token=job.owner_token,
        taken_over=taken_over,
    )


async def settle_retryable_failure(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    owner_token: uuid.UUID,
) -> JobStatus | None:
    """Atomically close an attempt and schedule retry or terminal exhaustion.

    A duplicate callback can never create a second event. The outbox event
    carries only the job id, and its due time is owned by PostgreSQL.
    """
    job = await _get_job_locked(session, job_id=job_id)
    _verify_owner(job, owner_token)
    if _terminal(job.status):
        return None
    if job.status != JobStatus.RUNNING:
        return None
    now = datetime.now(UTC)
    attempt = await _running_attempt(session, job)
    if job.attempt_count >= MAX_ATTEMPTS:
        await _fail_locked(
            session,
            job,
            error_code=ERROR_CODE_RETRIES_EXHAUSTED,
            error_message=ERROR_MESSAGE_RETRIES_EXHAUSTED,
            attempt_status=JobAttemptStatus.EXHAUSTED,
        )
        await _run_exhaustion_hook(session, job, failure_code=ERROR_CODE_RETRIES_EXHAUSTED)
        await session.commit()
        JOBS_FAILED_TOTAL.labels(job_type=job.job_type).inc()
        return JobStatus.FAILED
    _close_attempt(
        attempt,
        status=JobAttemptStatus.RETRY_SCHEDULED,
        now=now,
        error_code=ERROR_CODE_TRANSIENT,
    )
    next_dispatch_id = uuid7()
    next_event = await create_dispatch_event(
        session,
        organisation_id=job.organisation_id,
        job_id=job.id,
        event_id=next_dispatch_id,
        deduplication_key=retry_dispatch_key(job.id, attempt_number=job.attempt_count),
    )
    next_event.available_at = now + _retry_delay(job.attempt_count)
    job.status = JobStatus.QUEUED
    job.dispatch_id = next_dispatch_id
    job.execution_lease_expires_at = None
    await session.commit()
    return JobStatus.QUEUED


async def update_progress(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    progress: int,
    owner_token: uuid.UUID | None = None,
) -> Job:
    """Record a task's progress (0-100) on the owned job and renew the lease.

    Renewing the lease on progress protects active document work: a worker
    that keeps reporting progress keeps its ownership, while a worker that
    stalls is eventually taken over. A terminal job whose owner matches is a
    genuine conflict (409); a terminal or leased job owned by another dispatch
    raises :class:`StaleDispatchError` instead.
    """
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
    job = await _get_job_locked(session, job_id=job_id)
    if _terminal(job.status):
        _verify_owner(job, owner_token)
        raise ConflictError(
            code="job_in_terminal_state",
            message="A finished job cannot be updated.",
        )
    _verify_owner(job, owner_token)
    if job.status != JobStatus.RUNNING:
        raise ConflictError(
            code="job_not_running", message="Only a running job can report progress."
        )
    job.progress = progress
    lease_expires_at = datetime.now(UTC) + timedelta(seconds=_execution_lease_seconds())
    job.execution_lease_expires_at = lease_expires_at
    attempt = await _running_attempt(session, job)
    if attempt is not None:
        attempt.lease_expires_at = lease_expires_at
    await session.commit()
    await session.refresh(job)
    return job


async def succeed(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    result_reference: str | None = None,
    owner_token: uuid.UUID | None = None,
) -> Job:
    """Transition the owned job to ``succeeded`` with progress 100, and audit it.

    Idempotent: calling it again on an already-``succeeded`` job is a no-op
    (a retried message that raced the original completion), so no second audit
    row is written. Owner-checked: a stale attempt cannot succeed a job a
    newer owner holds, and the execution lease is cleared on terminal
    settlement.
    """
    job = await _get_job_locked(session, job_id=job_id)
    if job.status == JobStatus.SUCCEEDED:
        return job
    _verify_owner(job, owner_token)
    if _terminal(job.status):
        raise ConflictError(
            code="job_in_terminal_state",
            message="A finished job cannot succeed.",
        )
    job.status = JobStatus.SUCCEEDED
    job.progress = 100
    completed_at = datetime.now(UTC)
    job.completed_at = completed_at
    job.execution_lease_expires_at = None
    _close_attempt(
        await _running_attempt(session, job),
        status=JobAttemptStatus.SUCCEEDED,
        now=completed_at,
    )
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
    JOBS_SUCCEEDED_TOTAL.labels(job_type=job.job_type).inc()
    return job


async def _fail_locked(
    session: AsyncSession,
    job: Job,
    *,
    error_code: str,
    error_message: str,
    attempt_status: JobAttemptStatus = JobAttemptStatus.FAILED,
) -> None:
    """Write failure, attempt outcome and audit before the caller commits."""
    now = datetime.now(UTC)
    _close_attempt(
        await _running_attempt(session, job),
        status=attempt_status,
        now=now,
        error_code=error_code,
    )
    job.status = JobStatus.FAILED
    job.error_code = error_code
    job.error_message = error_message
    job.completed_at = now
    job.execution_lease_expires_at = None
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


async def fail(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    error_code: str,
    error_message: str,
    owner_token: uuid.UUID | None = None,
) -> Job:
    """Transition the owned job to ``failed`` with the error surface, and audit it.

    Called by tasks for permanent failures and by the retries-exhausted
    finalizer when transient retries ran out. Idempotent on an already-
    ``failed`` job (so a re-delivered message cannot double-audit); owner-
    checked so a stale attempt cannot fail a newer owner's dispatch; the
    execution lease is cleared on terminal settlement.
    """
    job = await _get_job_locked(session, job_id=job_id)
    if job.status == JobStatus.FAILED:
        return job
    _verify_owner(job, owner_token)
    if _terminal(job.status):
        raise ConflictError(
            code="job_in_terminal_state",
            message="A finished job cannot fail.",
        )
    await _fail_locked(session, job, error_code=error_code, error_message=error_message)
    await session.commit()
    await session.refresh(job)
    JOBS_FAILED_TOTAL.labels(job_type=job.job_type).inc()
    return job


async def settle_after_retries_exhausted(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    exhausted_dispatch_id: uuid.UUID | None = None,
    exhausted_owner_token: uuid.UUID | None = None,
) -> Job | None:
    """Settle the currently owned attempt failed, or ``None`` for a stale message.

    Called by the retries-exhausted finalizer (``app.modules.jobs.tasks``).
    The finalizer settles only the attempt the exhausted message actually
    attempted and only when no live attempt owns it: a terminal job, or a row
    whose current dispatch still carries a non-expired lease (a newer/live
    attempt), means the exhausted message is a stale no-op that must not fail
    the job.

    ``exhausted_dispatch_id`` and ``exhausted_owner_token`` are the dispatch
    and attempt credential the exhausted message actually claimed, stamped
    into the message at claim time by the execution wrapper. Correlation is by
    *attempt*, not dispatch alone: a retry re-claim or an expired-lease
    takeover keeps the dispatch identity while rotating the owner token, so a
    delayed exhausted message for an older attempt of the same dispatch must
    not fail the newer queued/owned attempt (acceptance §5.5: a superseded
    attempt cannot trigger an exhausted failure over a newer owner). When
    either value differs from the row's current state, the message is
    superseded and is acknowledged without settling the newer owner.

    A stamp-less message (``None``/``None``) covers legacy releases sent
    before the stamp existed. The explicit legacy behaviour is to settle the
    current dispatch, but only while the row is still in the legacy state
    (never claimed, so it carries no owner credential). Once the row carries
    an owner token, a stamp-less message can only be a duplicate that
    exhausted without ever claiming (deferral never stamps), so it is a stale
    no-op and must not fail the row around the live owner's release.
    """
    job = await _get_job_locked(session, job_id=job_id)
    if _terminal(job.status):
        return None
    if exhausted_dispatch_id is None and exhausted_owner_token is None:
        # Stamp-less message: settle the current dispatch only for rows that
        # are still in the legacy (never-claimed) state.
        if job.owner_token is not None:
            return None
    else:
        # Stamped message: the exhausted attempt must be the current owner.
        # Either a different dispatch or a rotated owner token means the
        # attempt was superseded; a partially-stamped message is treated as
        # stale rather than risking the newer owner.
        if exhausted_dispatch_id is None or exhausted_dispatch_id != job.dispatch_id:
            return None
        if exhausted_owner_token is None or exhausted_owner_token != job.owner_token:
            return None
    if (
        job.status == JobStatus.RUNNING
        and job.execution_lease_expires_at is not None
        and job.execution_lease_expires_at > datetime.now(UTC)
    ):
        return None
    await _fail_locked(
        session,
        job,
        error_code=ERROR_CODE_RETRIES_EXHAUSTED,
        error_message=ERROR_MESSAGE_RETRIES_EXHAUSTED,
        attempt_status=JobAttemptStatus.EXHAUSTED,
    )
    await _run_exhaustion_hook(session, job, failure_code=ERROR_CODE_RETRIES_EXHAUSTED)
    await session.commit()
    JOBS_FAILED_TOTAL.labels(job_type=job.job_type).inc()
    return job


async def settle_dead_initial_dispatch(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    aggregate_id: uuid.UUID | None,
    organisation_id: uuid.UUID | None,
) -> Job | None:
    """Fail only a current, never-claimed job behind a dead initial event.

    The coordinator owns the event claim and commits this change together
    with its dead-event settlement. A stale dispatch cannot fail a newer job.
    """
    if aggregate_id is None or organisation_id is None:
        return None
    job = await session.scalar(select(Job).where(Job.id == aggregate_id).with_for_update())
    if (
        job is None
        or job.organisation_id != organisation_id
        or job.dispatch_id != event_id
        or job.status != JobStatus.QUEUED
        or job.attempt_count != 0
    ):
        return None
    await _fail_locked(
        session,
        job,
        error_code=ERROR_CODE_DISPATCH_INVALID,
        error_message="The job could not be delivered.",
    )
    await _run_exhaustion_hook(session, job, failure_code=ERROR_CODE_DISPATCH_INVALID)
    return job
