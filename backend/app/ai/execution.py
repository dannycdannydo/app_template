"""The ``ai.execute`` durable job (v0.7 Scope §6.6, blueprint §18, ADR-0017).

Document-scale AI work runs as a durable job, not in an HTTP handler (BP §18):
the demonstration feature persists a ``queued`` job row whose
``input_reference`` is the private storage reference, then enqueues this actor
on the ``ai`` queue. The broker message carries only the job id — never file
bytes (v0.7 Scope §2) — and the worker passes the storage reference through
``AIService.execute`` on every idempotent attempt so the service resolves it
to a bounded provider-neutral attachment (v0.7 Scope §2/§6.6).

One execution maps to exactly one AI request id, derived deterministically from
the durable job id (``job_id.hex``). That makes the §6.5 idempotency key
structural: a re-delivered message re-uses the same execution id, so
:class:`~app.ai.errors.AIRequestReplayError` from ``AIService.execute`` means a
previous attempt already reserved (and settled) the durable request row. The
worker reconciles the job to the winning attempt's terminal state instead of
re-dispatching (v0.7 Scope §6.5/§6.6), so a retried message can never
double-charge budget or duplicate a terminal output record.

AI failures are terminal for the job. The ``AIService`` already applies the
task's bounded internal retry/repair policy (Scope §6.4); by the time an
:class:`~app.ai.errors.AIError` reaches the worker the execution has settled
(post-dispatch failures) or never reserved a row (pre-dispatch validation/
policy/routing failures). Either way re-running the identical job would be a
replay or reproduce the same permanent failure, so the worker marks the durable
row ``failed`` and raises :class:`~app.modules.jobs.service.JobPermanentError`
so the Retries middleware never retries it. Non-AI exceptions (a database
outage before reservation) propagate for the bounded Dramatiq retry, which is
the only retry layer that can usefully re-attempt.

The handler is deliberately separate from its actor declaration so a test can
re-declare it bound to its own broker (the same pattern as
``app.modules.files.tasks``). The actor runs on the ``ai`` queue (blueprint §18
example queues) so AI workloads never compete with the ``default`` or
``documents`` queues.
"""

from __future__ import annotations

import uuid

import dramatiq
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import runtime
from app.ai.errors import AIError, AIRequestReplayError
from app.ai.persistence.queries import (
    ai_latest_attempt_statement,
    ai_winning_attempt_statement,
)
from app.ai.persistence.service import AIPersistencePortImpl
from app.ai.schemas import AIRequest
from app.core.logging import bind_worker_context
from app.db.session import async_session_factory
from app.modules.jobs import service as jobs_service

#: The durable ``job_type`` this task owns (v0.7 Scope §2/§6.6). The
#: demonstration service names it when it writes the row, so the constant
#: lives with the task that owns the identity.
JOB_TYPE_AI_EXECUTE = "ai.execute"

#: The queue AI workloads run on (blueprint §18 example queues: default,
#: documents, integrations, ai, emails). The retries-exhausted finalizer keeps
#: running on the infrastructure ``default`` queue (jobs.tasks).
HANDLER_QUEUE = "ai"

#: The single demonstrated task this job executes (v0.7 Scope §2). The job is
#: provider-neutral and task-agnostic in shape; the template ships the one
#: non-product demonstration task and a derived application adds its own.
DEMO_TASK = "document.classify"

#: Permanent error codes the AI execution job records on the durable row when
#: the referenced object cannot become valid task input (before any dispatch,
#: so no ``ai_requests`` row is created).
ERROR_CODE_INVALID_JOB_CONTEXT = "invalid_ai_job_context"
#: A re-delivered message whose execution id is still ``running`` — a previous
#: attempt reserved budget but never settled (a crashed worker). Conservative:
#: the job fails, the reservation is held until the retention sweep reconciles
#: the stale row (v0.7 Scope §6.5), so a budget is never silently released.
ERROR_CODE_REQUEST_IN_PROGRESS = "ai_request_in_progress"

logger = structlog.get_logger()


def request_id_for_job(job_id: uuid.UUID) -> str:
    """The deterministic AI request id for one durable job.

    Derived from the job id so a re-delivered message reconstructs the same
    execution id without carrying it in the broker message: the §6.5
    idempotency key is structural, not metadata (v0.7 Scope §6.6).
    """
    return job_id.hex


