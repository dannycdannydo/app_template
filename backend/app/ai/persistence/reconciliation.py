"""Provider-file reference reconciliation sweep (v0.8 Scope §2.5, §6.7, BP §18, §28).

The scheduled reconciliation job covers exactly the provider-hosted copies
(``provider_upload`` mode) that terminal cleanup could not remove — the
cleanup outage, worker crash and deletion-failure cases of Scope §5.5. It is
bounded, idempotent and crash-recoverable:

- **Bounded:** one run claims at most ``batch_size`` references (the typed
  deployment setting), ordered oldest-claim-first, so a large backlog is
  drained across runs without one sweep monopolising the ``ai`` queue.
- **Atomic idempotent claims:** the candidate batch is selected and every row
  stamped (``deletion_attempted_at``) in one ``FOR UPDATE SKIP LOCKED``
  transaction *before* any provider delete, so two workers can never select
  the same row and a crashed worker cannot double-delete; a successful deletion
  marks the row ``deleted`` and it drops out of every candidate query.
- **Bounded backoff:** every claimed row — including rows whose provider has
  no deployed store (fail-closed) — carries a fresh attempt stamp, and a
  failed deletion additionally leaves the safe error code
  ``provider_reference_deletion_failed``; a row is re-claimed only after the
  configured retry window, so a failing provider is never hammered every
  sweep run.
- **Never these:** managed signed URLs (no provider copy), Vertex GCS staging
  objects (deployer-owned ``age = 1`` lifecycle backstop — the application
  runs no GCS cleanup) and feature-owned source objects (the orchestrator
  never calls ``ObjectStorage.delete_object``). The candidate query cannot
  return them by construction (Scope §2.5).

Outcomes are exposed through the low-cardinality
``ai_transfer_reconciliation_total`` counter and the ``ai_transfer_cleanup_backlog``
gauge, plus one ``ai.transfer_reconciled`` audit event per organisation with
counts only — never object keys, external ids, gs:// URIs, managed signed
URLs or content (BP §28, Scope §2.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.persistence.queries import ai_attachment_reference_reconciliation_backlog_statement
from app.ai.persistence.references import SQLTransferReferenceStore, TransferReferenceStore
from app.ai.staging import TransferStore
from app.ai.transfer_orchestrator import TransferOrchestrator
from app.modules.audit.service import (
    ACTION_AI_TRANSFER_RECONCILED,
    record_event,
)
from app.observability.metrics import (
    observe_ai_transfer_reconciliation,
    set_ai_transfer_cleanup_backlog,
)
from app.storage.base import ObjectStorage

logger = structlog.get_logger()

#: Resource type for every transfer-lifecycle audit event.
_RESOURCE_TYPE = "ai_attachment_reference"


def _session_audit_recorder(session: AsyncSession):
    """Build the audit recorder the orchestrator emits lifecycle events through.

    Wired to the sweep's own session so the ``ai.transfer_deleted`` events of
    a reconciled deletion land in the same persistence boundary as the row
    updates. Payloads are low-cardinality (mode/provider/count) only (BP §28).
    """

    async def _record(
        action: str,
        resource_type: str,
        resource_id: str,
        organisation_id: UUID,
        metadata: dict[str, object] | None = None,
    ) -> None:
        await record_event(
            session,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            organisation_id=organisation_id,
            metadata=metadata,
        )

    return _record


async def reconcile_provider_file_references(
    session: AsyncSession,
    *,
    storage: ObjectStorage,
    stores: Mapping[str, TransferStore],
    references: TransferReferenceStore | None = None,
    batch_size: int,
    retry_after_seconds: int,
) -> dict[str, int]:
    """Claim and delete the bounded next batch of orphaned provider files.

    Runs inside the caller's session (the maintenance actor owns its own
    session factory). ``references`` defaults to a store bound to that
    session; the orchestrators share it, so every stamp/delete/audit lands in
    the same organisation-scoped persistence boundary while no transaction
    ever spans provider I/O (BP §11). The batch is claimed atomically
    (:meth:`TransferReferenceStore.claim_needing_reconciliation` — candidate
    selection and the deletion-attempt stamp are one ``FOR UPDATE SKIP LOCKED``
    transaction, so two workers can never claim the same row and a crash
    mid-sweep leaves every row stamped for the bounded backoff). Each claimed
    candidate is deleted through the store of its owning provider — a freshly
    constructed store, so the provider DELETE is driven purely from the
    durable row (Scope §2.5/§6.7); a row whose provider has no deployed store
    fails closed (counted ``failed``) while staying stamped for the backoff,
    so the sweep never deletes through the wrong adapter and never hammers a
    missing provider every run. Returns a safe summary for the job log.
    """
    if references is None:
        references = SQLTransferReferenceStore(session)
    now = datetime.now(UTC)
    retry_after = now - timedelta(seconds=retry_after_seconds)
    candidates = await references.claim_needing_reconciliation(
        retry_after=retry_after, batch_size=batch_size
    )
    deleted = 0
    failed = 0
    deleted_by_org: dict[UUID, int] = {}
    for reference in candidates:
        store = stores.get(reference.provider)
        if store is None:
            # No deployed store owns this provider's copies: fail closed and
            # leave the row stamped for the next sweep (the deployment may
            # enable the provider later). Never logged with ids or URLs (BP §28).
            failed += 1
            observe_ai_transfer_reconciliation(provider=reference.provider, result="failed")
            continue
        orchestrator = TransferOrchestrator(
            storage=storage,
            store=store,
            references=references,
            audit_recorder=_session_audit_recorder(session),
        )
        try:
            if await orchestrator.delete_reference(reference=reference):
                deleted += 1
                observe_ai_transfer_reconciliation(provider=reference.provider, result="deleted")
                deleted_by_org[reference.organisation_id] = (
                    deleted_by_org.get(reference.organisation_id, 0) + 1
                )
        except Exception:
            # The row stays stamped for the bounded backoff window; the next
            # sweep re-claims it. Never logged with ids or URLs (BP §28).
            failed += 1
            observe_ai_transfer_reconciliation(provider=reference.provider, result="failed")
    for organisation_id, count in deleted_by_org.items():
        await record_event(
            session,
            organisation_id=organisation_id,
            action=ACTION_AI_TRANSFER_RECONCILED,
            resource_type=_RESOURCE_TYPE,
            resource_id=str(organisation_id),
            # Counts only — never request ids, object keys, external ids,
            # URLs or content (BP §28; the audit contract in
            # ``app/modules/audit/service.py``).
            metadata={"deleted": count},
        )
    await session.commit()
    set_ai_transfer_cleanup_backlog(count=await _backlog_count(session, retry_after=retry_after))
    logger.info(
        "ai.transfer_reconcile.completed",
        candidates=len(candidates),
        deleted=deleted,
        failed=failed,
    )
    return {"candidates": len(candidates), "deleted": deleted, "failed": failed}


async def _backlog_count(session: AsyncSession, *, retry_after: datetime) -> int:
    """Return the currently eligible provider-file backlog for the gauge."""
    return (
        await session.scalar(
            ai_attachment_reference_reconciliation_backlog_statement(retry_after=retry_after)
        )
        or 0
    )
