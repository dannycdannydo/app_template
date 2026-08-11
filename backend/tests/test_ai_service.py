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
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field
from tests.ai_test_helpers import InMemoryPromptRegistry, InMemoryRegistries

from app.ai.attachments import MAX_ATTACHMENT_BYTES, MAX_TOTAL_ATTACHMENT_BYTES, Attachment
from app.ai.errors import (
    AIInputValidationError,
    ModelNotAvailableError,
    OutputSchemaError,
    OutputValidationError,
    PromptNotFoundError,
    ProviderResponseError,
    TaskNotFoundError,
)
from app.ai.providers.base import LLMProvider, ProviderRequest, ProviderResponse
from app.ai.providers.fake import FakeLLMProvider
from app.ai.registry import (
    Capability,
    ModelDefinition,
    ModelRegistry,
    PricingBasis,
    PromptDefinition,
    TaskDefinition,
)
from app.ai.schemas import AIRequest, TokenUsage
from app.ai.service import AIService


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
) -> tuple[AIService, FakeLLMProvider]:
    registries = registries or InMemoryRegistries.default()
    provider = provider or FakeLLMProvider()
    service = AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        provider=provider,
        schema_resolver=_resolver,
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
    success — acceptance criterion §5.4."""
    provider = FakeLLMProvider()
    provider.set_next_response(_canned_response(content="not json at all", structured=None))
    service, _ = _service(provider=provider)
    with pytest.raises(OutputValidationError):
        await service.execute(_request())


async def test_schema_mismatch_fails_validation() -> None:
    """Valid JSON that does not match the schema is rejected."""
    provider = FakeLLMProvider()
    provider.set_next_response(
        _canned_response(
            content='{"task": "document.classify"}',
            structured={"task": "document.classify"},
        )
    )
    service, _ = _service(provider=provider)
    with pytest.raises(OutputValidationError):
        await service.execute(_request())


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
