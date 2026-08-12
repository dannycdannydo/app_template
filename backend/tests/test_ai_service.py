"""AIService contract tests (v0.7 Scope §6.1, ADR-0017).

The service is exercised end-to-end with the in-memory registries (Scope §6.2
ships the checked-in YAML/JSON registries) and the deterministic
FakeLLMProvider: task → prompt → model resolution → provider dispatch →
structured-output validation → result with usage/cost/routing metadata. Every
error path returns the safe taxonomy (never unvalidated data, never provider
content), matching acceptance criterion §5.4.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, Field
from tests.ai_test_helpers import InMemoryPromptRegistry, InMemoryRegistries, metadata_attachment

from app.ai.attachments import (
    ALLOWED_ATTACHMENT_MIME_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    Attachment,
)
from app.ai.errors import (
    AIInputValidationError,
    ModelNotAvailableError,
    OutputSchemaError,
    OutputValidationError,
    PromptNotFoundError,
    ProviderResponseError,
    TaskNotFoundError,
    TransferExecutionUnavailableError,
    TransferModeUnavailableError,
)
from app.ai.persistence.port import AIRequestReservation, OrganisationAIPolicy
from app.ai.providers.base import LLMProvider, ProviderRequest, ProviderResponse
from app.ai.providers.fake import FakeLLMProvider
from app.ai.registry import (
    Capability,
    CapabilityCostModelRegistry,
    ModelDefinition,
    ModelRegistry,
    NonInlineModeLimit,
    PricingBasis,
    PromptDefinition,
    RetryPolicy,
    TaskDefinition,
)
from app.ai.schemas import AIRequest, TokenUsage
from app.ai.service import AIService
from app.ai.transfer import NON_INLINE_MIME_TYPES, TransferDeploymentPolicy, TransferMode


class ClassificationResult(BaseModel):
    """Pydantic output schema the demo task validates against."""

    task: str
    prompt_hash: str
    variables: dict[str, str]
    attachments: list[str] = Field(default_factory=list)


_ORG_ID = uuid4()
_USER_ID = uuid4()


def _request(**overrides: object) -> AIRequest:
    payload: dict[str, object] = {
        "task": "document.classify",
        "text": "classify this lease document",
        "organisation_id": _ORG_ID,
        "user_id": _USER_ID,
        "metadata": {"document_id": "doc-1"},
    }
    payload.update(overrides)
    return AIRequest.model_validate(payload)


def _resolver(path: str) -> type[BaseModel]:
    if path == "demo.ClassificationResult":
        return ClassificationResult
    raise OutputSchemaError(f"unknown schema {path!r}")


def _service(
    registries: InMemoryRegistries | None = None,
    *,
    provider: FakeLLMProvider | None = None,
    transfer_deployment: TransferDeploymentPolicy | None = None,
) -> tuple[AIService, FakeLLMProvider]:
    registries = registries or InMemoryRegistries.default()
    provider = provider or FakeLLMProvider()
    service = AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        provider=provider,
        schema_resolver=_resolver,
        transfer_deployment=transfer_deployment,
        allow_unmanaged_execution=True,
    )
    return service, provider


async def test_execute_returns_validated_structured_result() -> None:
    service, provider = _service()
    result = await service.execute(_request())

    assert result.routing.task == "document.classify"
    assert result.routing.provider == "fake"
    assert result.routing.model == "fake-model-document.classify"
    assert result.routing.prompt_name == "classify"
    assert result.routing.prompt_version == 1
    assert result.routing.fallback_used is False
    assert isinstance(result.output, ClassificationResult)
    assert result.output.task == "document.classify"
    assert result.output.variables == {"document_id": "doc-1"}
    assert result.usage.total_tokens > 0
    assert result.cost.amount >= 0
    assert result.completed_at.tzinfo is not None
    assert len(provider.requests) == 1
    # The adapter only ever saw the rendered prompt + approved metadata.
    request = provider.requests[0]
    assert "document.classify" in request.prompt
    assert "doc-1" in request.prompt
    assert request.output_schema == "demo.ClassificationResult"
    assert request.metadata == {"document_id": "doc-1"}


async def test_execute_is_deterministic_for_the_same_input() -> None:
    service, _ = _service()
    first = await service.execute(_request())
    second = await service.execute(_request())
    assert first.output == second.output
    assert first.usage == second.usage
    assert first.cost == second.cost
    assert first.routing == second.routing


async def test_routing_metadata_records_the_configured_region() -> None:
    """The adapter's configured region is recorded in routing metadata
    (v0.7 Scope §6.3 regional amendment) without leaking content."""
    provider = FakeLLMProvider()
    provider.region = "eu"
    service, _ = _service(provider=provider)
    result = await service.execute(_request())
    assert result.routing.region == "eu"
    assert result.routing.provider == "fake"


async def test_unknown_task_raises_task_not_found() -> None:
    service, _ = _service()
    with pytest.raises(TaskNotFoundError):
        await service.execute(_request(task="lease.extract_terms"))


async def test_unknown_prompt_version_raises_prompt_not_found() -> None:
    registries = InMemoryRegistries.default()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=99,
            output_schema="demo.ClassificationResult",
        )
    )
    service, _ = _service(registries)
    with pytest.raises(PromptNotFoundError):
        await service.execute(_request())


async def test_missing_input_variable_raises_input_validation_error() -> None:
    service, _ = _service()
    with pytest.raises(AIInputValidationError):
        await service.execute(_request(metadata={}))


async def test_task_input_form_must_match_prompt_variable() -> None:
    registries = InMemoryRegistries.default()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            input_variables=["text"],
            output_schema="demo.ClassificationResult",
        )
    )
    registries.prompts = InMemoryPromptRegistry(
        {
            ("classify", 1): PromptDefinition(
                name="classify",
                version=1,
                system_instructions="Classify input.",
                input_variables=["text"],
                user_template="{text}",
                output_contract="demo.ClassificationResult",
            )
        }
    )
    service, _ = _service(registries)
    with pytest.raises(AIInputValidationError, match="text input"):
        await service.execute(_request(text=None, storage_reference="private://document/1"))


async def test_disallowed_provider_raises_model_not_available() -> None:
    service, _ = _service()
    with pytest.raises(ModelNotAvailableError):
        await service.execute(_request(), allowed_providers=["openai"])


async def test_no_model_with_required_capability_raises_model_not_available() -> None:
    registries = InMemoryRegistries.default()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            required_capabilities=[Capability.VISION],
            output_schema="demo.ClassificationResult",
        )
    )
    service, _ = _service(registries)
    with pytest.raises(ModelNotAvailableError):
        await service.execute(_request())


async def test_unexpected_provider_failure_is_normalised() -> None:
    """A non-taxonomy exception from an adapter is wrapped in a safe,
    retryable=False ProviderResponseError — no provider detail leaks out."""
    provider = FakeLLMProvider()
    provider.fail_next_call(error=RuntimeError)
    service, _ = _service(provider=provider)
    with pytest.raises(ProviderResponseError) as exc_info:
        await service.execute(_request())
    assert exc_info.value.error_code == "provider_response_invalid"
    assert "raw SDK blowup" not in str(exc_info.value)


async def test_malformed_provider_json_fails_validation() -> None:
    """The provider returned garbage JSON: an OutputValidationError, never a
    success — acceptance criterion §5.4. The default task's retry policy
    allows one bounded repair then bounded task retries (Scope §6.4,
    ADR-0017), so the original, the repair and every retried dispatch must
    fail for the terminal error; exactly max_attempts dispatches plus the one
    repair happen and no repair storm occurs."""
    provider = FakeLLMProvider()
    provider.queue_responses(
        _canned_response(content="not json at all", structured=None),
        _canned_response(content="still not json", structured=None),
        _canned_response(content="still not json", structured=None),
        _canned_response(content="still not json", structured=None),
    )
    service, _ = _service(provider=provider)
    with pytest.raises(OutputValidationError):
        await service.execute(_request())
    assert len(provider.requests) == 4
    assert [request.repair for request in provider.requests] == [False, True, False, False]


async def test_schema_mismatch_fails_validation() -> None:
    """Valid JSON that does not match the schema is rejected — the repair
    request and the retried dispatches get the same malformed shape, so the
    terminal failure fires after exactly one repair."""
    provider = FakeLLMProvider()
    provider.queue_responses(
        _canned_response(
            content='{"task": "document.classify"}',
            structured={"task": "document.classify"},
        ),
        _canned_response(
            content='{"task": "document.classify"}',
            structured={"task": "document.classify"},
        ),
        _canned_response(
            content='{"task": "document.classify"}',
            structured={"task": "document.classify"},
        ),
        _canned_response(
            content='{"task": "document.classify"}',
            structured={"task": "document.classify"},
        ),
    )
    service, _ = _service(provider=provider)
    with pytest.raises(OutputValidationError):
        await service.execute(_request())
    assert len(provider.requests) == 4


async def test_unknown_output_schema_raises_output_schema_error() -> None:
    service, _ = _service()
    with pytest.raises(OutputSchemaError):
        await service.execute(_request(output_schema="missing.module.Schema"))


async def test_text_result_is_returned_only_when_declared() -> None:
    """Free text is only returned when the task explicitly declares it
    (Scope §6.4); a structured task without a schema fails validation instead.
    """
    registries = InMemoryRegistries.default()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            declares_text_result=True,
            output_schema=None,
        )
    )
    service, provider = _service(registries)
    result = await service.execute(_request())
    assert isinstance(result.output, str)
    assert len(result.output) > 0
    assert provider.requests[0].output_schema is None


async def test_cost_is_calculated_from_model_pricing_and_usage() -> None:
    """Cost = input_price * input_tokens + output_price * output_tokens,
    scaled per million tokens, in the model's currency (Scope §6.2)."""
    registries = InMemoryRegistries.default()
    registries.models = _PricedModelRegistry()
    service, _ = _service(registries)
    result = await service.execute(_request())
    expected_input_cost = Decimal("1.00") * Decimal(result.usage.input_tokens) / Decimal(1_000_000)
    expected_output_cost = (
        Decimal("2.00") * Decimal(result.usage.output_tokens) / Decimal(1_000_000)
    )
    assert result.cost.amount == pytest.approx(expected_input_cost + expected_output_cost)
    assert result.cost.currency == "USD"


