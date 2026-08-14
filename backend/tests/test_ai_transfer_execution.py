"""AIService non-inline transfer execution seam tests (v0.8 Scope §2.3, §6.3, §6.4).

The v0.8 storage-reference and provider-upload seams are exercised
hermetically: the checked-in registry bundle (``document.ask`` + the Vertex,
OpenAI and fake models), a :class:`FakeObjectStorage` source object, the
deterministic :class:`~app.ai.staging.FakeTransferStore` and an in-memory
:class:`TransferReferenceStore` stand in for the GCS staging and OpenAI Files
upload paths. These tests prove the wiring itself — ``AIService.execute``
heads an oversized source, streams it bounded into the staging seam, hands
the adapter an opaque ``staged_file`` reference, records/cleans up the
durable reference and never touches the feature-owned source object — without
Google or OpenAI credentials or network access. The real GCS adapter, the
real OpenAI upload store and the durable SQL store are covered by
``test_ai_vertex_staging.py``, ``test_ai_openai_upload.py`` and
``test_ai_reference_db.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from prometheus_client import REGISTRY
from tests.ai_test_helpers import InMemoryTaskRegistry

from app.ai.anthropic_staging import FakeAnthropicUploadStore
from app.ai.attachments import MAX_ATTACHMENT_BYTES
from app.ai.errors import (
    AIInputValidationError,
    ProviderResponseError,
    ProviderTimeoutError,
    TransferExecutionUnavailableError,
    TransferModeUnavailableError,
    TransferStagingError,
)
from app.ai.persistence.port import AIRequestReservation, OrganisationAIPolicy
from app.ai.providers.fake import FakeLLMProvider
from app.ai.registry import (
    Capability,
    FallbackPolicy,
    LatencyTier,
    QualityTier,
    RetryPolicy,
    TaskDefinition,
    load_registry_bundle,
)
from app.ai.schemas import AIRequest
from app.ai.service import AIService
from app.ai.staging import (
    ExternalFileReference,
    ExternalReferenceStatus,
    FakeTransferStore,
    TransferStore,
)
from app.ai.storage_resolver import StorageAttachmentResolver
from app.ai.transfer import (
    SourceLifecycle,
    TransferDeploymentPolicy,
    TransferMode,
    derive_idempotency_key,
)
from app.ai.transfer_orchestrator import TransferOrchestrator
from app.storage.fake import FakeObjectStorage
from app.storage.types import SignedUrl

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()

#: A source object comfortably above the 5,000,000-byte inline threshold but
#: well below the 50,000,000-byte large-file ceiling.
_BIG_PDF_BYTES = MAX_ATTACHMENT_BYTES + 1024
_INLINE_THRESHOLD = 5_000_000


class _InMemoryReferenceStore:
    """Minimal in-memory :class:`TransferReferenceStore` for the seam tests.

    Mirrors the SQL store's observable contract (idempotent create-or-adopt,
    retry-only live reuse by idempotency key, authoritative row resolution for
    deletion) without a database; the full SQL semantics are covered by
    ``test_ai_reference_db.py``.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, ExternalFileReference] = {}

    @property
    def records(self) -> list[ExternalFileReference]:
        """Every reference stored by this store, in insertion order (tests)."""
        return list(self._by_key.values())

    async def create_or_adopt(self, reference: ExternalFileReference) -> ExternalFileReference:
        existing = self._by_key.get(reference.idempotency_key)
        if existing is not None and existing.is_live:
            adopted = existing.model_copy(
                update={"external_id": reference.external_id, "last_used_at": datetime.now(UTC)}
            )
            self._by_key[reference.idempotency_key] = adopted
            return adopted
        self._by_key[reference.idempotency_key] = reference
        return reference

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
        # Mirrors the SQL store: the derived idempotency key is the exact
        # reuse predicate (provider, mode, org, logical request, digest, region).
        key = derive_idempotency_key(
            provider=provider_id,
            mode=mode,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            source_digest=source_digest,
            region=region,
        )
        candidate = self._by_key.get(key)
        if candidate is not None and candidate.is_live:
            return candidate
        return None

    async def adopt(self, *, organisation_id: UUID, idempotency_key: str) -> bool:
        reference = self._by_key.get(idempotency_key)
        if reference is None or not reference.is_live:
            return False
        self._by_key[idempotency_key] = reference.model_copy(
            update={"last_used_at": datetime.now(UTC)}
        )
        return True

    async def mark_expired(self, *, organisation_id: UUID, idempotency_key: str) -> bool:
        reference = self._by_key.get(idempotency_key)
        if reference is None or reference.status is not ExternalReferenceStatus.LIVE:
            return False
        self._by_key[idempotency_key] = reference.model_copy(
            update={"status": ExternalReferenceStatus.EXPIRED}
        )
        return True

    async def mark_deleted(self, *, organisation_id: UUID, idempotency_key: str) -> bool:
        reference = self._by_key.get(idempotency_key)
        if reference is None:
            return False
        self._by_key[idempotency_key] = reference.model_copy(
            update={
                "status": ExternalReferenceStatus.DELETED,
                "deleted_at": datetime.now(UTC),
            }
        )
        return True

    async def mark_deletion_attempted(
        self, *, organisation_id: UUID, idempotency_key: str, error_code: str | None
    ) -> bool:
        reference = self._by_key.get(idempotency_key)
        if reference is None or reference.status is ExternalReferenceStatus.DELETED:
            return False
        self._by_key[idempotency_key] = reference.model_copy(
            update={
                "deletion_attempted_at": datetime.now(UTC),
                "error_code": error_code,
            }
        )
        return True

    async def claim_needing_reconciliation(
        self, *, retry_after: datetime, batch_size: int
    ) -> list[ExternalFileReference]:
        return [
            r for r in self._by_key.values() if r.status is not ExternalReferenceStatus.DELETED
        ][:batch_size]

    async def claim_for_deletion(
        self, *, organisation_id: UUID, idempotency_key: str
    ) -> ExternalFileReference | None:
        reference = self._by_key.get(idempotency_key)
        if reference is None or reference.status is ExternalReferenceStatus.DELETED:
            return None
        claimed = reference.model_copy(update={"deletion_attempted_at": datetime.now(UTC)})
        self._by_key[idempotency_key] = claimed
        return claimed

    async def list_for_request(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> list[ExternalFileReference]:
        return [
            reference
            for reference in self._by_key.values()
            if reference.logical_request_id == logical_request_id
        ]

    async def expire_all_for_request(
        self, *, organisation_id: UUID, logical_request_id: str
    ) -> int:
        expired = 0
        for reference in await self.list_for_request(
            organisation_id=organisation_id, logical_request_id=logical_request_id
        ):
            if await self.mark_expired(
                organisation_id=organisation_id, idempotency_key=reference.idempotency_key
            ):
                expired += 1
        return expired


class _StorageReferenceRecorder:
    """Minimal :class:`AIPersistencePort` permitting the storage-reference mode."""

    def __init__(self) -> None:
        self.policy = OrganisationAIPolicy(
            enabled=True,
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.STORAGE_REFERENCE],
        )

    async def load_policy(self, *, organisation_id: object) -> OrganisationAIPolicy:
        return self.policy

    async def reserve(self, **kwargs: object) -> AIRequestReservation:
        return AIRequestReservation(row_id=uuid.uuid4(), created=True)

    async def record_attempt(self, **kwargs: object) -> UUID:
        return uuid.uuid4()

    async def settle(self, **kwargs: object) -> None:
        return None


