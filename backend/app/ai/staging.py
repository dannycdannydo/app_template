"""Provider-neutral staging/upload seam for large AI attachments (v0.8 Scope §2.3, §6.1).

Non-inline transfer modes never expose provider concepts to the AI layer: the
service talks to a :class:`TransferStore`, which owns the provider-specific
HTTP and Google Cloud behavior behind adapters (Scope §2.3). A store stages a
source object into a provider-hosted or cloud-hosted form and returns an
opaque, provider-neutral :class:`ExternalFileReference` (never a managed signed
URL, credentials, request headers or raw response).

The retry-only reuse rule (Scope §2.1/§2.3) is part of the contract:
:meth:`TransferStore.find_reusable` may return a reference only for a retry of
the same logical AI request (same logical request id, provider, mode, digest
and region) while that reference is still live; distinct requests never share a
reference. Terminal cleanup runs best-effort through :meth:`TransferStore.delete`
and never touches the feature-owned source object (Scope §2.5).

:class:`FakeTransferStore` is the deterministic default for the test suite
(Scope §2.4): it simulates upload, reference, reuse, expiry and deletion in
memory without bytes, so transfer orchestration and lifecycle tests can run
hermetically. Feature modules never import this module (import-boundary test,
Scope §6.1 checkbox 3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, computed_field

from app.ai.transfer import SourceLifecycle, TransferMode, derive_idempotency_key


class ExternalReferenceStatus(StrEnum):
    """Lifecycle status of one provider-side or cloud-side reference."""

    LIVE = "live"
    EXPIRED = "expired"
    DELETED = "deleted"


class ExternalFileReference(BaseModel):
    """A provider-neutral external file reference (Scope §2.3 durable fields).

    Carries only safe, durable metadata: the mode, provider, opaque external
    identifier (a provider file id or ``gs://`` URI — never a signed URL), the
    source storage reference and SHA-256 digest, size, MIME type, source
    lifecycle, region, idempotency key and expiry. It never carries bytes,
    credentials, request headers, a managed signed URL or its query string, or
    a raw provider response. A managed signed URL is minted anew for each
    dispatch/retry and is not a reusable durable reference.
    """

    model_config = {"extra": "forbid"}

    mode: TransferMode
    provider: str = Field(min_length=1, max_length=128)
    external_id: str = Field(min_length=1, max_length=2048)
    source_reference: str = Field(min_length=1, max_length=1024)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    mime_type: str = Field(min_length=1, max_length=128)
    source_lifecycle: SourceLifecycle
    region: str = Field(default="", max_length=128)
    organisation_id: UUID
    logical_request_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    status: ExternalReferenceStatus = ExternalReferenceStatus.LIVE
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    deleted_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_live(self) -> bool:
        """Whether the reference is currently reusable (live and unexpired)."""
        if self.status is not ExternalReferenceStatus.LIVE:
            return False
        return self.expires_at is None or self.expires_at > datetime.now(UTC)


class TransferStore(ABC):
    """The provider-neutral seam between the AI layer and provider file APIs.

    Concrete adapters (OpenAI, Anthropic, Vertex GCS, and the fake) implement
    one :class:`TransferStore` per provider id; the provider-specific HTTP and
    Google Cloud behavior stays behind the adapter (Scope §2.3, ADR-0017). The
    service never constructs provider requests or cloud URIs itself.
    """

    provider_id: str

    @abstractmethod
    async def stage(
        self,
        *,
        mode: TransferMode,
        organisation_id: UUID,
        logical_request_id: str,
        source_reference: str,
        source_digest: str,
        mime_type: str,
        size_bytes: int,
        source_lifecycle: SourceLifecycle,
        region: str,
        expires_at: datetime | None,
    ) -> ExternalFileReference:
        """Stage one source object and return the new external reference.

        Idempotent on the derived idempotency key: a retry that already staged
        a live matching reference receives that reference instead of a second
        upload. Raises a safe AI error on failure; never returns bytes, a
        signed URL or credentials.
        """

    @abstractmethod
    async def find_reusable(
        self,
        *,
        mode: TransferMode,
        organisation_id: UUID,
        logical_request_id: str,
        source_digest: str,
        region: str,
    ) -> ExternalFileReference | None:
        """Return the live matching reference for a retry, or ``None``.

        Reuse is allowed only within retries of one logical AI request and
        only while the reference is live; the digest, provider, mode and
        region must all still match, or ``None`` is returned so the caller
        creates a new idempotent transfer (Scope §2.1, §2.3).
        """

    @abstractmethod
    async def delete(self, reference: ExternalFileReference) -> None:
        """Best-effort terminal deletion of a provider-side reference.

        Never deletes the feature-owned source object; a deletion failure
        leaves the reference in place for the reconciliation job (Scope §2.5).
        """


class FakeTransferStore(TransferStore):
    """Deterministic, in-memory :class:`TransferStore` for the default suite.

    Simulates the upload, reference, reuse, expiry and deletion behavior the
    real provider adapters must exhibit (Scope §2.4) without touching a
    network: external ids are deterministic per idempotency key plus a
    per-store sequence, reuse is scoped to one logical request, and expiry and
    deletion are explicit and inspectable so tests can drive every lifecycle
    path hermetically. No bytes are ever stored.
    """

    provider_id = "fake"

    def __init__(self) -> None:
        self._records: dict[str, ExternalFileReference] = {}
        self._sequence = 0
        self.deleted: list[ExternalFileReference] = []

    @property
    def records(self) -> list[ExternalFileReference]:
        """All references staged by this store, in stage order (tests)."""
        return list(self._records.values())

    async def stage(
        self,
        *,
        mode: TransferMode,
        organisation_id: UUID,
        logical_request_id: str,
        source_reference: str,
        source_digest: str,
        mime_type: str,
        size_bytes: int,
        source_lifecycle: SourceLifecycle,
        region: str,
        expires_at: datetime | None,
    ) -> ExternalFileReference:
        key = derive_idempotency_key(
            provider=self.provider_id,
            mode=mode,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            source_digest=source_digest,
            region=region,
        )
        existing = self._records.get(key)
        if existing is not None and existing.is_live:
            # A live idempotent transfer already exists for this retry: reuse
            # it instead of uploading again (Scope §2.1 retry-only reuse).
            self._last_used(existing)
            return existing
        self._sequence += 1
        reference = ExternalFileReference(
            mode=mode,
            provider=self.provider_id,
            external_id=f"fake-{mode.value}-{key[:16]}-{self._sequence}",
            source_reference=source_reference,
            source_digest=source_digest,
            size_bytes=size_bytes,
            mime_type=mime_type,
            source_lifecycle=source_lifecycle,
            region=region,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            idempotency_key=key,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        self._records[key] = reference
        return reference

    async def find_reusable(
        self,
        *,
        mode: TransferMode,
        organisation_id: UUID,
        logical_request_id: str,
        source_digest: str,
        region: str,
    ) -> ExternalFileReference | None:
        key = derive_idempotency_key(
            provider=self.provider_id,
            mode=mode,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            source_digest=source_digest,
            region=region,
        )
        record = self._records.get(key)
        if record is None or not record.is_live:
            return None
        self._last_used(record)
        return record

    async def delete(self, reference: ExternalFileReference) -> None:
        """Best-effort terminal deletion; never touches the source object."""
        record = self._records.get(reference.idempotency_key)
        if record is None or record.status is ExternalReferenceStatus.DELETED:
            return
        record.status = ExternalReferenceStatus.DELETED
        record.deleted_at = datetime.now(UTC)
        self.deleted.append(record)

    def expire_due(self, *, now: datetime | None = None) -> int:
        """Mark every record whose expiry has passed as expired; returns count.

        Test hook mirroring provider-side expiry: an expired reference is no
        longer reusable and must be replaced by a new idempotent transfer.
        """
        current = now or datetime.now(UTC)
        expired = 0
        for record in self._records.values():
            if (
                record.status is ExternalReferenceStatus.LIVE
                and record.expires_at is not None
                and record.expires_at <= current
            ):
                record.status = ExternalReferenceStatus.EXPIRED
                expired += 1
        return expired

    @staticmethod
    def _last_used(reference: ExternalFileReference) -> None:
        reference.last_used_at = datetime.now(UTC)


__all__ = [
    "ExternalFileReference",
    "ExternalReferenceStatus",
    "FakeTransferStore",
    "TransferStore",
]