async def test_request_requires_exactly_one_input() -> None:
    with pytest.raises(ValueError):
        _request(text=None)
    with pytest.raises(ValueError):
        _request(text="a", storage_reference="s3://x")


async def test_request_bounds_metadata() -> None:
    with pytest.raises(ValueError):
        _request(metadata={f"key-{i}": "v" for i in range(20)})


def _canned_response(*, content: str, structured: dict[str, object] | None) -> ProviderResponse:
    return ProviderResponse(
        model="fake-model-document.classify",
        content=content,
        structured=structured,
        usage=TokenUsage(input_tokens=10, output_tokens=10),
        latency_ms=1.0,
        finish_reason="stop",
    )


class _PricedModelRegistry(ModelRegistry):
    """Model registry whose pricing basis is fixed by the test."""

    def __init__(self) -> None:
        self._model = ModelDefinition(
            id="fake.document-classifier",
            provider="fake",
            model="fake-model-document.classify",
            capabilities=[Capability.STRUCTURED_OUTPUT],
            context_window=128_000,
            supported_parameters=[],
            pricing=PricingBasis(
                currency="USD",
                input_price_per_million_tokens=Decimal("1.00"),
                output_price_per_million_tokens=Decimal("2.00"),
                effective_date=date(2026, 1, 1),
                owner="tests",
            ),
        )

    def get(self, provider: str, model: str) -> ModelDefinition:
        if (provider, model) != ("fake", self._model.model):
            raise KeyError(model)
        return self._model

    def all(self) -> list[ModelDefinition]:
        return [self._model]

    def resolve(
        self,
        task: TaskDefinition,
        *,
        allowed_providers: list[str] | None = None,
    ) -> ModelDefinition:
        if allowed_providers is not None and self._model.provider not in allowed_providers:
            raise ValueError("provider not allowed")
        return self._model