class _ProviderUploadRecorder:
    """Minimal :class:`AIPersistencePort` permitting the provider-upload mode."""

    def __init__(self) -> None:
        self.policy = OrganisationAIPolicy(
            enabled=True,
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
        )

    async def load_policy(self, *, organisation_id: object) -> OrganisationAIPolicy:
        return self.policy

    async def reserve(self, **kwargs: object) -> AIRequestReservation:
        return AIRequestReservation(row_id=uuid.uuid4(), created=True)

    async def record_attempt(self, **kwargs: object) -> UUID:
        return uuid.uuid4()

    async def settle(self, **kwargs: object) -> None:
        return None


class _ManagedUrlRecorder:
    """Minimal :class:`AIPersistencePort` permitting the managed-signed-url mode."""

    def __init__(self) -> None:
        self.policy = OrganisationAIPolicy(
            enabled=True,
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.MANAGED_SIGNED_URL],
        )

    async def load_policy(self, *, organisation_id: object) -> OrganisationAIPolicy:
        return self.policy

    async def reserve(self, **kwargs: object) -> AIRequestReservation:
        return AIRequestReservation(row_id=uuid.uuid4(), created=True)

    async def record_attempt(self, **kwargs: object) -> UUID:
        return uuid.uuid4()

    async def settle(self, **kwargs: object) -> None:
        return None


def _service(
    *, storage: FakeObjectStorage, enabled_transfer_modes: set[TransferMode] | None = None
) -> tuple[AIService, FakeLLMProvider, FakeTransferStore, _InMemoryReferenceStore]:
    bundle = load_registry_bundle()
    fake_provider = FakeLLMProvider()
    store = FakeTransferStore()
    references = _InMemoryReferenceStore()
    service = AIService(
        task_registry=bundle.tasks,
        prompt_registry=bundle.prompts,
        model_registry=bundle.models,
        providers={"fake": fake_provider},
        attachment_resolver=StorageAttachmentResolver(storage),
        transfer_deployment=TransferDeploymentPolicy(
            inline_aggregate_threshold_bytes=_INLINE_THRESHOLD,
            max_large_attachment_bytes=50_000_000,
            enabled_transfer_modes=frozenset(
                enabled_transfer_modes or {TransferMode.STORAGE_REFERENCE}
            ),
        ),
        storage=storage,
        transfer_stores={"fake": store},
    )
    return service, fake_provider, store, references


def _ask_request(
    storage_reference: str, *, question: str = "What is in this document?"
) -> AIRequest:
    return AIRequest(
        task="document.ask",
        storage_reference=storage_reference,
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        metadata={"question": question},
    )


async def _put_pdf(storage: FakeObjectStorage, *, size: int) -> str:
    key = f"organisations/{_ORG_ID}/documents/doc-{uuid.uuid4().hex[:8]}/original"
    payload = b"%PDF-1.4\n%%EOF\n" + b"x" * max(0, size - len(b"%PDF-1.4\n%%EOF\n"))
    await storage.put(key, payload, content_type="application/pdf")
    return key


