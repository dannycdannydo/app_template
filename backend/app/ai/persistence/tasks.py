"""AI retention/deletion Dramatiq tasks (v0.7 Scope §6.5, v0.8 Scope §6.7, BP §18, §28).

``enforce_ai_retention`` is the privacy-safe maintenance sweep for the AI
platform: it deletes expired ``ai_outputs`` records and orphaned analyse-only
scratch objects per each organisation's retention policy, reconciles crashed
reservations, and audits ``ai.retention_deleted`` — never logging content,
prompts or object keys (BP §28). It runs on the ``ai`` queue (blueprint §18
example queues) and is a maintenance actor, not a durable job row: it is
enqueued by the documented operational schedule (runbook, v0.7 Scope §6.7), exactly
like the other infrastructure actors.

``reconcile_provider_file_references`` is the v0.8 Scope §2.5/§6.7
provider-file reconciliation sweep: it claims a bounded batch of provider-hosted
copies whose owning AI request is terminal but whose terminal cleanup failed
or never ran, deletes them through the provider store, and exposes
``ai_transfer_reconciliation_total`` / ``ai_transfer_cleanup_backlog`` plus
``ai.transfer_reconciled`` audit events. It is the only scheduled job that
touches provider-hosted files: managed signed URLs (no provider copy), Vertex
GCS staging objects (deployer-owned lifecycle backstop) and feature sources
are never candidates by construction (Scope §2.5).

The handler functions are deliberately separate from their actor declarations
so a test can re-declare them bound to its own broker (the same pattern as
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


async def reconcile_provider_file_references() -> None:
    """Run the §6.7 provider-file reconciliation sweep (bounded batch)."""
    from app.ai.persistence import reconciliation as ai_reconciliation
    from app.ai.persistence.references import SQLTransferReferenceStore
    from app.ai.runtime import get_transfer_stores
    from app.core.config import get_settings

    settings = get_settings()
    logger.info(
        "ai.transfer_reconcile.started",
        batch_size=settings.ai_reconcile_batch_size,
    )
    async with async_session_factory() as session:
        summary = await ai_reconciliation.reconcile_provider_file_references(
            session,
            storage=get_storage(),
            stores=get_transfer_stores(),
            references=SQLTransferReferenceStore(session),
            batch_size=settings.ai_reconcile_batch_size,
            retry_after_seconds=settings.ai_reconcile_retry_after_seconds,
        )
        logger.info("ai.transfer_reconcile.completed", **summary)


enforce_ai_retention_actor = dramatiq.actor(
    queue_name=HANDLER_QUEUE,
    max_retries=2,
    throws=(),
)(enforce_ai_retention)

reconcile_provider_file_references_actor = dramatiq.actor(
    queue_name=HANDLER_QUEUE,
    max_retries=3,
    throws=(),
)(reconcile_provider_file_references)