def test_token_usage_total_property() -> None:
    usage = TokenUsage(input_tokens=10, output_tokens=5)
    assert usage.total_tokens == 15


# --- v0.7 attachment amendment: service-side rejection before dispatch ---


def _attachment(
    *,
    name: str = "lease.pdf",
    mime_type: str = "application/pdf",
    content: bytes = b"%PDF-1.7 fixture",
) -> Attachment:
    return Attachment(display_name=name, mime_type=mime_type, content=content)


class _DocumentModelRegistry(ModelRegistry):
    """Model registry with one fake model that declares document support."""

    def __init__(self, *, capabilities: list[Capability] | None = None) -> None:
        self._capabilities = capabilities or [
            Capability.STRUCTURED_OUTPUT,
            Capability.DOCUMENTS,
        ]
        self._model = ModelDefinition(
            id="fake.document-classifier",
            provider="fake",
            model="fake-model-document.classify",
            capabilities=self._capabilities,
            context_window=128_000,
            supported_parameters=[],
            max_attachment_bytes=MAX_ATTACHMENT_BYTES,
            max_total_attachment_bytes=MAX_TOTAL_ATTACHMENT_BYTES,
            attachment_mime_types=sorted(ALLOWED_ATTACHMENT_MIME_TYPES),
            pricing=PricingBasis(
                currency="USD",
                input_price_per_million_tokens=Decimal("1.00"),
                output_price_per_million_tokens=Decimal("2.00"),
                effective_date=date(2026, 1, 1),
                owner="tests",
            ),
        )

    def get(self, provider: str, model: str) -> ModelDefinition:
        if (provider, model) != ("fake", self._model.model):
            raise KeyError(model)
        return self._model

    def all(self) -> list[ModelDefinition]:
        return [self._model]

    def resolve(
        self,
        task: TaskDefinition,
        *,
        allowed_providers: list[str] | None = None,
    ) -> ModelDefinition:
        return self._model


