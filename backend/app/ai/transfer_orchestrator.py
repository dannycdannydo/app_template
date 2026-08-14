"""Transfer orchestration: create/adopt/reuse/expire/delete (v0.8 Scope §2.3, §2.5, §6.3).

:class:`TransferOrchestrator` is the service that owns one non-inline transfer
of a private source object end to end. It coordinates the three seams the v0.8
architecture draws around the AI layer — never touching a provider SDK itself
(BP §23, ADR-0017):

- the private :class:`~app.storage.base.ObjectStorage` seam, used to verify and
  stream the source (Scope §2.3) and to mint just-in-time managed download
  URLs for retained sources;
- the provider-neutral :class:`~app.ai.staging.TransferStore` seam, which owns
  the provider-specific upload/staging/deletion behavior behind adapters;
- the durable :class:`~app.ai.persistence.references.TransferReferenceStore`
  seam, which persists the organisation-scoped ``ai_attachment_references``
  row and enforces the retry-only reuse idempotency (Scope §2.3).

Transaction boundaries follow BP §11: the orchestrator performs external
staging and provider deletion *around* the reference store's own committed
database transactions — no transaction ever spans provider I/O.

Ownership rule (Scope §2.5, §6.3): every operation here treats the provider
copy and the Vertex staging object as **AI-owned derivatives**. :meth:`delete_reference`
deletes the provider-side copy only (through the store) and never calls
:meth:`ObjectStorage.delete_object` on the feature-owned source; the source
object and its feature lifecycle are untouched by construction.

The deterministic mode selection (Scope §6.2) stays in ``AIService``; this
orchestrator only ever executes an already-selected non-inline mode. ``inline``
produces no durable reference and is refused here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import structlog

from app.ai.errors import AIError, TransferExecutionUnavailableError
from app.ai.managed_url import mint_managed_download_url
from app.ai.persistence.references import (
    ERROR_CODE_DELETION_FAILED,
    TransferReferenceStore,
)
from app.ai.staging import ExternalFileReference, TransferStore
from app.ai.transfer import (
    MANAGED_URL_DEFAULT_TTL_SECONDS,
    SourceLifecycle,
    TransferMode,
    derive_idempotency_key,
)
from app.modules.audit.service import (
    ACTION_AI_TRANSFER_DELETED,
    ACTION_AI_TRANSFER_EXPIRED,
    ACTION_AI_TRANSFER_FAILED,
    ACTION_AI_TRANSFER_REUSED,
    ACTION_AI_TRANSFER_STAGED,
)
from app.observability.metrics import observe_ai_transfer_outcome
from app.storage.base import ObjectStorage
from app.storage.types import SignedUrl

#: The non-inline modes that own a provider-side copy which terminal cleanup
#: deletes through the store. The managed-signed-url mode has no provider copy:
#: the URL is minted per dispatch and expires through its short TTL (Scope §2.3).
_PROVIDER_COPY_MODES = frozenset({TransferMode.PROVIDER_UPLOAD, TransferMode.STORAGE_REFERENCE})

#: Safe audit metadata keys. Never object keys, external ids, gs:// URIs,
#: signed URLs or content (BP §28).
_AUDIT_MODE_KEY = "transfer_mode"
_AUDIT_PROVIDER_KEY = "provider"
_AUDIT_COUNT_KEY = "count"
_AUDIT_ERROR_CODE_KEY = "error_code"


def _safe_error_code(exc: Exception) -> str:
    """Extract the safe AI taxonomy error code from a transfer exception.

    The AI taxonomy's ``error_code`` is the only exception surface allowed
    into audit events and metric labels — never the exception message, a
    provider response, a URL or content (BP §28). A non-taxonomy exception
    (unexpected adapter crash) falls back to a generic safe code so the
    failure outcome stays observable without leaking internals.
    """
    if isinstance(exc, AIError) and exc.error_code:
        return exc.error_code
    return "ai_transfer_error"


#: Module logger. Mode/outcome metadata only — never object keys, gs:// URIs,
#: signed URLs or content (BP §28).
logger = structlog.get_logger()

#: The audit-recording seam the orchestrator emits transfer-lifecycle events
#: through. Wired by the caller with its session (the ``ai.execute`` job and
#: the demonstration flow pass the caller-bound session; the reconciliation
#: job passes its own). ``None`` disables audit recording for hermetic tests.
AuditRecorder = Callable[[str, str, str, UUID, dict[str, Any] | None], Awaitable[None]]


@dataclass(frozen=True)
class RequestFinalizeResult:
    """Outcome of the terminal reference sweep for one logical request."""

    expired: int
    deleted: int


class ManagedUrlStager(Protocol):
    """The dev managed-URL staging seam (v0.8 Scope §2.3, §6.4/§6.5).

    A source storage that cannot produce a provider-reachable HTTPS signed URL
    (local MinIO in development) is served by a stager that re-verifies the
    retained source, stages a copy into the user-provisioned GCS temp bucket
    and mints an HTTPS URL the provider can fetch. ``None`` (production with a
    public HTTPS storage) mints the URL directly from the source storage.
    """

    @property
    def region(self) -> str:
        """The staging region (the configured Vertex location)."""
        ...

    async def mint(
        self,
        *,
        reference: ExternalFileReference,
        ttl_seconds: int,
        source_storage: ObjectStorage,
    ) -> SignedUrl: ...


class TransferOrchestrator:
    """Coordinates source verification, provider staging and durable references.

    Construct one per execution with the caller's session-bound reference store
    (like :class:`~app.ai.persistence.service.AIPersistencePortImpl`) and the
    process-wide storage and provider store seams; the ``ai.execute`` job and
    the demonstration flow share this pattern. Tests may substitute the
    deterministic fake store/storage and an in-memory reference store.
    """

    def __init__(
        self,
        *,
        storage: ObjectStorage,
        store: TransferStore | None = None,
        references: TransferReferenceStore,
        managed_url_stager: ManagedUrlStager | None = None,
        audit_recorder: AuditRecorder | None = None,
    ) -> None:
        self._storage = storage
        self._store = store
        self._references = references
        self._managed_url_stager = managed_url_stager
        self._audit_recorder = audit_recorder

    async def _audit(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        organisation_id: UUID,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record one low-cardinality transfer-lifecycle audit event, if wired.

        Safe payloads only: mode/provider/count/error code, never object keys,
        external ids, gs:// URIs, managed signed URLs or content (BP §28,
        Scope §2.3/§2.5). Failures are suppressed — auditing must never mask
        or block the transfer lifecycle itself.
        """
        if self._audit_recorder is None:
            return
        with contextlib.suppress(Exception):
            await self._audit_recorder(
                action, resource_type, resource_id, organisation_id, metadata
            )

    async def create_or_reuse_reference(
        self,
        *,
        organisation_id: UUID,
        logical_request_id: str,
        provider_id: str,
        mode: TransferMode,
        source_reference: str,
        source_digest: str,
        size_bytes: int,
        mime_type: str,
        source_lifecycle: SourceLifecycle,
        region: str,
        expires_at: datetime | None,
        source_path: Path | None = None,
    ) -> ExternalFileReference:
        """Stage one source object and persist (or adopt) its durable reference.

        The caller has already verified the source (ownership, size, MIME and
        SHA-256 — Scope §2.3/§6.3 streaming seam) and selected the mode
        (Scope §6.2), and ``source_path`` is the verified secure temporary file
        that verified copy was streamed into. For ``provider_upload``/
        ``storage_reference`` the provider store stages the copy from that file
        (idempotent on the derived key: a retry receives the live reference
        instead of a second upload, Scope §2.1); for ``managed_signed_url``
        there is no provider copy and the reference is built directly. The
        durable row is created or adopted
        (:meth:`TransferReferenceStore.create_or_adopt`), so a retry of one
        logical request keeps exactly one live reference. ``inline`` is refused
        — it produces no durable reference. Any contract-declared PDF page
        ceiling has already been enforced by ``AIService`` at the shared
        verified-source boundary before this method is called.
        """
        if mode not in _PROVIDER_COPY_MODES | {TransferMode.MANAGED_SIGNED_URL}:
            raise TransferExecutionUnavailableError("inline transfers produce no durable reference")
        if mode in _PROVIDER_COPY_MODES:
            # The store is the adapter for exactly one provider; a wiring error
            # that selects a different provider than the store stages through
            # must fail closed instead of silently staging a copy elsewhere.
            if self._store is None:
                raise TransferExecutionUnavailableError(
                    "the selected transfer mode requires a provider transfer store"
                )
            if provider_id != self._store.provider_id:
                raise TransferExecutionUnavailableError(
                    "the selected provider does not match the transfer store"
                )
            try:
                staged = await self._store.stage(
                    mode=mode,
                    organisation_id=organisation_id,
                    logical_request_id=logical_request_id,
                    source_reference=source_reference,
                    source_digest=source_digest,
                    mime_type=mime_type,
                    size_bytes=size_bytes,
                    source_lifecycle=source_lifecycle,
                    region=region,
                    expires_at=expires_at,
                    source_path=source_path,
                )
            except Exception as exc:
                # Safe failure outcome observability (Scope §6.7 checkbox 3):
                # a staging/upload failure carries the safe AI taxonomy error
                # code only — never exception text, provider responses, URLs,
                # external ids, keys or content (BP §28).
                await self._audit(
                    ACTION_AI_TRANSFER_FAILED,
                    resource_type="ai_attachment_reference",
                    resource_id=logical_request_id,
                    organisation_id=organisation_id,
                    metadata={
                        _AUDIT_MODE_KEY: mode.value,
                        _AUDIT_PROVIDER_KEY: provider_id,
                        _AUDIT_ERROR_CODE_KEY: _safe_error_code(exc),
                    },
                )
                observe_ai_transfer_outcome(mode=mode.value, provider=provider_id, result="failed")
                raise
        else:
            staged = ExternalFileReference(
                mode=mode,
                provider=provider_id,
                # There is no provider-hosted file: the external id is the
                # source reference itself, the immutable object identity the
                # managed URL is minted against on every dispatch (Scope §2.3).
                external_id=source_reference,
                source_reference=source_reference,
                source_digest=source_digest,
                size_bytes=size_bytes,
                mime_type=mime_type,
                source_lifecycle=source_lifecycle,
                region=region,
                organisation_id=organisation_id,
                logical_request_id=logical_request_id,
                idempotency_key=derive_idempotency_key(
                    provider=provider_id,
                    mode=mode,
                    organisation_id=organisation_id,
                    logical_request_id=logical_request_id,
                    source_digest=source_digest,
                    region=region,
                ),
                created_at=datetime.now(UTC),
            )
        try:
            reference = await self._references.create_or_adopt(staged)
        except Exception:
            # Compensation boundary: the provider copy is staged but the
            # durable record failed, so the AI-owned copy must not be left
            # untracked (no row for §6.7 reconciliation to find). Best-
            # effort delete through the provider-neutral store; if that also
            # fails the copy stays bounded by provider expiry and the
            # reconciliation job's coverage of cleanup failures.
            if mode in _PROVIDER_COPY_MODES and self._store is not None:
                with contextlib.suppress(Exception):
                    await self._store.delete(staged)
            raise
        await self._audit(
            ACTION_AI_TRANSFER_STAGED,
            resource_type="ai_attachment_reference",
            resource_id=logical_request_id,
            organisation_id=organisation_id,
            metadata={
                _AUDIT_MODE_KEY: mode.value,
                _AUDIT_PROVIDER_KEY: provider_id,
            },
        )
        observe_ai_transfer_outcome(mode=mode.value, provider=provider_id, result="staged")
        return reference

    async def find_reusable_reference(
        self,
        *,
        organisation_id: UUID,
        logical_request_id: str,
        provider_id: str,
        mode: TransferMode,
        source_digest: str,
        region: str,
    ) -> ExternalFileReference | None:
        """Return the live matching reference for a retry, or ``None``.

        Retry-only reuse (Scope §2.1/§2.3): only a live reference from the
        same logical request whose provider, mode, digest and region still
        match is returned (the derived idempotency key is the predicate), and
        a hit is adopted (last-used touched). ``None`` means the caller must
        re-stage through :meth:`create_or_reuse_reference` — a changed
        digest/provider/region or an expired reference always yields a new
        idempotent transfer (Scope §5.4).
        """
        reference = await self._references.find_live(
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            provider_id=provider_id,
            mode=mode,
            source_digest=source_digest,
            region=region,
        )
        if reference is not None:
            await self._audit(
                ACTION_AI_TRANSFER_REUSED,
                resource_type="ai_attachment_reference",
                resource_id=logical_request_id,
                organisation_id=organisation_id,
                metadata={
                    _AUDIT_MODE_KEY: mode.value,
                    _AUDIT_PROVIDER_KEY: provider_id,
                },
            )
            observe_ai_transfer_outcome(mode=mode.value, provider=provider_id, result="reused")
        return reference

    async def expire_references_for_request(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> int:
        """Mark every live reference of one logical request ``expired``.

        Runs at terminal execution (success, permanent failure or exhausted
        retries): the references are no longer reusable, the transient
        provider files still expire through their configured provider expiry,
        and the rows stay as the durable record without blocking a
        replacement. Provider copies are not deleted here — terminal deletion
        goes through :meth:`delete_reference` / :meth:`delete_references_for_request`
        and the reconciliation job covers failures (Scope §2.5, §6.7).
        Returns the number of rows marked.
        """
        return await self._references.expire_all_for_request(
            organisation_id=organisation_id, logical_request_id=logical_request_id
        )

    async def delete_reference(self, *, reference: ExternalFileReference) -> bool:
        """Best-effort terminal deletion of the current provider copy, then the row.

        The caller's ``reference`` may be stale — it was captured before the
        provider copy was recreated — so the authoritative current row is
        resolved *and claimed atomically* from the durable record first
        (:meth:`TransferReferenceStore.claim_for_deletion`: one transaction
        resolves the row and stamps the deletion attempt with ``FOR UPDATE``,
        serializing concurrent claims of the same key; duplicate provider
        DELETEs from a later caller are absorbed by 404/410 idempotency), and
        its provider/mode validated against the caller's. For a
        ``provider_upload`` or ``storage_reference`` reference the owning
        store removes the provider-hosted file named by the **resolved** row,
        and only then is that row marked ``deleted``; a delayed cleanup can
        never delete an old copy and orphan the live one (Scope §6.3). The
        feature-owned source object is never touched — this method never calls
        ``ObjectStorage.delete_object`` (Scope §2.5). A provider deletion
        failure is stamped on the row (safe ``provider_reference_deletion_failed``
        code plus the attempt timestamp) and propagates so the caller's
        terminal handling and the §6.7 reconciliation sweep can re-claim it
        after the bounded backoff window. The managed-signed-url mode has no
        provider copy: its row is marked deleted directly (the URL is minted
        per dispatch and expires through its short TTL, Scope §2.3). Vertex
        GCS staging objects are deleted immediately through the store at
        terminal cleanup (Scope §2.5 permits immediate best-effort deletion;
        only the *scheduled* reconciliation job excludes them — the deployer's
        ``age = 1`` lifecycle is the backstop). Returns whether a durable row
        was marked deleted (``False`` when nothing was left to delete, e.g.
        the row was already terminal).
        """
        current = await self._references.claim_for_deletion(
            organisation_id=reference.organisation_id,
            idempotency_key=reference.idempotency_key,
        )
        if current is None:
            return False
        if current.provider != reference.provider or current.mode is not reference.mode:
            raise TransferExecutionUnavailableError(
                "the durable reference does not match the caller's transfer"
            )
        if current.mode is TransferMode.MANAGED_SIGNED_URL:
            # No provider-hosted copy (managed signed URL): the row is marked
            # deleted directly, exactly as before.
            deleted = await self._references.mark_deleted(
                organisation_id=current.organisation_id,
                idempotency_key=current.idempotency_key,
            )
            if deleted:
                await self._audit(
                    ACTION_AI_TRANSFER_DELETED,
                    resource_type="ai_attachment_reference",
                    resource_id=current.logical_request_id,
                    organisation_id=current.organisation_id,
                    metadata={
                        _AUDIT_MODE_KEY: current.mode.value,
                        _AUDIT_PROVIDER_KEY: current.provider,
                    },
                )
                observe_ai_transfer_outcome(
                    mode=current.mode.value,
                    provider=current.provider,
                    result="deleted",
                )
            return deleted
        if self._store is None or self._store.provider_id != current.provider:
            # Unreachable in normal wiring (creation requires the store of the
            # owning provider); fail closed rather than deleting nothing
            # silently. The claim stamp stays on the row so it backs off and
            # the sweep re-claims it once the provider is deployed.
            raise TransferExecutionUnavailableError(
                "the selected transfer mode requires a provider transfer store"
            )
        try:
            await self._store.delete(current)
        except Exception as exc:
            # The provider copy survives; record the safe failure so the
            # reconciliation sweep (not this path) retries after backoff, then
            # re-raise for the caller's terminal handling. Never the exception
            # internals — the durable error code is the safe surface (BP §28).
            await self._references.mark_deletion_attempted(
                organisation_id=current.organisation_id,
                idempotency_key=current.idempotency_key,
                error_code=ERROR_CODE_DELETION_FAILED,
            )
            # Safe failure outcome observability (Scope §6.7 checkbox 3): the
            # audit payload carries mode/provider and the safe error code only
            # — never exception text, provider responses, URLs, external ids,
            # keys or content (BP §28).
            await self._audit(
                ACTION_AI_TRANSFER_FAILED,
                resource_type="ai_attachment_reference",
                resource_id=current.logical_request_id,
                organisation_id=current.organisation_id,
                metadata={
                    _AUDIT_MODE_KEY: current.mode.value,
                    _AUDIT_PROVIDER_KEY: current.provider,
                    _AUDIT_ERROR_CODE_KEY: _safe_error_code(exc),
                },
            )
            observe_ai_transfer_outcome(
                mode=current.mode.value,
                provider=current.provider,
                result="failed",
            )
            raise TransferExecutionUnavailableError(
                "the provider-side copy could not be deleted"
            ) from exc
        deleted = await self._references.mark_deleted(
            organisation_id=current.organisation_id,
            idempotency_key=current.idempotency_key,
        )
        if deleted:
            await self._audit(
                ACTION_AI_TRANSFER_DELETED,
                resource_type="ai_attachment_reference",
                resource_id=current.logical_request_id,
                organisation_id=current.organisation_id,
                metadata={
                    _AUDIT_MODE_KEY: current.mode.value,
                    _AUDIT_PROVIDER_KEY: current.provider,
                },
            )
            observe_ai_transfer_outcome(
                mode=current.mode.value,
                provider=current.provider,
                result="deleted",
            )
        return deleted

    async def delete_references_for_request(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> int:
        """Terminal cleanup of every reference of one logical request.

        Iterates the request's references (org-scoped) and deletes each
        remaining provider copy best-effort through :meth:`delete_reference`,
        which resolves and atomically claims the authoritative current row per
        key — so this sweep removes the copies of expired references too, and
        calling it after :meth:`expire_references_for_request` composes safely
        instead of stranding provider copies. A provider deletion failure is
        collected and suppressed per reference (the row is already stamped for
        the §6.7 reconciliation sweep's bounded backoff, and for GCS staging
        objects the deployer-owned ``age = 1`` lifecycle is the backstop) so a
        failing copy never blocks the immediate best-effort attempt of the
        remaining copies (Scope §6.7 checkbox 1). Returns the number of
        durable rows marked deleted.
        """
        references = await self._references.list_for_request(
            organisation_id=organisation_id, logical_request_id=logical_request_id
        )
        deleted = 0
        for reference in references:
            try:
                if await self.delete_reference(reference=reference):
                    deleted += 1
            except Exception:
                # The row was claimed (stamped) before the provider call, so
                # the sweep re-claims it after the backoff window; continue so
                # every later copy still receives an immediate attempt. Never
                # logged with ids or URLs (BP §28).
                continue
        return deleted

    async def finalize_request_references(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> RequestFinalizeResult:
        """Terminal reference sweep for one logical request (v0.8 Scope §2.5/§6.7).

        Runs once at terminal execution — success, permanent failure or
        exhausted retries — and composes the two terminal transitions safely:

        1. every live reference is marked ``expired`` (no longer reusable,
           rows never block a replacement), and
        2. every remaining provider copy is deleted best-effort through
           :meth:`delete_reference`, which resolves each authoritative row,
           stamps the deletion attempt and records the safe failure code when
           the provider delete fails — leaving the row for the §6.7
           reconciliation sweep's bounded backoff instead of silently
           stranding it.

        The feature-owned source object is never touched by construction.
        Returns the counts for the audit/observability surface; the audit
        events themselves are emitted by :meth:`expire_references_for_request`
        and :meth:`delete_reference`.
        """
        expired = await self.expire_references_for_request(
            organisation_id=organisation_id, logical_request_id=logical_request_id
        )
        if expired:
            await self._audit(
                ACTION_AI_TRANSFER_EXPIRED,
                resource_type="ai_attachment_reference",
                resource_id=logical_request_id,
                organisation_id=organisation_id,
                metadata={_AUDIT_COUNT_KEY: expired},
            )
        deleted = await self.delete_references_for_request(
            organisation_id=organisation_id, logical_request_id=logical_request_id
        )
        return RequestFinalizeResult(expired=expired, deleted=deleted)

    async def mint_managed_url(
        self,
        *,
        reference: ExternalFileReference,
        ttl_seconds: int | None = None,
    ) -> SignedUrl:
        """Mint one just-in-time managed download URL for a retained source.

        Verifies the mode/lifecycle contract, the organisation ownership
        boundary and the exact immutable object identity (a fresh head for size
        and MIME, plus a bounded re-stream re-verifying the SHA-256 digest)
        before minting a short-lived, read-only HTTPS URL (Scope §2.3, §6.3).
        With a dev managed-URL stager wired (local storage seam), the verified
        source is staged into the GCS temp bucket and the URL is minted from
        the staged copy instead — the provider must be able to fetch it. The
        URL is a temporary bearer capability for one dispatch: it is never
        returned to the caller, persisted, audited or logged — use
        :func:`~app.ai.managed_url.redact_signed_url` at every boundary.
        """
        if self._managed_url_stager is not None:
            logger.info(
                "ai.managed_url.minting",
                mode="gcs_staging",
                region=self._managed_url_stager.region,
                ttl_seconds=ttl_seconds or MANAGED_URL_DEFAULT_TTL_SECONDS,
            )
            return await self._managed_url_stager.mint(
                reference=reference,
                ttl_seconds=ttl_seconds or MANAGED_URL_DEFAULT_TTL_SECONDS,
                source_storage=self._storage,
            )
        logger.info(
            "ai.managed_url.minting",
            mode="direct",
            ttl_seconds=ttl_seconds or MANAGED_URL_DEFAULT_TTL_SECONDS,
        )
        return await mint_managed_download_url(
            storage=self._storage,
            reference=reference,
            ttl_seconds=ttl_seconds,
        )


__all__ = ["RequestFinalizeResult", "TransferOrchestrator"]
