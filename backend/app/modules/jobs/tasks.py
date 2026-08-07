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
from typing import Any

import dramatiq

from app.db.session import async_session_factory
from app.modules.jobs import service as jobs_service

# The handler runs on the default queue: it is job infrastructure, not a
# workload, so it never competes with the workload queues (blueprint §18
# example queues: default, documents, integrations, ai, emails).
HANDLER_QUEUE = "default"


def job_id_from_message(message_dict: dict[str, Any]) -> uuid.UUID:
    """Extract the durable job id from the message dict a task was sent with.

    Tasks are enqueued with ``job_id`` as their only keyword argument (see
    ``jobs_service.create_and_enqueue``), and the Retries middleware forwards
    the whole message dict to the exhausted-handler, so the job id travels in
    ``kwargs``. Raising ``KeyError`` here fails the handler message loudly.
    """
    return uuid.UUID(message_dict["kwargs"]["job_id"])


async def mark_job_failed_after_retries(
    message_dict: dict[str, Any], retry_info: dict[str, Any]
) -> None:
    """Record a durable job's terminal failure once its retries are exhausted.

    Invoked by the Retries middleware (via the ``on_retry_exhausted`` option
    in ``jobs_service.retry_policy()``) after the last transient attempt
    failed. The durable row transitions ``running`` -> ``failed`` with the
    exhausted error code, and the ``job.failed`` audit row is written. The
    actor declaration below binds this function to the name
    ``MARK_FAILED_AFTER_RETRIES_ACTOR`` so the two can never drift apart.
    """
    job_id = job_id_from_message(message_dict)
    async with async_session_factory() as session:
        await jobs_service.fail(
            session,
            job_id=job_id,
            error_code=jobs_service.ERROR_CODE_RETRIES_EXHAUSTED,
            error_message=jobs_service.ERROR_MESSAGE_RETRIES_EXHAUSTED,
        )


mark_job_failed_after_retries_actor = dramatiq.actor(
    queue_name=HANDLER_QUEUE,
    actor_name=jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR,
    max_retries=0,
    throws=(),
)(mark_job_failed_after_retries)