class _NoDocumentsProvider(LLMProvider):
    """A provider that truthfully cannot carry documents."""

    provider_id = "fake"
    supports_structured_output = True
    supports_documents = False

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError  # pragma: no cover - never reached


async def test_execute_routes_attachments_to_document_model_and_passes_them() -> None:
    registries = InMemoryRegistries.default()
    registries.models = _DocumentModelRegistry()
    service, provider = _service(registries)
    attachment = _attachment()

    result = await service.execute(_request(), attachments=[attachment])

    assert result.routing.model == "fake-model-document.classify"
    assert result.output.attachments == [attachment.sha256_digest]
    assert len(provider.requests) == 1
    assert provider.requests[0].attachments == [attachment]


async def test_execute_rejects_attachments_when_provider_lacks_document_support() -> None:
    """The model may declare the documents capability, but the configured
    adapter must truthfully support it too — otherwise fail before dispatch."""
    registries = InMemoryRegistries.default()
    registries.models = _DocumentModelRegistry()
    service = AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        provider=_NoDocumentsProvider(),
        schema_resolver=_resolver,
        allow_unmanaged_execution=True,
    )

    with pytest.raises(ModelNotAvailableError, match="does not support document"):
        await service.execute(_request(), attachments=[_attachment()])


async def test_execute_rejects_attachments_when_no_model_can_carry_them() -> None:
    service, _ = _service()  # default registry has no documents-capable model
    with pytest.raises(ModelNotAvailableError, match="no model satisfies"):
        await service.execute(_request(), attachments=[_attachment()])


async def test_execute_rejects_oversized_attachment_set_before_dispatch() -> None:
    registries = InMemoryRegistries.default()
    registries.models = _DocumentModelRegistry()
    service, provider = _service(registries)
    oversized = [
        _attachment(name=f"part-{i}.txt", content=b"x" * MAX_ATTACHMENT_BYTES) for i in range(3)
    ]

    with pytest.raises(AIInputValidationError, match="combined"):
        await service.execute(_request(), attachments=oversized)
    assert provider.requests == []  # nothing was dispatched


async def test_execute_rejects_unallowlisted_attachment_mime_before_dispatch() -> None:
    registries = InMemoryRegistries.default()
    registries.models = _DocumentModelRegistry()
    service, provider = _service(registries)
    with pytest.raises(ValueError, match="MIME"):
        _attachment(name="payload.exe", mime_type="application/octet-stream", content=b"MZ...")

    # A valid set reaches the adapter; an invalid one never does.
    result = await service.execute(_request(), attachments=[_attachment()])
    assert result.routing.model == "fake-model-document.classify"
    assert provider.requests[0].attachments == [_attachment()]


async def test_execute_rejects_image_attachments_when_model_lacks_vision() -> None:
    """An image must not route to a documents-only model: the service rejects
    the set before dispatch, so the adapter never sees the request."""
    registries = InMemoryRegistries.default()
    registries.models = _DocumentModelRegistry()
    service, provider = _service(registries)
    image = _attachment(name="scan.png", mime_type="image/png", content=b"\x89PNG fixture")

    with pytest.raises(ModelNotAvailableError, match="no model satisfies"):
        await service.execute(_request(), attachments=[image])
    assert provider.requests == []  # nothing was dispatched


# --- v0.8 §6.2 transfer gate: pre-dispatch denial above the inline threshold ---


