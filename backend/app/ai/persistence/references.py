"""Durable transfer-reference persistence (v0.8 Scope §2.3, §6.3, BP §9-§11, §28).

This module is the database-backed half of the durable reference lifecycle:
the :class:`SQLTransferReferenceStore` persists one
:class:`~app.ai.staging.ExternalFileReference` per non-inline transfer in the
organisation-scoped ``ai_attachment_references`` table and implements the
create/adopt/reuse/expire/delete operations the
:class:`~app.ai.transfer_orchestrator.TransferOrchestrator` drives.

The seam mirrors the ``AIPersistencePort`` pattern: the orchestrator depends
on the small :class:`TransferReferenceStore` protocol and is constructed with a
session-bound implementation per caller (the ``ai.execute`` job and the
demonstration flow both follow this pattern), while tests may substitute a
deterministic in-memory fake.

Contract rules enforced here:

- **Idempotent create-or-adopt** — the partial unique index allows at most one
  live row per derived idempotency key per organisation. A retry that already
  staged a live matching reference adopts it (refreshing the external id and
  provider expiry when the provider-side copy was recreated) instead of
  inserting a second row; a lost race against a concurrent duplicate falls
  back to the winner's row instead of surfacing a constraint error. A
  time-expired live row is marked ``expired`` first so the replacement insert
  is never blocked (Scope §2.3 expired-reference replacement).
- **Retry-only reuse** — :meth:`find_live` keys on the derived idempotency key
  (provider, mode, organisation, logical request, digest and region) and is
  strictly org-scoped: a cross-organisation lookup is indistinguishable from a
  missing row, and a changed digest/provider/region produces a different key
  and therefore a new idempotent transfer (Scope §5.4).
- **Safe records** — rows carry opaque external references, digests and safe
  error codes only; a managed signed URL or its query string can never be
  stored (there is no column for it, Scope §2.3/§2.5, BP §28).

No transaction here ever spans provider I/O (BP §11): each operation commits
its own database transaction, and the orchestrator performs external staging
and deletion *around* these calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.errors import TransferExecutionUnavailableError
from app.ai.persistence.models import AIAttachmentReference, ExternalReferenceStatus
from app.ai.persistence.queries import (
    ai_attachment_reference_for_deletion_statement,
    ai_attachment_references_for_request_statement,
    ai_live_attachment_reference_by_key_statement,
)
from app.ai.staging import ExternalFileReference
from app.ai.transfer import SourceLifecycle, TransferMode, derive_idempotency_key


def _row_to_reference(row: AIAttachmentReference) -> ExternalFileReference:
    """Map a durable row back to the provider-neutral reference contract.

    The row is the source of truth for the durable fields; the error code and
    bounded metadata are operational row columns that never leave the
    database.
    """
    return ExternalFileReference(
        mode=TransferMode(row.transfer_mode),
        provider=row.provider,
        external_id=row.external_id,
        source_reference=row.source_reference,
        source_digest=row.source_digest,
        size_bytes=row.size_bytes,
        mime_type=row.mime_type,
        source_lifecycle=SourceLifecycle(row.source_lifecycle),
        region=row.region,
        organisation_id=row.organisation_id,
        logical_request_id=row.logical_request_id,
        idempotency_key=row.idempotency_key,
        status=ExternalReferenceStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        deleted_at=row.deleted_at,
    )


@runtime_checkable
class TransferReferenceStore(Protocol):
    """The durable-reference seam the orchestrator drives (Scope §6.3)."""

    async def create_or_adopt(self, reference: ExternalFileReference) -> ExternalFileReference: ...

    async def find_live(
        self,
        *,
        organisation_id: UUID,
        logical_request_id: str,
        provider_id: str,
        mode: TransferMode,
        source_digest: str,
        region: str,
    ) -> ExternalFileReference | None: ...

    async def adopt(self, *, organisation_id: UUID, idempotency_key: str) -> bool: ...

    async def mark_expired(self, *, organisation_id: UUID, idempotency_key: str) -> bool: ...

    async def mark_deleted(self, *, organisation_id: UUID, idempotency_key: str) -> bool: ...

    async def resolve_for_deletion(
        self, *, organisation_id: UUID, idempotency_key: str
    ) -> ExternalFileReference | None: ...

    async def list_for_request(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> list[ExternalFileReference]: ...

    async def expire_all_for_request(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> int: ...


class SQLTransferReferenceStore:
    """Session-bound :class:`TransferReferenceStore` over the reference table.

    Construct one per execution with the caller's session, exactly like
    :class:`~app.ai.persistence.service.AIPersistencePortImpl`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_or_adopt(self, reference: ExternalFileReference) -> ExternalFileReference:
        """Insert the reference row, or adopt the live winner of a duplicate.

        Fast path: a live row already exists for the derived key (a retry of
        the same logical transfer) — its external id and provider expiry are
        refreshed when the provider-side copy was recreated, its last-used
        marker is touched and the existing row is returned. Otherwise the row
        is inserted; a lost race against a concurrent duplicate rolls back and
        adopts the winner. A live row whose provider expiry has passed is
        marked ``expired`` first so the replacement insert is never blocked by
        the partial unique index (Scope §2.3 expired-reference replacement).
        """
        session = self._session
        key = reference.idempotency_key
        for _ in range(2):
            existing = await session.scalar(
                ai_live_attachment_reference_by_key_statement(reference.organisation_id, key)
            )
            if existing is not None:
                if existing.expires_at is not None and existing.expires_at <= datetime.now(UTC):
                    await self.mark_expired(
                        organisation_id=reference.organisation_id, idempotency_key=key
                    )
                else:
                    self._refresh_existing(existing, reference)
                    await session.commit()
                    return _row_to_reference(existing)
            row = AIAttachmentReference(
                organisation_id=reference.organisation_id,
                logical_request_id=reference.logical_request_id,
                provider=reference.provider,
                transfer_mode=reference.mode.value,
                external_id=reference.external_id,
                source_reference=reference.source_reference,
                source_digest=reference.source_digest,
                size_bytes=reference.size_bytes,
                mime_type=reference.mime_type,
                source_lifecycle=reference.source_lifecycle.value,
                region=reference.region,
                status=ExternalReferenceStatus.LIVE.value,
                idempotency_key=key,
                expires_at=reference.expires_at,
                last_used_at=reference.last_used_at,
            )
            session.add(row)
            try:
                await session.commit()
                return _row_to_reference(row)
            except IntegrityError:
                await session.rollback()
                # A concurrent duplicate won the insert; loop once to adopt it.
        raise TransferExecutionUnavailableError(
            "the transfer reference could not be persisted; retry"
        )

    async def find_live(
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

        Keys on the derived idempotency key (provider, mode, organisation,
        logical request, digest and region), so a retry reuses only a live
        matching record from the same logical request (Scope §2.1 retry-only
        reuse). A live row whose provider expiry has passed is marked
        ``expired`` and reported as missing so the caller creates a new
        idempotent transfer. On a hit the row is adopted (last-used touched).
        """
        key = derive_idempotency_key(
            provider=provider_id,
            mode=mode,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            source_digest=source_digest,
            region=region,
        )
        row = await self._session.scalar(
            ai_live_attachment_reference_by_key_statement(organisation_id, key)
        )
        if row is None:
            return None
        if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
            await self.mark_expired(organisation_id=organisation_id, idempotency_key=key)
            return None
        # Adopt the live row in place (touch last-used) so the returned
        # reference reflects the reuse and the adopted marker lands on the live
        # row, never on a historical row sharing the idempotency key.
        row.last_used_at = datetime.now(UTC)
        await self._session.commit()
        return _row_to_reference(row)

    async def adopt(self, *, organisation_id: UUID, idempotency_key: str) -> bool:
        """Touch the last-used marker of the one live reference; whether found.

        Uses the live-only query: the partial unique index means historical
        expired/deleted rows can share the idempotency key with a live
        replacement, and adoption must always land on the live row.
        """
        row = await self._session.scalar(
            ai_live_attachment_reference_by_key_statement(organisation_id, idempotency_key)
        )
        if row is None:
            return False
        row.last_used_at = datetime.now(UTC)
        await self._session.commit()
        return True

    async def mark_expired(self, *, organisation_id: UUID, idempotency_key: str) -> bool:
        """Mark one reference terminal-expired; returns whether it was found."""
        row = await self._session.scalar(
            ai_live_attachment_reference_by_key_statement(organisation_id, idempotency_key)
        )
        if row is None:
            return False
        row.status = ExternalReferenceStatus.EXPIRED.value
        await self._session.commit()
        return True

    async def mark_deleted(self, *, organisation_id: UUID, idempotency_key: str) -> bool:
        """Mark the authoritative remaining reference terminal-deleted.

        Resolves the live row first, else the newest non-deleted (expired) row,
        so terminal cleanup after :meth:`expire_all_for_request` still records
        the deletion on the row that owned the deleted provider copy. Called
        only *after* the best-effort provider-side deletion succeeded or when
        there is no provider copy to delete; the reference row is the durable
        proof that the provider copy was cleaned up (Scope §2.5). Returns
        whether a row was marked (``False`` when every row for the key is
        already terminal).
        """
        row = await self._session.scalar(
            ai_attachment_reference_for_deletion_statement(organisation_id, idempotency_key)
        )
        if row is None:
            return False
        row.status = ExternalReferenceStatus.DELETED.value
        row.deleted_at = datetime.now(UTC)
        await self._session.commit()
        return True

    async def resolve_for_deletion(
        self, *, organisation_id: UUID, idempotency_key: str
    ) -> ExternalFileReference | None:
        """Resolve the authoritative row owning the current provider copy.

        Terminal deletion must act on the row that owns the current provider
        copy, not on a caller-supplied possibly stale reference: the live row is
        preferred (it names the copy the last create/adopt left in place), and
        when no row is live the newest remaining (expired) row is used so a
        sweep after :meth:`expire_all_for_request` still deletes the copies.
        Returns ``None`` when every row for the key is already terminal.
        """
        row = await self._session.scalar(
            ai_attachment_reference_for_deletion_statement(organisation_id, idempotency_key)
        )
        if row is None:
            return None
        return _row_to_reference(row)

    async def expire_all_for_request(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> int:
        """Mark every live reference of one logical request expired.

        Runs when the logical request terminates (success, permanent failure
        or exhausted retries): its references are no longer reusable, the
        transient provider files still expire through their configured
        provider expiry, and the rows no longer block a replacement (Scope
        §2.3). Returns the number of rows marked. Provider copies are not
        deleted here — terminal deletion goes through the orchestrator's
        best-effort ``delete`` path, and the reconciliation job covers
        failures (Scope §2.5, §6.7).
        """
        rows = (
            await self._session.scalars(
                ai_attachment_references_for_request_statement(
                    organisation_id, logical_request_id
                )
            )
        ).all()
        changed = 0
        for row in rows:
            if row.status == ExternalReferenceStatus.LIVE.value:
                row.status = ExternalReferenceStatus.EXPIRED.value
                changed += 1
        if changed:
            await self._session.commit()
        return changed

    async def list_for_request(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> list[ExternalFileReference]:
        """Return every reference row of one logical request, in creation order.

        Strictly org-scoped (BP §9): one request id can never reach another
        organisation's rows. Used by the terminal cleanup path and by the
        reconciliation surfaces (Scope §6.7).
        """
        rows = (
            await self._session.scalars(
                ai_attachment_references_for_request_statement(
                    organisation_id, logical_request_id
                )
            )
        ).all()
        return [_row_to_reference(row) for row in rows]

    @staticmethod
    def _refresh_existing(row: AIAttachmentReference, reference: ExternalFileReference) -> None:
        """Refresh a recreated provider-side copy on the existing live row.

        When the store recreated the reference (the previous provider copy
        expired provider-side), the durable row is updated in place with the
        new external id and provider expiry — there is still exactly one live
        row per idempotency key, and the old provider copy is left for the
        reconciliation job (Scope §6.7). The verified source identity is
        immutable and never refreshed here.
        """
        if row.external_id != reference.external_id:
            row.external_id = reference.external_id
            row.expires_at = reference.expires_at
        row.last_used_at = datetime.now(UTC)


__all__ = [
    "SQLTransferReferenceStore",
    "TransferReferenceStore",
    "_row_to_reference",
]
