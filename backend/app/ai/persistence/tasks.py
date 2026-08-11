"""AI retention/deletion Dramatiq tasks (v0.7 Scope §6.5, BP §18, §28).

``enforce_ai_retention`` is the privacy-safe maintenance sweep for the AI
platform: it deletes expired ``ai_outputs`` records and orphaned analyse-only
scratch objects per each organisation's retention policy, reconciles crashed
reservations, and audits ``ai.retention_deleted`` — never logging content,
prompts or object keys (BP §28). It runs on the ``ai`` queue (blueprint §18
example queues) and is a maintenance actor, not a durable job row: it is
enqueued by the documented operational schedule (runbook, v0.7 Scope §6.7), exactly
like the other infrastructure actors.

The handler function is deliberately separate from its actor declaration so a
test can re-declare it bound to its own broker (the same pattern as
``app.modules.jobs.tasks``).
"""

from __future__ import annotations

import dramatiq
import structlog

from app.db.session import async_session_factory
from app.storage import get_storage

#: The queue maintenance workloads run on (blueprint §18 example queues:
#: default, documents, integrations, ai, emails).
HANDLER_QUEUE = "ai"

logger = structlog.get_logger()


async def enforce_ai_retention() -> None:
    """Run the §6.5 retention sweep across every organisation with a policy."""
    from app.ai.persistence import service as ai_persistence

    logger.info("ai.retention.started")
    async with async_session_factory() as session:
        summary = await ai_persistence.enforce_ai_retention(session, get_storage())
        logger.info("ai.retention.completed", **summary)


enforce_ai_retention_actor = dramatiq.actor(
    queue_name=HANDLER_QUEUE,
    max_retries=2,
    throws=(),
)(enforce_ai_retention)