class _NonInlineDocumentModelRegistry(_DocumentModelRegistry):
    """A document model that additionally declares the provider-upload mode."""

    def __init__(self) -> None:
        super().__init__()
        self._model = ModelDefinition(
            id="fake.document-classifier",
            provider="fake",
            model="fake-model-document.classify",
            capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            context_window=128_000,
            supported_parameters=[],
            max_attachment_bytes=MAX_ATTACHMENT_BYTES,
            max_total_attachment_bytes=MAX_TOTAL_ATTACHMENT_BYTES,
            attachment_mime_types=sorted(ALLOWED_ATTACHMENT_MIME_TYPES),
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            transfer_mode_limits={
                TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                    mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=50_000_000
                )
            },
            pricing=PricingBasis(
                currency="USD",
                input_price_per_million_tokens=Decimal("1.00"),
                output_price_per_million_tokens=Decimal("2.00"),
                effective_date=date(2026, 1, 1),
                owner="tests",
            ),
        )


class _PermissiveRecorder:
    """Minimal :class:`AIPersistencePort` whose policy permits a non-inline mode."""

    def __init__(self) -> None:
        self.policy = OrganisationAIPolicy(
            enabled=True,
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
        )

    async def load_policy(self, *, organisation_id: object) -> OrganisationAIPolicy:
        return self.policy

    async def reserve(self, **kwargs: object) -> AIRequestReservation:
        return AIRequestReservation(row_id=uuid4(), created=True)

    async def record_attempt(self, **kwargs: object) -> UUID:
        return uuid4()

    async def settle(self, **kwargs: object) -> None:
        return None


def _doc_model(
    model_id: str,
    *,
    priority: int = 100,
    attachment_mime_types: list[str] | None = None,
    allowed_transfer_modes: list[TransferMode] | None = None,
    transfer_mode_limits: dict[TransferMode, NonInlineModeLimit] | None = None,
) -> ModelDefinition:
    """A fake document model with the reviewed inline ceilings and optional
    v0.8 non-inline declarations, for multi-model routing tests."""
    return ModelDefinition(
        id=model_id,
        provider="fake",
        model=f"fake-model-{model_id}.classify",
        capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
        context_window=128_000,
        supported_parameters=["max_tokens", "temperature"],
        priority=priority,
        max_attachment_bytes=MAX_ATTACHMENT_BYTES,
        max_total_attachment_bytes=MAX_TOTAL_ATTACHMENT_BYTES,
        attachment_mime_types=(
            attachment_mime_types
            if attachment_mime_types is not None
            else sorted(ALLOWED_ATTACHMENT_MIME_TYPES)
        ),
        allowed_transfer_modes=allowed_transfer_modes or [TransferMode.INLINE],
        transfer_mode_limits=transfer_mode_limits or {},
        pricing=PricingBasis(
            currency="USD",
            input_price_per_million_tokens=Decimal("1.00"),
            output_price_per_million_tokens=Decimal("2.00"),
            effective_date=date(2026, 1, 1),
            owner="tests",
        ),
    )


async def test_execute_denies_attachments_above_inline_threshold_with_default_policy() -> None:
    """Default-deny: with an inline-only organisation policy (and an inline-only
    model declaration), a set above the 5,000,000-byte aggregate threshold fails
    before any external transfer (Scope §5.2, §6.2)."""
    registries = InMemoryRegistries.default()
    registries.models = _DocumentModelRegistry()
    service, provider = _service(registries)
    # One PDF just above the aggregate threshold (still under the per-file
    # ceiling) cannot ride a non-inline mode the policy never allows.
    large_pdf = _attachment(name="lease.pdf", content=b"x" * 5_100_000)

    with pytest.raises(TransferModeUnavailableError, match="no permitted"):
        await service.execute(_request(), attachments=[large_pdf])
    assert provider.requests == []  # nothing was dispatched


async def test_execute_denies_non_inline_selection_until_execution_seam_lands() -> None:
    """A policy/task/model/deployment intersection that selects a non-inline
    mode fails closed with the execution-unavailable error until §6.3 wires the
    transfer seam — never by silently riding the inline path above the
    threshold."""
    registries = InMemoryRegistries.default()
    registries.models = _NonInlineDocumentModelRegistry()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            input_variables=["document_id"],
            required_capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            output_schema="demo.ClassificationResult",
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
        )
    )
    service, provider = _service(
        registries,
        transfer_deployment=TransferDeploymentPolicy(
            enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD})
        ),
    )

    large_pdf = _attachment(name="lease.pdf", content=b"x" * 5_100_000)
    with pytest.raises(TransferExecutionUnavailableError, match="not executable"):
        await service.execute(_request(), attachments=[large_pdf], recorder=_PermissiveRecorder())
    assert provider.requests == []