async def execute_ai_task(job_id: str) -> None:
    """Run one attempt of the ``ai.execute`` durable job.

    Loads the durable row, passes the private storage reference through
    :class:`~app.ai.service.AIService` so the service resolves it to a bounded
    provider-neutral attachment on every attempt (v0.7 Scope §2/§6.6), and
    dispatches with the job-derived request id. On a replay the job is
    reconciled to the winning attempt's terminal state; on any other AI
    failure the job is marked ``failed`` permanently.
    """
    job_uuid = uuid.UUID(job_id)
    # The deterministic request id is derived before the first log line so
    # every AI-job log entry binds ``ai_request_id`` (v0.7 Scope §6.7, BP §28)
    # — including ``ai.execute.started`` itself.
    request_id = request_id_for_job(job_uuid)
    bind_worker_context(job_id=str(job_uuid), ai_request_id=request_id)
    logger.info("ai.execute.started")
    async with async_session_factory() as session:
        job = await jobs_service.get_job_for_task(session, job_id=job_uuid)
        if jobs_service.is_terminal(job.status):
            logger.info("ai.execute.skipped", reason="terminal_state")
            return

        if job.job_type != JOB_TYPE_AI_EXECUTE:
            await jobs_service.fail(
                session,
                job_id=job_uuid,
                error_code=ERROR_CODE_INVALID_JOB_CONTEXT,
                error_message="The AI job has an invalid task type.",
            )
            logger.warning(
                "ai.execute.failed",
                error_code=ERROR_CODE_INVALID_JOB_CONTEXT,
                reason="wrong_job_type",
            )
            raise jobs_service.JobPermanentError("the AI job context is invalid")

        await jobs_service.mark_running(session, job_id=job_uuid)
        if job.created_by_user_id is None:
            # A durable AI job always records the initiating user (the demo
            # service passes the authenticated caller). A row without one is a
            # malformed/foreign context: fail permanently rather than retry.
            failure = _PermanentJobFailure(
                ERROR_CODE_INVALID_JOB_CONTEXT,
                "The AI job has no initiating user.",
            )
            await _fail_permanent(session, job_uuid, failure)
            raise jobs_service.JobPermanentError(failure.message)
        if not job.input_reference:
            failure = _PermanentJobFailure(
                ERROR_CODE_INVALID_JOB_CONTEXT,
                "The AI job has no storage reference to process.",
            )
            await _fail_permanent(session, job_uuid, failure)
            raise jobs_service.JobPermanentError(failure.message)

        try:
            result = await runtime.get_ai_service().execute(
                AIRequest(
                    task=DEMO_TASK,
                    storage_reference=job.input_reference,
                    organisation_id=job.organisation_id,
                    user_id=job.created_by_user_id,
                    metadata={"source": "ai_demo", "job_id": str(job_uuid)},
                ),
                recorder=AIPersistencePortImpl(session),
                request_id=request_id,
            )
        except AIRequestReplayError:
            # A previous attempt reserved this execution id. Reconcile the job
            # to the durable row's state instead of re-dispatching (v0.7 Scope
            # §6.5/§6.6): a re-delivered message never double-charges budget or
            # duplicates a terminal output record.
            await _reconcile_replay(session, job_uuid, job.organisation_id, request_id)
            return
        except AIError as exc:
            await _fail_permanent(
                session,
                job_uuid,
                _PermanentJobFailure(exc.error_code, exc.args[0] if exc.args else str(exc)),
            )
            logger.warning("ai.execute.failed", error_code=exc.error_code)
            raise jobs_service.JobPermanentError(
                f"the AI execution failed ({exc.error_code})"
            ) from exc

        await jobs_service.succeed(
            session,
            job_id=job_uuid,
            result_reference=result.request_id,
        )
        logger.info(
            "ai.execute.succeeded",
            ai_request_id=result.request_id,
            provider=result.routing.provider,
            model=result.routing.model,
        )


class _PermanentJobFailure(Exception):
    """Internal control-flow carrier for a pre-dispatch permanent failure."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


async def _reconcile_replay(
    session: AsyncSession,
    job_uuid: uuid.UUID,
    organisation_id: uuid.UUID,
    request_id: str,
) -> None:
    """Settle the durable job to match an existing AI request's outcome.

    A re-delivered message whose execution id already has durable rows. The
    winning attempt (any ``succeeded`` row) is the execution-level outcome: a
    multi-attempt execution settles every non-winning attempt ``failed`` and
    exactly one ``succeeded``, so checking across all attempts — not just
    attempt 1 — means a transient first failure followed by a later success is
    reconciled correctly even if the worker crashed between the winning
    settlement and ``jobs_service.succeed`` (v0.7 Scope §6.4/§6.6).

    With no succeeded attempt, the latest row's status is the outcome. A row
    still ``running`` or ``queued`` is a crashed previous attempt; the job
    fails conservatively and the retention sweep reconciles the stale
    reservation later (v0.7 Scope §6.5).
    """
    winning = await session.scalar(ai_winning_attempt_statement(organisation_id, request_id))
    if winning is not None:
        await jobs_service.succeed(session, job_id=job_uuid, result_reference=request_id)
        logger.info("ai.execute.reconciled", ai_request_id=request_id, status="succeeded")
        return
    latest = await session.scalar(ai_latest_attempt_statement(organisation_id, request_id))
    if latest is None:
        # No row despite the replay signal: a transient race or a partially
        # rolled-back reservation. Fail permanently so the message is not
        # retried as a replay forever; an operator can re-submit if needed.
        await _fail_permanent(
            session,
            job_uuid,
            _PermanentJobFailure(
                ERROR_CODE_REQUEST_IN_PROGRESS,
                "The AI request could not be reconciled.",
            ),
        )
        raise jobs_service.JobPermanentError("the AI request row vanished during replay")
    if latest.status.value in ("running", "queued"):
        await _fail_permanent(
            session,
            job_uuid,
            _PermanentJobFailure(
                ERROR_CODE_REQUEST_IN_PROGRESS,
                "A previous attempt of this AI request is still in progress.",
            ),
        )
        raise jobs_service.JobPermanentError("the AI request is still in progress")
    await _fail_permanent(
        session,
        job_uuid,
        _PermanentJobFailure(
            latest.error_code or "ai_execution_failed",
            latest.error_code or "The AI request previously failed.",
        ),
    )
    raise jobs_service.JobPermanentError("the AI request previously failed")


async def _fail_permanent(
    session: AsyncSession, job_uuid: uuid.UUID, failure: _PermanentJobFailure
) -> None:
    """Mark the durable job failed with a safe error code before raising."""
    await jobs_service.fail(
        session,
        job_id=job_uuid,
        error_code=failure.error_code,
        error_message=failure.message,
    )
    logger.warning("ai.execute.failed", error_code=failure.error_code)


execute_ai_task_actor = dramatiq.actor(
    queue_name=HANDLER_QUEUE,
    **jobs_service.retry_policy(),
)(execute_ai_task)
