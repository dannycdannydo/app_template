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
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.ai.errors import TransferExecutionUnavailableError
from app.ai.managed_url import mint_managed_download_url
from app.ai.persistence.references import TransferReferenceStore
from app.ai.staging import ExternalFileReference, TransferStore
from app.ai.transfer import SourceLifecycle, TransferMode, derive_idempotency_key
from app.storage.base import ObjectStorage
from app.storage.types import SignedUrl

#: The non-inline modes that own a provider-side copy which terminal cleanup
#: deletes through the store. The managed-signed-url mode has no provider copy:
#: the URL is minted per dispatch and expires through its short TTL (Scope §2.3).
_PROVIDER_COPY_MODES = frozenset({TransferMode.PROVIDER_UPLOAD, TransferMode.STORAGE_REFERENCE})


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
        store: TransferStore,
        references: TransferReferenceStore,
    ) -> None:
        self._storage = storage
        self._store = store
        self._references = references

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
        — it produces no durable reference.
        """
        if mode not in _PROVIDER_COPY_MODES | {TransferMode.MANAGED_SIGNED_URL}:
            raise TransferExecutionUnavailableError("inline transfers produce no durable reference")
        if mode in _PROVIDER_COPY_MODES:
            # The store is the adapter for exactly one provider; a wiring error
            # that selects a different provider than the store stages through
            # must fail closed instead of silently staging a copy elsewhere.
            if provider_id != self._store.provider_id:
                raise TransferExecutionUnavailableError(
                    "the selected provider does not match the transfer store"
                )
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
            try:
                return await self._references.create_or_adopt(staged)
            except Exception:
                # Compensation boundary: the provider copy is staged but the
                # durable record failed, so the AI-owned copy must not be left
                # untracked (no row for §6.7 reconciliation to find). Best-
                # effort delete through the provider-neutral store; if that also
                # fails the copy stays bounded by provider expiry and the
                # reconciliation job's coverage of cleanup failures.
                with contextlib.suppress(Exception):
                    await self._store.delete(staged)
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
        return await self._references.create_or_adopt(staged)

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
        return await self._references.find_live(
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            provider_id=provider_id,
            mode=mode,
            source_digest=source_digest,
            region=region,
        )

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
        resolved from the durable record first (and its provider/mode validated
        against the caller's) before any provider deletion. For
        ``provider_upload`` and ``storage_reference`` the provider store removes
        the provider-hosted file / GCS staging object named by the **resolved**
        row, and only then is that row marked ``deleted``; a delayed cleanup can
        never delete an old copy and orphan the live one (Scope §6.3). The
        feature-owned source object is never touched — this method never calls
        ``ObjectStorage.delete_object`` (Scope §2.5). A provider deletion
        failure propagates and leaves the row for the reconciliation job (Scope
        §6.7). The managed-signed-url mode has no provider copy; the row is
        marked deleted directly. Returns whether a durable row was marked
        deleted (``False`` when nothing was left to delete, e.g. the row was
        already terminal).
        """
        current = await self._references.resolve_for_deletion(
            organisation_id=reference.organisation_id,
            idempotency_key=reference.idempotency_key,
        )
        if current is None:
            return False
        if current.provider != reference.provider or current.mode is not reference.mode:
            raise TransferExecutionUnavailableError(
                "the durable reference does not match the caller's transfer"
            )
        if current.mode in _PROVIDER_COPY_MODES:
            await self._store.delete(current)
        return await self._references.mark_deleted(
            organisation_id=current.organisation_id,
            idempotency_key=current.idempotency_key,
        )

    async def delete_references_for_request(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> int:
        """Terminal cleanup of every reference of one logical request.

        Iterates the request's references (org-scoped) and deletes each
        remaining provider copy best-effort through :meth:`delete_reference`,
        which resolves the authoritative current row per key — so this sweep
        removes the copies of expired references too, and calling it after
        :meth:`expire_references_for_request` composes safely instead of
        stranding provider copies. A provider deletion failure stops the sweep
        and leaves the remaining rows for the reconciliation job. Returns the
        number of durable rows marked deleted.
        """
        references = await self._references.list_for_request(
            organisation_id=organisation_id, logical_request_id=logical_request_id
        )
        deleted = 0
        for reference in references:
            if await self.delete_reference(reference=reference):
                deleted += 1
        return deleted

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
        The URL is a temporary bearer capability for one dispatch: it is never
        returned to the caller, persisted, audited or logged — use
        :func:`~app.ai.managed_url.redact_signed_url` at every boundary.
        """
        return await mint_managed_download_url(
            storage=self._storage,
            reference=reference,
            ttl_seconds=ttl_seconds,
        )


__all__ = ["TransferOrchestrator"]
