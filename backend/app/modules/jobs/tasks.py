"""Job-infrastructure Dramatiq tasks (Scope §6.4, blueprint §18).

This module is the home of the tasks every job depends on. Today that is a
single actor — ``mark_job_failed_after_retries`` — which the Retries
middleware messages when a job's transient retries are exhausted, so the
durable row records the failure instead of sitting in ``running`` forever.
Domain tasks (the first is the file-processing job, Scope §6.5) live next to
their domain module and are imported by ``app.workers`` alongside this one.

The handler function is deliberately separate from its actor declaration so a
test can re-declare it bound to its own broker; the actor is the thin wrapper
that registers the function under the name ``jobs_service.retry_policy()``
declares in ``on_retry_exhausted``. The ``throws=()`` declaration means
*nothing* this actor raises is retried: it is a finalizer, and if it cannot
reach the database there is nothing a retry would fix (the message fails
loudly and stays visible in the broker's dead-letter queue).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import dramatiq
import structlog

from app.core.exceptions import NotFoundError
from app.core.logging import bind_worker_context
from app.db.session import async_session_factory
from app.modules.jobs import service as jobs_service
from app.observability.metrics import JOBS_STALE_MESSAGES_TOTAL

# The handler runs on the default queue: it is job infrastructure, not a
# workload, so it never competes with the workload queues (blueprint §18
# example queues: default, documents, integrations, ai, emails).
HANDLER_QUEUE = "default"

logger = structlog.get_logger()


def job_id_from_message(message_dict: dict[str, Any]) -> uuid.UUID:
    """Extract the durable job id from the message dict a task was sent with.

    Tasks are enqueued with ``job_id`` as their only keyword argument (see
    ``jobs_service.create_and_enqueue``), and the Retries middleware forwards
    the whole message dict to the exhausted-handler, so the job id travels in
    ``kwargs``. Raising ``KeyError`` here fails the handler message loudly.
    """
    return uuid.UUID(message_dict["kwargs"]["job_id"])


@dataclass(frozen=True)
class AttemptStamp:
    """The ownership stamp an exhausted message carries from its last claim.

    ``dispatch_id`` is the outbox dispatch the message last attempted;
    ``owner_token`` is the attempt-distinguishing credential rotated for that
    claim. Both are needed to correlate the finalizer with the *attempt*: a
    retry re-claim or an expired-lease takeover keeps the dispatch while
    rotating the token, so dispatch alone cannot distinguish attempts.
    """

    dispatch_id: uuid.UUID
    owner_token: uuid.UUID


def attempt_stamp_from_message(message_dict: dict[str, Any]) -> AttemptStamp | None:
    """Extract the stamped dispatch id and owner token from a task message.

    The execution wrapper stamps both at claim time into the message
    ``options`` (see ``app.modules.jobs.execution``), and the Retries
    middleware forwards those options verbatim to the exhausted-handler.
    ``None`` covers messages without the stamp — direct test calls and
    duplicates that exhausted without ever claiming — which the finalizer
    settles under the explicit legacy rules. A partially-stamped message
    cannot be produced by one release of the wrapper; it is treated as
    unstamped so the legacy rules decide rather than risking a newer owner.
    """
    options = message_dict.get("options", {})
    dispatch_id = options.get("dispatch_id")
    owner_token = options.get("owner_token")
    if dispatch_id is None and owner_token is None:
        return None
    if dispatch_id is None or owner_token is None:
        return None
    return AttemptStamp(dispatch_id=uuid.UUID(dispatch_id), owner_token=uuid.UUID(owner_token))


async def mark_job_failed_after_retries(
    message_dict: dict[str, Any], retry_info: dict[str, Any]
) -> None:
    """Record a durable job's terminal failure once its retries are exhausted.

    Invoked by the Retries middleware (via the ``on_retry_exhausted`` option
    in ``jobs_service.retry_policy()``) after the last transient attempt
    failed. The finalizer settles only the attempt the exhausted message
    actually claimed (correlated by the dispatch id and owner token the
    wrapper stamped into the message at claim time): a job that reached a
    terminal state, a job whose current dispatch still carries a live lease
    held by a newer/live attempt, or a job whose current attempt was
    superseded — by a newer dispatch or by a rotated owner token on a retry or
    takeover of the same dispatch — is a stale message and is acknowledged
    without touching the row. Otherwise the durable row transitions to
    ``failed`` with the exhausted error code and the ``job.failed`` audit row
    is written. The actor declaration below binds this function to the name
    ``MARK_FAILED_AFTER_RETRIES_ACTOR`` so the two can never drift apart.
    """
    job_id = job_id_from_message(message_dict)
    stamp = attempt_stamp_from_message(message_dict)
    bind_worker_context(job_id=str(job_id))
    logger.info("job.retries_exhausted.started")
    async with async_session_factory() as session:
        try:
            settled = await jobs_service.settle_after_retries_exhausted(
                session,
                job_id=job_id,
                exhausted_dispatch_id=stamp.dispatch_id if stamp is not None else None,
                exhausted_owner_token=stamp.owner_token if stamp is not None else None,
            )
        except NotFoundError:
            # The original task has already exhausted its bounded retries, so
            # a still-missing row cannot be the normal enqueue-before-commit
            # race. A reset, retention action or malformed external message
            # has made this message stale; acknowledge it without creating a
            # second dead letter, while retaining an operator-visible signal.
            JOBS_STALE_MESSAGES_TOTAL.inc()
            logger.warning("job.retries_exhausted.skipped", reason="job_not_found")
            return
        if settled is None:
            # Terminal or still-leased: a newer/live attempt owns the job, so
            # this exhausted message must not fail it (plan P2 stale-settlement
            # rule). Acknowledge the message and retain the operator signal.
            JOBS_STALE_MESSAGES_TOTAL.inc()
            logger.warning("job.retries_exhausted.skipped", reason="stale_dispatch")
            return
        logger.info("job.retries_exhausted.recorded")


mark_job_failed_after_retries_actor = dramatiq.actor(
    queue_name=HANDLER_QUEUE,
    actor_name=jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR,
    max_retries=0,
    throws=(),
)(mark_job_failed_after_retries)