async def test_execute_denies_multiple_pdfs_above_the_threshold() -> None:
    """v0.8 Scope §2.1 decision 3/§5.3: the non-inline path carries exactly one
    PDF, so multiple large files fail before any external transfer."""
    registries = InMemoryRegistries.default()
    registries.models = _NonInlineDocumentModelRegistry()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            input_variables=["document_id"],
            required_capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            output_schema="demo.ClassificationResult",
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
        )
    )
    service, provider = _service(
        registries,
        transfer_deployment=TransferDeploymentPolicy(
            enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD})
        ),
    )
    parts = [_attachment(name=f"part-{i}.pdf", content=b"x" * 3_000_000) for i in range(2)]

    with pytest.raises(TransferModeUnavailableError, match="exactly one application/pdf"):
        await service.execute(_request(), attachments=parts, recorder=_PermissiveRecorder())
    assert provider.requests == []  # nothing was dispatched


async def test_execute_denies_non_pdf_input_above_the_threshold() -> None:
    """A large non-PDF attachment above the threshold has no eligible non-inline
    mode and fails before dispatch (Scope §2.1/§5.3)."""
    registries = InMemoryRegistries.default()
    registries.models = _NonInlineDocumentModelRegistry()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            input_variables=["document_id"],
            required_capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            output_schema="demo.ClassificationResult",
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
        )
    )
    service, provider = _service(
        registries,
        transfer_deployment=TransferDeploymentPolicy(
            enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD})
        ),
    )
    large_csv = _attachment(name="ledger.csv", mime_type="text/csv", content=b"x" * 5_100_000)

    with pytest.raises(TransferModeUnavailableError, match="exactly one application/pdf"):
        await service.execute(_request(), attachments=[large_csv], recorder=_PermissiveRecorder())
    assert provider.requests == []  # nothing was dispatched


class _TightCeilingDocumentModelRegistry(_NonInlineDocumentModelRegistry):
    """A document model whose provider-upload mode ceiling sits just above the
    inline threshold, so a slightly larger single PDF has no eligible mode."""

    def __init__(self) -> None:
        super().__init__()
        self._model = ModelDefinition(
            id="fake.document-classifier",
            provider="fake",
            model="fake-model-document.classify",
            capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            context_window=128_000,
            supported_parameters=[],
            max_attachment_bytes=MAX_ATTACHMENT_BYTES,
            max_total_attachment_bytes=MAX_TOTAL_ATTACHMENT_BYTES,
            attachment_mime_types=sorted(ALLOWED_ATTACHMENT_MIME_TYPES),
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            transfer_mode_limits={
                TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                    mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=5_100_000
                )
            },
            pricing=PricingBasis(
                currency="USD",
                input_price_per_million_tokens=Decimal("1.00"),
                output_price_per_million_tokens=Decimal("2.00"),
                effective_date=date(2026, 1, 1),
                owner="tests",
            ),
        )


async def test_execute_denies_request_above_a_model_specific_mode_ceiling() -> None:
    """The routed model's per-mode byte ceiling gates selection: a single PDF
    above it fails before any external transfer (Scope §2.2, lowest ceiling
    wins)."""
    registries = InMemoryRegistries.default()
    registries.models = _TightCeilingDocumentModelRegistry()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            input_variables=["document_id"],
            required_capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            output_schema="demo.ClassificationResult",
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
        )
    )
    service, provider = _service(
        registries,
        transfer_deployment=TransferDeploymentPolicy(
            enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD})
        ),
    )
    # 5.2 MB single PDF: above the model's 5,100,000-byte provider-upload
    # ceiling, still under the 5 MiB per-file template ceiling (5,242,880
    # bytes), so this set reaches the selector through the legacy router.
    large_pdf = _attachment(name="lease.pdf", content=b"x" * 5_200_000)

    with pytest.raises(TransferModeUnavailableError, match="no permitted"):
        await service.execute(_request(), attachments=[large_pdf], recorder=_PermissiveRecorder())
    assert provider.requests == []  # nothing was dispatched


class _NonInlineOnlyModelRegistry(_DocumentModelRegistry):
    """A document model that declares ``provider_upload`` but not inline."""

    def __init__(self) -> None:
        super().__init__()
        self._model = ModelDefinition(
            id="fake.document-classifier",
            provider="fake",
            model="fake-model-document.classify",
            capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            context_window=128_000,
            supported_parameters=[],
            max_attachment_bytes=MAX_ATTACHMENT_BYTES,
            max_total_attachment_bytes=MAX_TOTAL_ATTACHMENT_BYTES,
            attachment_mime_types=sorted(ALLOWED_ATTACHMENT_MIME_TYPES),
            allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
            transfer_mode_limits={
                TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                    mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=50_000_000
                )
            },
            pricing=PricingBasis(
                currency="USD",
                input_price_per_million_tokens=Decimal("1.00"),
                output_price_per_million_tokens=Decimal("2.00"),
                effective_date=date(2026, 1, 1),
                owner="tests",
            ),
        )