async def _put_scratch_pdf(storage: FakeObjectStorage, *, size: int) -> str:
    """A transient source in the organisation-scoped AI scratch namespace."""
    key = f"organisations/{_ORG_ID}/ai/scratch/doc-{uuid.uuid4().hex[:8]}.pdf"
    payload = b"%PDF-1.4\n%%EOF\n" + b"x" * max(0, size - len(b"%PDF-1.4\n%%EOF\n"))
    await storage.put(key, payload, content_type="application/pdf")
    return key


async def test_large_pdf_routes_through_staging_seam() -> None:
    """A PDF above the inline threshold stages, dispatches with a staged_file
    and best-effort-deletes the AI-owned copy after success (v0.8 Scope
    §2.4/§2.5); the feature-owned source object is untouched."""
    storage = FakeObjectStorage(bucket="seam-test")
    service, fake_provider, store, references = _service(storage=storage)
    key = await _put_pdf(storage, size=_BIG_PDF_BYTES)

    result = await service.execute(
        _ask_request(key),
        recorder=_StorageReferenceRecorder(),
        transfer_references=references,
    )

    assert isinstance(result.output, str) and result.output
    assert result.routing.provider == "fake"
    assert len(fake_provider.requests) == 1
    request = fake_provider.requests[0]
    assert request.attachments == []
    assert request.staged_file is not None
    assert request.staged_file.mime_type == "application/pdf"
    assert request.staged_file.external_id.startswith("fake-")
    # The staged reference was recorded and its AI-owned copy deleted through
    # the store immediately after terminal success (Scope §2.5 permits
    # immediate best-effort terminal deletion of the GCS staging object; only
    # the *scheduled* §6.7 reconciliation job excludes storage references —
    # the deployer's age = 1 lifecycle is the backstop); the feature-owned
    # source object still exists.
    assert len(store.records) == 1
    assert len(store.deleted) == 1
    assert store.deleted[0].external_id == store.records[0].external_id
    staged = store.records[0]
    assert staged.source_reference == key
    assert staged.size_bytes == _BIG_PDF_BYTES
    assert await storage.head_object(key) is not None


async def test_small_pdf_uses_inline_path_without_staging() -> None:
    """A PDF at or below the inline threshold resolves inline; nothing is
    staged and no staged_file reaches the adapter (v0.8 Scope §2.1)."""
    storage = FakeObjectStorage(bucket="seam-test")
    service, fake_provider, store, references = _service(storage=storage)
    key = await _put_pdf(storage, size=_INLINE_THRESHOLD)

    result = await service.execute(
        _ask_request(key),
        recorder=_StorageReferenceRecorder(),
        transfer_references=references,
    )

    assert isinstance(result.output, str)
    assert store.records == [] and store.deleted == []
    assert len(fake_provider.requests) == 1
    assert fake_provider.requests[0].staged_file is None
    assert len(fake_provider.requests[0].attachments) == 1
    assert fake_provider.requests[0].attachments[0].mime_type == "application/pdf"
    assert "What is in this document?" in fake_provider.requests[0].prompt


async def test_large_non_pdf_fails_the_shape_gate_before_transfer() -> None:
    """A large object that is not exactly one PDF fails before any staging or
    dispatch (v0.8 Scope §2.1 decision 3, §5.3)."""
    storage = FakeObjectStorage(bucket="seam-test")
    service, fake_provider, store, references = _service(storage=storage)
    key = f"organisations/{_ORG_ID}/ai/scratch/doc-{uuid.uuid4().hex[:8]}.txt"
    await storage.put(key, b"y" * _BIG_PDF_BYTES, content_type="text/plain")

    with pytest.raises(TransferModeUnavailableError, match="exactly one application/pdf"):
        await service.execute(
            _ask_request(key),
            recorder=_StorageReferenceRecorder(),
            transfer_references=references,
        )
    assert store.records == [] and store.deleted == []
    assert fake_provider.requests == []


async def test_cross_organisation_large_reference_is_denied() -> None:
    """A large reference outside the requesting organisation's namespace fails
    closed before any bytes are streamed (v0.8 Scope §5.8)."""
    storage = FakeObjectStorage(bucket="seam-test")
    other_org = uuid.uuid4()
    key = f"organisations/{other_org}/documents/doc-1/original"
    await storage.put(key, b"z" * _BIG_PDF_BYTES, content_type="application/pdf")
    service, fake_provider, store, references = _service(storage=storage)

    with pytest.raises(AIInputValidationError, match="not accessible"):
        await service.execute(
            _ask_request(key),
            recorder=_StorageReferenceRecorder(),
            transfer_references=references,
        )
    assert store.records == [] and store.deleted == []
    assert fake_provider.requests == []


async def test_storage_reference_mode_without_seam_fails_closed() -> None:
    """A service wired with storage but without the staging store refuses a
    selected storage-reference mode before any external transfer (v0.8 Scope
    §6.3)."""
    bundle = load_registry_bundle()
    storage = FakeObjectStorage(bucket="seam-test")
    service = AIService(
        task_registry=bundle.tasks,
        prompt_registry=bundle.prompts,
        model_registry=bundle.models,
        providers={"fake": FakeLLMProvider()},
        attachment_resolver=StorageAttachmentResolver(storage),
        transfer_deployment=TransferDeploymentPolicy(
            inline_aggregate_threshold_bytes=_INLINE_THRESHOLD,
            enabled_transfer_modes=frozenset({TransferMode.STORAGE_REFERENCE}),
        ),
        storage=storage,
        # Deliberately no transfer_stores.
    )
    key = await _put_pdf(storage, size=_BIG_PDF_BYTES)

    with pytest.raises(TransferExecutionUnavailableError, match="not executable"):
        await service.execute(
            _ask_request(key),
            recorder=_StorageReferenceRecorder(),
            transfer_references=_InMemoryReferenceStore(),
        )


