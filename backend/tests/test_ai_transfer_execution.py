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
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.ai.attachments import MAX_ATTACHMENT_BYTES
from app.ai.errors import (
    AIInputValidationError,
    TransferExecutionUnavailableError,
    TransferModeUnavailableError,
)
from app.ai.persistence.port import AIRequestReservation, OrganisationAIPolicy
from app.ai.providers.fake import FakeLLMProvider
from app.ai.registry import load_registry_bundle
from app.ai.schemas import AIRequest
from app.ai.service import AIService
from app.ai.staging import ExternalFileReference, ExternalReferenceStatus, FakeTransferStore
from app.ai.storage_resolver import StorageAttachmentResolver
from app.ai.transfer import SourceLifecycle, TransferDeploymentPolicy, TransferMode
from app.storage.fake import FakeObjectStorage

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
        candidate = self._by_key.get(
            f"{provider_id}|{mode.value}|{organisation_id}|{logical_request_id}|{source_digest}|{region}"
        )
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

    async def resolve_for_deletion(
        self, *, organisation_id: UUID, idempotency_key: str
    ) -> ExternalFileReference | None:
        return self._by_key.get(idempotency_key)

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
    # The staged reference was recorded and its AI-owned copy deleted after
    # terminal success; the source object still exists.
    assert len(store.records) == 1
    assert len(store.deleted) == 1
    assert store.deleted[0].idempotency_key == store.records[0].idempotency_key
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