async def test_execute_never_dispatches_inline_when_the_intersection_excludes_it() -> None:
    """Scope §5.2/§6.2: the service runs the full policy intersection for
    *every* attachment set — there is no inline shortcut below the aggregate
    threshold, so a task/model whose declarations exclude inline can never
    dispatch inline, even for a tiny file."""
    registries = InMemoryRegistries.default()
    registries.models = _NonInlineOnlyModelRegistry()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            input_variables=["document_id"],
            required_capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            output_schema="demo.ClassificationResult",
            allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
            retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
        )
    )
    service, provider = _service(
        registries,
        transfer_deployment=TransferDeploymentPolicy(
            enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD})
        ),
    )
    small_pdf = _attachment(name="lease.pdf", content=b"%PDF-1.7 tiny")

    with pytest.raises(TransferModeUnavailableError, match="no permitted"):
        await service.execute(_request(), attachments=[small_pdf], recorder=_PermissiveRecorder())
    assert provider.requests == []  # nothing was dispatched


async def test_execute_fails_inline_only_intersection_above_a_lowered_threshold() -> None:
    """The configured deployment threshold is authoritative (Scope §2.2/§5.2):
    above a lowered threshold an inline-only intersection fails closed instead
    of falling back to inline whenever the provider's fixed inline contract
    ceiling would still fit."""
    registries = InMemoryRegistries.default()
    registries.models = _DocumentModelRegistry()  # task and model declare inline only
    service, provider = _service(
        registries,
        transfer_deployment=TransferDeploymentPolicy(inline_aggregate_threshold_bytes=1_000_000),
    )
    # 2,000,000 bytes: above the lowered 1,000,000-byte deployment threshold,
    # still within the provider's 5,000,000-byte inline contract ceiling.
    above_lowered = _attachment(name="lease.pdf", content=b"x" * 2_000_000)

    with pytest.raises(TransferModeUnavailableError, match="no permitted"):
        await service.execute(
            _request(), attachments=[above_lowered], recorder=_PermissiveRecorder()
        )
    assert provider.requests == []  # nothing was dispatched


async def test_execute_routes_large_pdf_to_non_inline_model_before_execution_seam() -> None:
    """v0.8 Scope §6.2: routing is transfer-mode-aware — a PDF above the
    template inline per-file ceiling (5 MiB) still routes to a model whose
    declared provider-upload ceiling fits, so the request reaches the transfer
    gate, which then fails closed until the §6.3 execution seam lands. The
    carrier is metadata (size + MIME); no inline bytes are allocated."""
    registries = InMemoryRegistries.default()
    registries.models = _NonInlineDocumentModelRegistry()
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            input_variables=["document_id"],
            required_capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            output_schema="demo.ClassificationResult",
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
        )
    )
    service, provider = _service(
        registries,
        transfer_deployment=TransferDeploymentPolicy(
            enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD})
        ),
    )
    # 6,000,000 bytes: above MAX_ATTACHMENT_BYTES (5,242,880), below the 10 MiB
    # combined ceiling — a metadata carrier, never allocated inline bytes.
    large_pdf = metadata_attachment(size_bytes=6_000_000)

    with pytest.raises(TransferExecutionUnavailableError, match="not executable"):
        await service.execute(_request(), attachments=[large_pdf], recorder=_PermissiveRecorder())
    assert provider.requests == []  # routed, then denied before any dispatch