async def test_large_transient_pdf_routes_through_provider_upload_seam() -> None:
    """A transient (scratch) PDF above the inline threshold stages through the
    provider-upload mode, dispatches with a staged_file and best-effort-deletes
    the AI-owned copy after success (v0.8 Scope §2.4/§2.5/§6.5); the
    feature-owned source object is untouched."""
    storage = FakeObjectStorage(bucket="seam-test")
    service, fake_provider, store, references = _service(
        storage=storage, enabled_transfer_modes={TransferMode.PROVIDER_UPLOAD}
    )
    key = await _put_scratch_pdf(storage, size=_BIG_PDF_BYTES)

    result = await service.execute(
        _ask_request(key),
        recorder=_ProviderUploadRecorder(),
        transfer_references=references,
    )

    assert isinstance(result.output, str) and result.output
    assert result.routing.provider == "fake"
    assert len(fake_provider.requests) == 1
    request = fake_provider.requests[0]
    assert request.attachments == []
    assert request.staged_file is not None
    assert request.staged_file.mime_type == "application/pdf"
    assert request.staged_file.external_id.startswith("fake-provider_upload-")
    # The staged reference was recorded with the provider-upload mode and its
    # AI-owned copy deleted after terminal success; the source still exists.
    assert len(store.records) == 1
    assert store.records[0].mode is TransferMode.PROVIDER_UPLOAD
    assert store.records[0].source_lifecycle is SourceLifecycle.TRANSIENT
    assert len(store.deleted) == 1
    assert store.deleted[0].idempotency_key == store.records[0].idempotency_key
    staged = store.records[0]
    assert staged.source_reference == key
    assert staged.size_bytes == _BIG_PDF_BYTES
    assert await storage.head_object(key) is not None


async def test_retained_large_pdf_never_rides_provider_upload() -> None:
    """The provider-upload mode serves transient sources only (Scope §2.2
    reviewed contract): a retained large PDF with only provider_upload enabled
    has no eligible mode and fails before any transfer."""
    storage = FakeObjectStorage(bucket="seam-test")
    service, fake_provider, store, references = _service(
        storage=storage, enabled_transfer_modes={TransferMode.PROVIDER_UPLOAD}
    )
    key = await _put_pdf(storage, size=_BIG_PDF_BYTES)  # retained documents path

    with pytest.raises(TransferModeUnavailableError, match="no permitted"):
        await service.execute(
            _ask_request(key),
            recorder=_ProviderUploadRecorder(),
            transfer_references=references,
        )
    assert store.records == [] and store.deleted == []
    assert fake_provider.requests == []


async def test_large_retained_pdf_routes_through_managed_url_seam() -> None:
    """A retained (documents) PDF above the inline threshold builds a durable
    managed-signed-url reference and dispatches a just-in-time signed URL —
    never a staged provider copy — and the source object is untouched (v0.8
    Scope §2.3/§2.5/§6.3)."""
    storage = FakeObjectStorage(bucket="seam-test")
    service, fake_provider, store, references = _service(
        storage=storage, enabled_transfer_modes={TransferMode.MANAGED_SIGNED_URL}
    )
    key = await _put_pdf(storage, size=_BIG_PDF_BYTES)  # retained documents path

    result = await service.execute(
        _ask_request(key),
        recorder=_ManagedUrlRecorder(),
        transfer_references=references,
    )

    assert isinstance(result.output, str) and result.output
    assert result.routing.provider == "fake"
    assert len(fake_provider.requests) == 1
    request = fake_provider.requests[0]
    assert request.staged_file is None
    assert request.managed_url is not None
    assert request.managed_url.startswith("https://")
    # The durable reference records the managed mode in the reference store;
    # no provider copy was ever staged or deleted, and the retained source
    # object still exists.
    assert len(store.records) == 0 and store.deleted == []
    assert len(references.records) == 1
    durable = references.records[0]
    assert durable.mode is TransferMode.MANAGED_SIGNED_URL
    assert durable.source_lifecycle is SourceLifecycle.RETAINED
    assert durable.external_id == key
    assert await storage.head_object(key) is not None


async def test_transient_large_pdf_never_rides_managed_url() -> None:
    """The managed-signed-url mode serves retained sources only (Scope §2.2
    reviewed contract): a transient scratch large PDF with only managed_url
    enabled has no eligible mode and fails before any transfer."""
    storage = FakeObjectStorage(bucket="seam-test")
    service, fake_provider, store, references = _service(
        storage=storage, enabled_transfer_modes={TransferMode.MANAGED_SIGNED_URL}
    )
    key = await _put_scratch_pdf(storage, size=_BIG_PDF_BYTES)  # transient scratch path

    with pytest.raises(TransferModeUnavailableError, match="no permitted"):
        await service.execute(
            _ask_request(key),
            recorder=_ManagedUrlRecorder(),
            transfer_references=references,
        )
    assert store.records == [] and store.deleted == []
    assert fake_provider.requests == []


