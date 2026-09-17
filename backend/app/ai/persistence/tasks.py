"""AI retention/deletion Dramatiq tasks (v0.7 Scope §6.5, v0.8 Scope §6.7, BP §18, §28).

``enforce_ai_retention`` is the privacy-safe maintenance sweep for the AI
platform: it deletes expired ``ai_outputs`` records and orphaned analyse-only
scratch objects per each organisation's retention policy, reconciles crashed
reservations, and audits ``ai.retention_deleted`` — never logging content,
prompts or object keys (BP §28). It runs on the ``ai`` queue (blueprint §18
example queues).

``reconcile_provider_file_references`` is the v0.8 Scope §2.5/§6.7
provider-file reconciliation sweep: it claims a bounded batch of provider-hosted
copies whose owning AI request is terminal but whose terminal cleanup failed
or never ran, deletes them through the provider store, and exposes
``ai_transfer_reconciliation_total`` / ``ai_transfer_cleanup_backlog`` plus
``ai.transfer_reconciled`` audit events. It is the only scheduled job that
touches provider-hosted files: managed signed URLs (no provider copy), Vertex
GCS staging objects (deployer-owned lifecycle backstop) and feature sources
are never candidates by construction (Scope §2.5).

Both are durable maintenance runs (plan P4), not fire-and-forget actors: the
coordinator writes a ``maintenance_runs`` row and its reference-only outbox
dispatch in one transaction, and the message carries nothing but that run id.
``run_maintenance`` claims the run, holds the advisory lock as defence in
depth, and settles success, delayed retry or exhaustion in PostgreSQL — so a
``published`` outbox row is never mistaken for a completed sweep. The sweeps
themselves are unchanged: same bounded batches, provider adapters, per-item
audit records and privacy constraints.

``maintenance_run_id`` is optional only for rolling-deployment compatibility:
a version-1 message still in flight when the new workers start runs the
legacy advisory-lock-only path instead of crashing. Nothing produces those
messages any more.

The handler functions are deliberately separate from their actor declarations
so a test can re-declare them bound to its own broker (the same pattern as
``app.modules.jobs.tasks``).
"""

from __future__ import annotations

import dramatiq
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.maintenance.execution import run_maintenance
from app.modules.maintenance.models import MaintenanceTaskType
from app.storage import get_storage

#: The queue maintenance workloads run on (blueprint §18 example queues:
#: default, documents, integrations, ai, emails).
HANDLER_QUEUE = "ai"


async def _sweep_ai_retention(session: AsyncSession) -> dict[str, int]:
    """Run the §6.5 retention sweep across every organisation with a policy."""
    from app.ai.persistence import service as ai_persistence

    return await ai_persistence.enforce_ai_retention(session, get_storage())


async def _sweep_transfer_reconcile(session: AsyncSession) -> dict[str, int]:
    """Run the §6.7 provider-file reconciliation sweep (bounded batch)."""
    from app.ai.persistence import reconciliation as ai_reconciliation
    from app.ai.persistence.references import SQLTransferReferenceStore
    from app.ai.runtime import get_transfer_stores
    from app.core.config import get_settings

    settings = get_settings()
    return await ai_reconciliation.reconcile_provider_file_references(
        session,
        storage=get_storage(),
        stores=get_transfer_stores(),
        references=SQLTransferReferenceStore(session),
        batch_size=settings.ai_reconcile_batch_size,
        retry_after_seconds=settings.ai_reconcile_retry_after_seconds,
    )


async def enforce_ai_retention(maintenance_run_id: str | None = None) -> None:
    """Execute one durable ``ai.retention`` maintenance run."""
    await run_maintenance(
        task_type=MaintenanceTaskType.AI_RETENTION,
        maintenance_run_id=maintenance_run_id,
        handler=_sweep_ai_retention,
    )


async def reconcile_provider_file_references(maintenance_run_id: str | None = None) -> None:
    """Execute one durable ``ai.transfer_reconcile`` maintenance run."""
    await run_maintenance(
        task_type=MaintenanceTaskType.TRANSFER_RECONCILE,
        maintenance_run_id=maintenance_run_id,
        handler=_sweep_transfer_reconcile,
    )


# Durable failures are settled and swallowed by ``run_maintenance``, so these
# broker retries apply only to rolling-deployment legacy argument-free messages
# (or malformed input before a run can be claimed). They preserve the previous
# bounded legacy behaviour without competing with PostgreSQL-owned retries.
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