async def test_execute_picks_a_model_with_an_eligible_mode_over_a_higher_priority_incompatible_one() -> (
    None
):
    """v0.8 Scope §6.2: routing and mode selection are one coherent decision.
    A 6,000,000-byte transient PDF routes past the higher-priority model whose
    only non-inline mode (managed_signed_url) serves retained sources to the
    lower-priority provider_upload model, so the request reaches the transfer
    gate with a compatible model and fails closed at the §6.3 execution seam —
    never a no-eligible-mode denial while a valid model exists."""
    registries = InMemoryRegistries.default()
    registries.models = CapabilityCostModelRegistry(
        [
            _doc_model(
                "managed-url-only",
                priority=0,
                allowed_transfer_modes=[
                    TransferMode.INLINE,
                    TransferMode.MANAGED_SIGNED_URL,
                ],
                transfer_mode_limits={
                    TransferMode.MANAGED_SIGNED_URL: NonInlineModeLimit(
                        mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=50_000_000
                    )
                },
            ),
            _doc_model(
                "provider-upload",
                priority=100,
                allowed_transfer_modes=[
                    TransferMode.INLINE,
                    TransferMode.PROVIDER_UPLOAD,
                ],
                transfer_mode_limits={
                    TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                        mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=50_000_000
                    )
                },
            ),
        ]
    )
    registries.tasks.register(
        TaskDefinition(
            name="document.classify",
            prompt_name="classify",
            prompt_version=1,
            input_variables=["document_id"],
            required_capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
            output_schema="demo.ClassificationResult",
            allowed_transfer_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
        )
    )
    service, provider = _service(
        registries,
        transfer_deployment=TransferDeploymentPolicy(
            enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD})
        ),
    )
    large_pdf = metadata_attachment(size_bytes=6_000_000)

    with pytest.raises(TransferExecutionUnavailableError, match="not executable"):
        await service.execute(_request(), attachments=[large_pdf], recorder=_PermissiveRecorder())
    assert provider.requests == []  # the compatible model was routed and denied at the seam


async def test_execute_inline_dispatch_never_violates_a_models_inline_mime_declaration() -> None:
    """v0.8 Scope §6.2: a small PDF below the threshold routes past the
    higher-priority model whose inline MIME set excludes PDF (it only accepts
    PDF via provider_upload) to the lower-priority model whose inline
    declarations cover it, and the inline dispatch goes to that model — never
    a dispatch that violates a model's inline MIME declaration."""
    registries = InMemoryRegistries.default()
    registries.models = CapabilityCostModelRegistry(
        [
            _doc_model(
                "text-only-inline",
                priority=0,
                attachment_mime_types=["text/plain"],
                allowed_transfer_modes=[
                    TransferMode.INLINE,
                    TransferMode.PROVIDER_UPLOAD,
                ],
                transfer_mode_limits={
                    TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                        mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=50_000_000
                    )
                },
            ),
            _doc_model("pdf-inline", priority=100),
        ]
    )
    service, provider = _service(registries)
    small_pdf = _attachment(name="lease.pdf", content=b"%PDF-1.7 tiny")

    result = await service.execute(
        _request(), attachments=[small_pdf], recorder=_PermissiveRecorder()
    )
    assert result.routing.model == "fake-model-pdf-inline.classify"
    assert provider.requests[0].attachments == [small_pdf]


async def test_execute_denies_inline_when_the_only_model_cannot_carry_the_set_inline() -> None:
    """v0.8 Scope §6.2: with only a model whose inline MIME set excludes the
    PDF, a small below-threshold request fails closed before dispatch — the
    non-inline declaration that accepts PDF must never turn into an inline
    dispatch that violates the model's inline MIME declaration."""
    registries = InMemoryRegistries.default()
    registries.models = CapabilityCostModelRegistry(
        [
            _doc_model(
                "text-only-inline",
                attachment_mime_types=["text/plain"],
                allowed_transfer_modes=[
                    TransferMode.INLINE,
                    TransferMode.PROVIDER_UPLOAD,
                ],
                transfer_mode_limits={
                    TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                        mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=50_000_000
                    )
                },
            )
        ]
    )
    service, provider = _service(registries)
    small_pdf = _attachment(name="lease.pdf", content=b"%PDF-1.7 tiny")

    with pytest.raises(TransferModeUnavailableError, match="no permitted"):
        await service.execute(_request(), attachments=[small_pdf], recorder=_PermissiveRecorder())
    assert provider.requests == []  # nothing was dispatched


async def test_execute_routes_image_attachments_to_vision_capable_model() -> None:
    """A model declaring both ``documents`` and ``vision`` carries image
    attachments; the adapter receives the validated, still-immutable object."""
    registries = InMemoryRegistries.default()
    registries.models = _DocumentModelRegistry(
        capabilities=[
            Capability.STRUCTURED_OUTPUT,
            Capability.DOCUMENTS,
            Capability.VISION,
        ]
    )
    service, provider = _service(registries)
    image = _attachment(name="scan.png", mime_type="image/png", content=b"\x89PNG fixture")

    result = await service.execute(_request(), attachments=[image])

    assert result.routing.model == "fake-model-document.classify"
    assert result.output.attachments == [image.sha256_digest]
    assert len(provider.requests) == 1
    assert provider.requests[0].attachments == [image]