class _NoCopyTransferStore(TransferStore):
    """A provider_upload store that yields no-copy references (external id =
    source identity), mirroring the Anthropic store's local-transient
    scratch-GCS behavior (Scope §6.6 lesson learned)."""

    provider_id = "fake"

    def __init__(self) -> None:
        self.staged: list[ExternalFileReference] = []
        self.deleted_keys: list[str] = []

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
        source_path: Path | None = None,
    ) -> ExternalFileReference:
        reference = ExternalFileReference(
            mode=mode,
            provider=self.provider_id,
            external_id=source_reference,
            source_reference=source_reference,
            source_digest=source_digest,
            size_bytes=size_bytes,
            mime_type=mime_type,
            source_lifecycle=source_lifecycle,
            region=region,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            idempotency_key="idem-no-copy",
            created_at=datetime.now(UTC),
        )
        self.staged.append(reference)
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
        return None

    async def delete(self, reference: ExternalFileReference) -> None:
        self.deleted_keys.append(reference.external_id)


class _FakeManagedUrlStager:
    """Deterministic :class:`ManagedUrlStager` standing in for the dev GCS
    staging seam (local storage)."""

    region = "europe-west1"

    async def mint(
        self,
        *,
        reference: ExternalFileReference,
        ttl_seconds: int,
        source_storage: object,
    ) -> SignedUrl:
        assert reference.source_lifecycle is SourceLifecycle.TRANSIENT
        return SignedUrl(
            url="https://storage.googleapis.com/scratch/lease.pdf?X-Goog-Signature=secret",
            method="GET",
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )


async def test_local_transient_provider_upload_served_by_managed_url_seam() -> None:
    """With a local storage seam a transient provider_upload dispatch is served
    by a just-in-time signed URL to the scratch-GCS copy instead of a provider
    file id (Scope §6.6 lesson learned): the no-copy reference travels as a
    staged_file plus a dispatch-time managed_url, and terminal cleanup marks
    the no-copy reference deleted without touching the source."""
    bundle = load_registry_bundle()
    storage = FakeObjectStorage(bucket="seam-test")
    references = _InMemoryReferenceStore()
    no_copy_store = _NoCopyTransferStore()
    stager = _FakeManagedUrlStager()
    fake_provider = FakeLLMProvider()
    service = AIService(
        task_registry=bundle.tasks,
        prompt_registry=bundle.prompts,
        model_registry=bundle.models,
        providers={"fake": fake_provider},
        attachment_resolver=StorageAttachmentResolver(storage),
        transfer_deployment=TransferDeploymentPolicy(
            inline_aggregate_threshold_bytes=_INLINE_THRESHOLD,
            max_large_attachment_bytes=50_000_000,
            enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD}),
        ),
        storage=storage,
        transfer_stores={"fake": no_copy_store},
        managed_url_stager=stager,
    )
    key = await _put_scratch_pdf(storage, size=_BIG_PDF_BYTES)  # transient scratch path

    result = await service.execute(
        _ask_request(key),
        recorder=_ProviderUploadRecorder(),
        transfer_references=references,
    )

    assert isinstance(result.output, str) and result.output
    assert len(fake_provider.requests) == 1
    request = fake_provider.requests[0]
    # The dispatch carries the no-copy reference and the dispatch-time managed
    # URL; the managed URL wins as the document source.
    assert request.staged_file is not None
    assert request.staged_file.external_id == key
    assert request.managed_url is not None
    assert request.managed_url.startswith("https://storage.googleapis.com/")
    # The durable reference records the provider_upload mode with the no-copy
    # shape; the feature-owned source object is untouched.
    assert len(references.records) == 1
    durable = references.records[0]
    assert durable.mode is TransferMode.PROVIDER_UPLOAD
    assert durable.source_lifecycle is SourceLifecycle.TRANSIENT
    assert durable.external_id == key
    assert await storage.head_object(key) is not None
    # Terminal cleanup marks the no-copy reference deleted through the store
    # (no provider file call); the feature-owned source object is untouched.
    assert no_copy_store.deleted_keys == [key]
    assert len(references.records) == 1
    assert references.records[0].status is ExternalReferenceStatus.DELETED


# --- Scope §6.6: page/context ceiling on both Anthropic non-inline modes ----
#
# The routed Anthropic model's reviewed PDF page ceiling (providers.yaml
# `pdf_pages` + the model's context window -> 100 pages for
# claude-sonnet-4-6) must close over *both* non-inline operations: the
# provider-upload store rejects pre-upload, and the managed-signed-url
# branch rejects pre-mint/pre-dispatch. These service-level tests prove no
# provider call, no signed-URL mint and no scratch-GCS stage happens for an
# over-ceiling source.


def _classic_pdf(objects: list[tuple[int, bytes]], *, padding: bytes = b"") -> bytes:
    """A minimal classic xref-table PDF (see test_ai_anthropic_upload.py).

    ``padding`` is appended between the last object and the cross-reference
    table (e.g. a long ``%`` comment) so a fixture can be grown to any size
    while the trailer, xref table and ``startxref`` stay at the end where a
    real PDF keeps them.
    """
    body = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for number, obj in objects:
        offsets[number] = len(body)
        body += obj
    body += padding
    xref_offset = len(body)
    max_number = max(number for number, _ in objects)
    body += b"xref\n0 %d\n" % (max_number + 1)
    body += b"0000000000 65535 f \n"
    for number in range(1, max_number + 1):
        offset = offsets.get(number)
        if offset is None:
            body += b"0000000000 65535 f \n"
        else:
            body += b"%010d 00000 n \n" % offset
    body += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (max_number + 1)
    body += f"startxref\n{xref_offset}\n%%EOF\n".encode()
    return bytes(body)


def _multi_page_pdf(page_count: int, *, pad_to: int | None = None) -> bytes:
    """A minimal classic PDF with ``page_count`` page-tree leaves.

    With ``pad_to`` the fixture is grown to at least that many bytes via a
    ``%`` comment before the cross-reference table (never after ``%%EOF``,
    which would push the trailer out of the inspector's bounded tail read).
    """
    objects: list[tuple[int, bytes]] = [
        (1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"),
        (
            2,
            (
                b"2 0 obj\n<< /Type /Pages /Count %d /Kids [%s] >>\nendobj\n"
                % (page_count, b" ".join(b"%d 0 R" % i for i in range(3, 3 + page_count)))
            ),
        ),
    ]
    objects.extend(
        (i, b"%d 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n" % i)
        for i in range(3, 3 + page_count)
    )
    padding = b""
    if pad_to is not None:
        padding = b"% " + b" " * pad_to + b"\n"
    return _classic_pdf(objects, padding=padding)


async def _put_pdf_with_pages(storage: FakeObjectStorage, *, page_count: int, size: int) -> str:
    """A retained (documents) multi-page PDF above the inline threshold."""
    key = f"organisations/{_ORG_ID}/documents/doc-{uuid.uuid4().hex[:8]}/original"
    await storage.put(key, _multi_page_pdf(page_count, pad_to=size), content_type="application/pdf")
    return key


async def _put_scratch_pdf_with_pages(
    storage: FakeObjectStorage, *, page_count: int, size: int
) -> str:
    """A transient (scratch) multi-page PDF above the inline threshold."""
    key = f"organisations/{_ORG_ID}/ai/scratch/doc-{uuid.uuid4().hex[:8]}.pdf"
    await storage.put(key, _multi_page_pdf(page_count, pad_to=size), content_type="application/pdf")
    return key


class _RecordingManagedUrlStager:
    """A :class:`ManagedUrlStager` that records every mint (or its absence)."""

    region = "europe-west1"

    def __init__(self) -> None:
        self.mints: list[ExternalFileReference] = []

    async def mint(
        self,
        *,
        reference: ExternalFileReference,
        ttl_seconds: int,
        source_storage: object,
    ) -> SignedUrl:
        self.mints.append(reference)
        return SignedUrl(
            url="https://storage.googleapis.com/scratch/lease.pdf?X-Goog-Signature=secret",
            method="GET",
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )


def _anthropic_service(
    *,
    storage: FakeObjectStorage,
    enabled_transfer_modes: set[TransferMode],
    store: TransferStore | None = None,
    stager: _RecordingManagedUrlStager | None = None,
) -> tuple[AIService, FakeLLMProvider, _InMemoryReferenceStore]:
    """A service configured with the Anthropic provider (routing to
    ``anthropic.claude-sonnet-4-6``, whose 200k context window selects the
    reviewed 100-page ceiling)."""
    bundle = load_registry_bundle()
    provider = FakeLLMProvider()
    references = _InMemoryReferenceStore()
    service = AIService(
        task_registry=bundle.tasks,
        prompt_registry=bundle.prompts,
        model_registry=bundle.models,
        providers={"anthropic": provider},
        attachment_resolver=StorageAttachmentResolver(storage),
        transfer_deployment=TransferDeploymentPolicy(
            inline_aggregate_threshold_bytes=_INLINE_THRESHOLD,
            max_large_attachment_bytes=50_000_000,
            enabled_transfer_modes=frozenset(enabled_transfer_modes),
        ),
        storage=storage,
        transfer_stores={"anthropic": store} if store is not None else {},
        managed_url_stager=stager,
    )
    return service, provider, references


async def test_anthropic_retained_over_ceiling_pdf_rejected_before_url_mint() -> None:
    """A retained 101+-page PDF for the small-context Anthropic model never
    reaches the managed-URL path: the common source boundary rejects it before
    the durable reference exists or any signed URL is minted/dispatched
    (Scope §6.6 checkbox 2)."""
    storage = FakeObjectStorage(bucket="seam-test")
    stager = _RecordingManagedUrlStager()
    service, provider, references = _anthropic_service(
        storage=storage,
        enabled_transfer_modes={TransferMode.MANAGED_SIGNED_URL},
        stager=stager,
    )
    key = await _put_pdf_with_pages(storage, page_count=101, size=_BIG_PDF_BYTES)

    with pytest.raises(AIInputValidationError, match="exceeds the reviewed 100-page ceiling"):
        await service.execute(
            _ask_request(key),
            recorder=_ManagedUrlRecorder(),
            transfer_references=references,
        )
    # No provider call, no signed URL minted, no durable reference created,
    # and the retained source object is untouched.
    assert provider.requests == []
    assert stager.mints == []
    assert references.records == []
    assert await storage.head_object(key) is not None


async def test_anthropic_retained_boundary_pdf_dispatches_managed_url() -> None:
    """The boundary (100 pages = the reviewed ceiling) is accepted and the
    retained source dispatches through a just-in-time managed URL."""
    storage = FakeObjectStorage(bucket="seam-test")
    stager = _RecordingManagedUrlStager()
    service, provider, references = _anthropic_service(
        storage=storage,
        enabled_transfer_modes={TransferMode.MANAGED_SIGNED_URL},
        stager=stager,
    )
    key = await _put_pdf_with_pages(storage, page_count=100, size=_BIG_PDF_BYTES)

    result = await service.execute(
        _ask_request(key),
        recorder=_ManagedUrlRecorder(),
        transfer_references=references,
    )
    assert isinstance(result.output, str) and result.output
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.managed_url is not None
    assert len(references.records) == 1
    durable = references.records[0]
    assert durable.mode is TransferMode.MANAGED_SIGNED_URL
    assert durable.source_lifecycle is SourceLifecycle.RETAINED
    assert await storage.head_object(key) is not None


async def test_anthropic_transient_over_ceiling_pdf_rejected_before_scratch_stage() -> None:
    """A transient 101+-page PDF is rejected at the common source boundary
    before any upload or scratch-GCS staging: no no-copy reference, no signed-
    URL mint and no provider call (Scope §6.6 checkbox 2)."""
    storage = FakeObjectStorage(bucket="seam-test")
    stager = _RecordingManagedUrlStager()
    store = FakeAnthropicUploadStore(region="")
    service, provider, references = _anthropic_service(
        storage=storage,
        enabled_transfer_modes={TransferMode.PROVIDER_UPLOAD},
        store=store,
        stager=stager,
    )
    key = await _put_scratch_pdf_with_pages(storage, page_count=101, size=_BIG_PDF_BYTES)

    with pytest.raises(AIInputValidationError, match="exceeds the reviewed 100-page ceiling"):
        await service.execute(
            _ask_request(key),
            recorder=_ProviderUploadRecorder(),
            transfer_references=references,
        )
    assert store.uploads == []
    assert store.records == []
    assert provider.requests == []
    assert stager.mints == []
    assert references.records == []
    assert await storage.head_object(key) is not None


# --- §6.7 terminal failure/timeout outcomes and failure observability --------


def _outcome_samples(mode: str, provider: str, result: str) -> float:
    """Current process-wide ``ai_transfer_outcomes_total`` sample for labels."""
    return (
        REGISTRY.get_sample_value(
            "ai_transfer_outcomes_total",
            {"mode": mode, "provider": provider, "result": result},
        )
        or 0.0
    )


async def test_failed_staging_records_safe_failure_audit_and_outcome_metric() -> None:
    """A provider upload/staging failure emits ``ai.transfer_failed`` carrying
    the safe taxonomy error code only — never exception text, URLs, external
    ids, keys or content — and increments the transfer-outcome metric (Scope
    §6.7 checkbox 3)."""
    recorded: list[dict[str, Any]] = []

    async def _recorder(
        action: str,
        resource_type: str,
        resource_id: str,
        organisation_id: UUID,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        recorded.append({"action": action, "metadata": metadata or {}})

    class _BrokenStore(FakeTransferStore):
        async def stage(self, **kwargs: object) -> ExternalFileReference:
            raise TransferStagingError("upload refused https://secret.example/file-abc123?X=Y")

    orchestrator = TransferOrchestrator(
        storage=FakeObjectStorage(bucket="seam-test"),
        store=_BrokenStore(),
        references=_InMemoryReferenceStore(),
        audit_recorder=_recorder,
    )
    before = _outcome_samples("provider_upload", "fake", "failed")
    with pytest.raises(TransferStagingError):
        await orchestrator.create_or_reuse_reference(
            organisation_id=_ORG_ID,
            logical_request_id="req-stage-fail",
            provider_id="fake",
            mode=TransferMode.PROVIDER_UPLOAD,
            source_reference=f"organisations/{_ORG_ID}/documents/doc-1/original",
            source_digest="ab" * 32,
            size_bytes=_BIG_PDF_BYTES,
            mime_type="application/pdf",
            source_lifecycle=SourceLifecycle.TRANSIENT,
            region="eu-west-1",
            expires_at=None,
        )
    assert _outcome_samples("provider_upload", "fake", "failed") == before + 1
    assert len(recorded) == 1
    assert recorded[0]["action"] == "ai.transfer_failed"
    metadata = recorded[0]["metadata"]
    assert set(metadata) == {"transfer_mode", "provider", "error_code"}
    assert metadata["error_code"] == "transfer_staging_failed"
    assert metadata["transfer_mode"] == "provider_upload"
    # Redaction: the URL/external-id text in the exception never leaks.
    assert "secret.example" not in str(metadata)
    assert "file-abc123" not in str(metadata)


async def test_failed_deletion_records_safe_failure_audit_and_outcome_metric() -> None:
    """An immediate provider deletion failure emits ``ai.transfer_failed`` with
    the safe error code only and increments the outcome metric (Scope §6.7
    checkbox 3); the durable row keeps its safe failure marker."""
    recorded: list[dict[str, Any]] = []

    async def _recorder(
        action: str,
        resource_type: str,
        resource_id: str,
        organisation_id: UUID,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        recorded.append({"action": action, "metadata": metadata or {}})

    class _BrokenDeleteStore(FakeTransferStore):
        async def delete(self, reference: ExternalFileReference) -> None:
            raise ProviderResponseError("delete refused file-abc123 at https://secret.example/x")

    store = _BrokenDeleteStore()
    references = _InMemoryReferenceStore()
    orchestrator = TransferOrchestrator(
        storage=FakeObjectStorage(bucket="seam-test"),
        store=store,
        references=references,
        audit_recorder=_recorder,
    )
    staged = await orchestrator.create_or_reuse_reference(
        organisation_id=_ORG_ID,
        logical_request_id="req-delete-fail",
        provider_id="fake",
        mode=TransferMode.PROVIDER_UPLOAD,
        source_reference=f"organisations/{_ORG_ID}/documents/doc-1/original",
        source_digest="cd" * 32,
        size_bytes=_BIG_PDF_BYTES,
        mime_type="application/pdf",
        source_lifecycle=SourceLifecycle.TRANSIENT,
        region="eu-west-1",
        expires_at=None,
    )
    before = _outcome_samples("provider_upload", "fake", "failed")
    with pytest.raises(TransferExecutionUnavailableError):
        await orchestrator.delete_reference(reference=staged)
    assert _outcome_samples("provider_upload", "fake", "failed") == before + 1
    assert recorded[-1]["action"] == "ai.transfer_failed"
    metadata = recorded[-1]["metadata"]
    assert set(metadata) == {"transfer_mode", "provider", "error_code"}
    assert metadata["error_code"] == "provider_response_invalid"
    assert "secret.example" not in str(metadata)
    assert "file-abc123" not in str(metadata)
    # The durable row was claimed and keeps the safe deletion-failure marker
    # for the reconciliation sweep (Scope §2.5).
    durable = references.records[0]
    assert durable.status is ExternalReferenceStatus.LIVE
    assert durable.error_code == "provider_reference_deletion_failed"
    assert durable.deletion_attempted_at is not None


async def test_provider_upload_permanent_failure_still_runs_terminal_cleanup() -> None:
    """A permanent provider failure after staging still runs the terminal
    cleanup: the AI-owned copy is deleted through the store, the durable
    reference is expired then deleted, and the feature-owned source object is
    untouched (Scope §6.7 checkbox 1/4)."""
    storage = FakeObjectStorage(bucket="seam-test")
    service, fake_provider, store, references = _service(
        storage=storage, enabled_transfer_modes={TransferMode.PROVIDER_UPLOAD}
    )
    key = await _put_scratch_pdf(storage, size=_BIG_PDF_BYTES)
    fake_provider.fail_next_call(error=ProviderResponseError)

    with pytest.raises(ProviderResponseError):
        await service.execute(
            _ask_request(key),
            recorder=_ProviderUploadRecorder(),
            transfer_references=references,
        )
    # The staged copy was deleted after the failure and the durable reference
    # is terminal; the feature source is untouched.
    assert len(store.records) == 1
    assert len(store.deleted) == 1
    assert len(references.records) == 1
    assert references.records[0].status is ExternalReferenceStatus.DELETED
    assert await storage.head_object(key) is not None


async def test_provider_upload_timeout_exhausts_retries_still_runs_terminal_cleanup() -> None:
    """An exhausted bounded timeout retry budget after staging still runs the
    terminal cleanup: the retry reuses the live reference (one copy staged),
    the copy is then deleted, the durable reference is terminal, and the
    feature-owned source object is untouched (Scope §6.7 checkbox 1/4)."""
    bundle = load_registry_bundle()
    tasks = InMemoryTaskRegistry(
        {
            "document.transfer-fail": TaskDefinition(
                name="document.transfer-fail",
                prompt_name="document.ask",
                prompt_version=1,
                input_variables=["storage_reference", "question"],
                required_capabilities=[Capability.DOCUMENTS],
                parameter_defaults={"max_tokens": 1024, "temperature": 0},
                declares_text_result=True,
                allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
                retry_policy=RetryPolicy(max_attempts=2, repair_attempts=0),
                fallback_policy=FallbackPolicy(allowed=False),
                quality_tier=QualityTier.ECONOMY,
                latency_tier=LatencyTier.INTERACTIVE,
                max_input_tokens=4096,
            )
        }
    )
    storage = FakeObjectStorage(bucket="seam-test")
    fake_provider = FakeLLMProvider()
    store = FakeTransferStore()
    references = _InMemoryReferenceStore()
    service = AIService(
        task_registry=tasks,
        prompt_registry=bundle.prompts,
        model_registry=bundle.models,
        providers={"fake": fake_provider},
        attachment_resolver=StorageAttachmentResolver(storage),
        transfer_deployment=TransferDeploymentPolicy(
            inline_aggregate_threshold_bytes=_INLINE_THRESHOLD,
            max_large_attachment_bytes=50_000_000,
            enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD}),
        ),
        storage=storage,
        transfer_stores={"fake": store},
    )
    key = await _put_scratch_pdf(storage, size=_BIG_PDF_BYTES)
    fake_provider.fail_next_call(count=2, error=ProviderTimeoutError)

    with pytest.raises(ProviderTimeoutError):
        await service.execute(
            AIRequest(
                task="document.transfer-fail",
                storage_reference=key,
                organisation_id=_ORG_ID,
                user_id=_USER_ID,
                metadata={"question": "What is in this document?"},
            ),
            recorder=_ProviderUploadRecorder(),
            transfer_references=references,
        )
    # Both bounded attempts dispatched, and the retry reused the live
    # reference instead of staging a second copy (retry-only reuse).
    assert len(fake_provider.requests) == 2
    assert len(store.records) == 1
    assert len(references.records) == 1
    assert references.records[0].status is ExternalReferenceStatus.DELETED
    assert len(store.deleted) == 1
    assert await storage.head_object(key) is not None
