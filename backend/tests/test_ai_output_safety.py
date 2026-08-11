"""AI structured-output, retry and safety tests (v0.7 Scope §6.4, ADR-0017).

Scope §6.4 items covered here:

- native structured-output path (the service supplies the JSON Schema it
  generated from the Pydantic model) plus the JSON-mode prompt-contract
  fallback, with text results only when the task declares them;
- a bounded repair attempt separated from bounded transient retries and from
  permanent validation/policy failures — no retry storm, no unbounded cost;
- input normalisation: redaction hook, max-size/context checks and private
  storage reference resolution into validated bounded attachments with
  SHA-256 digests, propagating only approved metadata to adapters;
- unit/integration coverage for successful validation, malformed output,
  repair success/failure, timeout/rate-limit translation, idempotency and no
  content leakage.

The default suite stays provider-free: the deterministic FakeLLMProvider is
the adapter and FakeObjectStorage backs the storage-reference resolver.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field
from tests.ai_test_helpers import InMemoryPromptRegistry, InMemoryRegistries, InMemoryTaskRegistry

from app.ai.attachments import (
    ALLOWED_ATTACHMENT_MIME_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    Attachment,
)
from app.ai.errors import (
    AIInputValidationError,
    ModelNotAvailableError,
    OutputValidationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.ai.providers.base import ProviderRequest, ProviderResponse
from app.ai.providers.fake import FakeLLMProvider
from app.ai.providers.openai_compatible import OpenAICompatibleAdapter
from app.ai.registry import (
    Capability,
    CapabilityCostModelRegistry,
    FallbackPolicy,
    ModelDefinition,
    ModelRegistry,
    PricingBasis,
    PromptDefinition,
    RetryPolicy,
    RoutingDecision,
    TaskDefinition,
)
from app.ai.schemas import AIRequest, AIResult, TokenUsage
from app.ai.service import MAX_REPAIR_CONTEXT_LENGTH, AIService, RepairNotPossibleError
from app.ai.storage_resolver import StorageAttachmentResolver
from app.storage.fake import FakeObjectStorage
from app.storage.types import ObjectInfo

_ORG_ID = uuid4()
_USER_ID = uuid4()


class ClassificationResult(BaseModel):
    """Pydantic output schema the demo task validates against."""

    task: str
    prompt_hash: str
    variables: dict[str, str]
    attachments: list[str] = Field(default_factory=list)


def _resolver(path: str) -> type[BaseModel]:
    if path == "demo.ClassificationResult":
        return ClassificationResult
    raise ValueError(f"unknown schema {path!r}")


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


def _storage_request(*, reference: str | None = None) -> AIRequest:
    return AIRequest(
        task="document.classify",
        storage_reference=reference or f"organisations/{_ORG_ID}/scratch/doc-1/original.pdf",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        metadata={"document_id": "doc-1"},
    )


def _service(
    registries: InMemoryRegistries | None = None,
    *,
    provider: FakeLLMProvider | None = None,
    attachment_resolver: Any = None,
    redactor: Any = None,
) -> tuple[AIService, FakeLLMProvider]:
    registries = registries or InMemoryRegistries.default()
    provider = provider or FakeLLMProvider()
    service = AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        provider=provider,
        schema_resolver=_resolver,
        attachment_resolver=attachment_resolver,
        redactor=redactor,
    )
    return service, provider


def _canned(
    content: str,
    *,
    structured: dict[str, object] | None = None,
    usage: TokenUsage | None = None,
) -> ProviderResponse:
    return ProviderResponse(
        model="fake-model-document.classify",
        content=content,
        structured=structured,
        usage=usage or TokenUsage(input_tokens=10, output_tokens=10),
        latency_ms=1.0,
        finish_reason="stop",
    )


def _text_task_registries() -> InMemoryRegistries:
    """Registries whose demo task/prompt declare a ``text`` input variable."""
    registries = InMemoryRegistries.default()
    registries.tasks = InMemoryTaskRegistry(
        {
            "document.classify": TaskDefinition(
                name="document.classify",
                prompt_name="classify",
                prompt_version=1,
                input_variables=["text"],
                required_capabilities=[Capability.STRUCTURED_OUTPUT],
                output_schema="demo.ClassificationResult",
                retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
            )
        }
    )
    registries.prompts = InMemoryPromptRegistry(
        {
            ("classify", 1): PromptDefinition(
                name="classify",
                version=1,
                system_instructions="Classify input.",
                input_variables=["text"],
                user_template="Document: {text}",
                output_contract="demo.ClassificationResult",
            )
        }
    )
    return registries


def _no_repair_task_registries() -> InMemoryRegistries:
    """Registries whose task disables the repair path entirely."""
    registries = InMemoryRegistries.default()
    registries.tasks = InMemoryTaskRegistry(
        {
            "document.classify": TaskDefinition(
                name="document.classify",
                prompt_name="classify",
                prompt_version=1,
                input_variables=["document_id"],
                required_capabilities=[Capability.STRUCTURED_OUTPUT],
                output_schema="demo.ClassificationResult",
                retry_policy=RetryPolicy(max_attempts=2, repair_attempts=0),
            )
        }
    )
    return registries


# --- Item 1: native structured-output path and JSON-mode fallback ---


async def test_service_supplies_the_generated_json_schema_to_the_adapter() -> None:
    """The service generates the JSON Schema from the task's Pydantic model and
    passes it with the schema identifier so adapters can request native
    structured output (Scope §6.4)."""
    service, provider = _service()
    result = await service.execute(_request())
    assert isinstance(result.output, ClassificationResult)
    request = provider.requests[0]
    assert request.output_schema == "demo.ClassificationResult"
    assert request.output_json_schema is not None
    assert request.output_json_schema["type"] == "object"
    assert "task" in request.output_json_schema["properties"]


async def test_text_result_stays_declared_only() -> None:
    """A text task receives no output schema and its text is returned; the
    repair/native paths are not involved."""
    registries = InMemoryRegistries.default()
    registries.tasks = InMemoryTaskRegistry(
        {
            "document.classify": TaskDefinition(
                name="document.classify",
                prompt_name="classify",
                prompt_version=1,
                input_variables=["document_id"],
                declares_text_result=True,
                output_schema=None,
            )
        }
    )
    service, provider = _service(registries)
    result = await service.execute(_request())
    assert isinstance(result.output, str) and result.output
    assert provider.requests[0].output_schema is None
    assert provider.requests[0].output_json_schema is None


# --- Item 2: bounded repair and bounded transient retries ---


async def test_malformed_output_triggers_one_bounded_repair_then_success() -> None:
    """Malformed output → one repair request (same model, repair flag set,
    repair instruction appended) → validated success. Exactly two calls."""
    provider = FakeLLMProvider()
    provider.set_next_response(_canned("not json at all"))
    service, _ = _service(provider=provider)
    result = await service.execute(_request())

    assert isinstance(result.output, ClassificationResult)
    assert len(provider.requests) == 2
    first, repair = provider.requests
    assert first.repair is False
    assert repair.repair is True
    assert repair.model == first.model
    assert repair.output_schema == first.output_schema
    assert repair.output_json_schema == first.output_json_schema
    assert "not valid structured output" in repair.prompt
    assert "not json at all" in repair.prompt
    assert repair.prompt.startswith(first.prompt)


async def test_repair_context_is_truncated_to_bound_cost() -> None:
    """The previous invalid output is truncated before it is echoed back, so a
    repair can never amplify an output into an unbounded request."""
    provider = FakeLLMProvider()
    huge = "x" * (MAX_REPAIR_CONTEXT_LENGTH * 4)
    provider.set_next_response(_canned(huge))
    service, _ = _service(provider=provider)
    await service.execute(_request())
    repair_prompt = provider.requests[1].prompt
    assert len(repair_prompt) <= len(provider.requests[0].prompt) + MAX_REPAIR_CONTEXT_LENGTH + 512
    assert huge not in repair_prompt


async def test_repair_failure_consumes_one_bounded_task_retry_then_is_terminal() -> None:
    """The single repair budget is consumed by the first failed repair; further
    malformed output consumes the bounded malformed-output task retries, and the
    final attempt raises OutputValidationError — exactly one repair request and
    no repair storm (ADR-0017: one bounded repair *then* bounded task retries)."""
    provider = FakeLLMProvider()
    provider.queue_responses(
        _canned("not json"),
        _canned("still not json"),
        _canned("still not json"),
        _canned("still not json"),
    )
    service, _ = _service(provider=provider)
    with pytest.raises(OutputValidationError):
        await service.execute(_request())
    # max_attempts=3 dispatches + the one repair; attempts 2-3 get no repair.
    assert len(provider.requests) == 4
    assert [request.repair for request in provider.requests] == [False, True, False, False]


async def test_no_repair_when_the_task_disables_it() -> None:
    """repair_attempts=0: malformed output still retries within max_attempts
    (bounded malformed-output task retries, ADR-0017) and only fails terminal
    on the final attempt — it never fails on the first malformed response."""
    provider = FakeLLMProvider()
    provider.queue_responses(_canned("not json at all"), _canned("still not json"))
    service, _ = _service(_no_repair_task_registries(), provider=provider)
    with pytest.raises(OutputValidationError):
        await service.execute(_request())
    assert len(provider.requests) == 2  # max_attempts=2 in this task
    assert [request.repair for request in provider.requests] == [False, False]


async def test_transient_failure_retries_same_model_within_max_attempts() -> None:
    """Rate-limit errors retry on the same model (fallback disallowed by the
    default task) up to max_attempts; the third call succeeds."""
    provider = FakeLLMProvider()
    provider.fail_next_call(count=2, error=ProviderRateLimitError)
    service, _ = _service(provider=provider)
    result = await service.execute(_request())
    assert isinstance(result.output, ClassificationResult)
    assert len(provider.requests) == 3
    assert result.routing.fallback_used is False
    assert {request.model for request in provider.requests} == {"fake-model-document.classify"}


async def test_timeout_error_translation_retries_and_succeeds() -> None:
    provider = FakeLLMProvider()
    provider.fail_next_call(count=1, error=ProviderTimeoutError)
    service, _ = _service(provider=provider)
    result = await service.execute(_request())
    assert isinstance(result.output, ClassificationResult)
    assert len(provider.requests) == 2


async def test_transient_retries_are_bounded_by_max_attempts() -> None:
    """A permanently failing transient condition raises after exactly
    max_attempts calls — no retry storm."""
    provider = FakeLLMProvider()
    provider.fail_next_call(count=99, error=ProviderUnavailableError)
    service, _ = _service(provider=provider)
    with pytest.raises(ProviderUnavailableError):
        await service.execute(_request())
    assert len(provider.requests) == 3  # default max_attempts


async def test_permanent_provider_error_never_retries() -> None:
    """A non-retryable provider error (bad request from our side) fails on the
    first call — retrying the identical request would not help."""
    provider = FakeLLMProvider()
    provider.fail_next_call(error=ProviderResponseError)
    service, _ = _service(provider=provider)
    with pytest.raises(ProviderResponseError):
        await service.execute(_request())
    assert len(provider.requests) == 1


async def test_unexpected_adapter_error_is_non_retryable() -> None:
    """An SDK-shaped exception becomes the safe, non-retryable
    ProviderResponseError and does not retry."""
    provider = FakeLLMProvider()
    provider.fail_next_call(error=RuntimeError)
    service, _ = _service(provider=provider)
    with pytest.raises(ProviderResponseError):
        await service.execute(_request())
    assert len(provider.requests) == 1


async def test_transient_error_inside_repair_consumes_the_task_retry_budget() -> None:
    """A transient failure during the single repair call does not escape
    immediately (ADR-0017): it consumes one bounded task retry, so the next
    attempt re-dispatches and can still succeed — the budgets stay distinct."""
    provider = FakeLLMProvider()
    provider.set_next_response(_canned("not json at all"))
    provider.queue_transient_failure(count=1, error=ProviderRateLimitError)
    service, _ = _service(provider=provider)
    result = await service.execute(_request())

    assert isinstance(result.output, ClassificationResult)
    assert len(provider.requests) == 3
    assert [request.repair for request in provider.requests] == [False, True, False]
    assert result.routing.fallback_used is False


async def test_transient_error_inside_repair_is_bounded_by_max_attempts() -> None:
    """A permanently transient repair (and then dispatch) path still stops at
    max_attempts dispatches plus the one repair — never a retry storm."""
    provider = FakeLLMProvider()
    provider.set_next_response(_canned("not json at all"))
    provider.queue_transient_failure(count=99, error=ProviderUnavailableError)
    service, _ = _service(provider=provider)
    with pytest.raises(ProviderUnavailableError):
        await service.execute(_request())
    # attempt 1 + repair (transient) + attempts 2..max_attempts (transient).
    assert len(provider.requests) == 4
    assert [request.repair for request in provider.requests] == [False, True, False, False]


async def test_repair_is_rejected_when_the_enlarged_prompt_exceeds_the_context_limit() -> None:
    """The task context bound is re-applied to the enlarged repair prompt
    (Scope §6.4): a repair that would push the request over the token ceiling
    is refused before any second dispatch, and the failure is terminal —
    retrying the identical cycle cannot shrink the prompt."""
    registries = InMemoryRegistries.default()
    registries.tasks = InMemoryTaskRegistry(
        {
            "document.classify": TaskDefinition(
                name="document.classify",
                prompt_name="classify",
                prompt_version=1,
                input_variables=["document_id"],
                required_capabilities=[Capability.STRUCTURED_OUTPUT],
                output_schema="demo.ClassificationResult",
                retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
                # The rendered prompt fits (24 estimated tokens) but the repair
                # prompt, which appends the bounded repair instruction, does not
                # (88 estimated tokens).
                max_input_tokens=40,
            )
        }
    )
    provider = FakeLLMProvider()
    provider.set_next_response(_canned("not json at all"))
    service, _ = _service(registries, provider=provider)
    with pytest.raises(RepairNotPossibleError, match="context limit"):
        await service.execute(_request())
    assert len(provider.requests) == 1  # the repair is never dispatched


async def test_repair_is_rejected_when_it_exceeds_the_cost_limit() -> None:
    """The request cost ceiling is re-applied to the enlarged repair prompt
    (Scope §6.4/§6.5): a repair that would push the estimated cost over the
    limit is refused before any second dispatch, so a malformed response can
    never trigger an unbounded-cost second request."""
    provider = FakeLLMProvider()
    huge = "x" * MAX_REPAIR_CONTEXT_LENGTH
    provider.set_next_response(_canned(huge))
    service, _ = _service(provider=provider)
    # The original prompt's estimated cost (0.002072) fits the limit; the
    # enlarged repair prompt's (0.004181) does not, so the repair is refused.
    with pytest.raises(RepairNotPossibleError, match="cost limit"):
        await service.execute(_request(), maximum_estimated_cost=Decimal("0.003"))
    assert len(provider.requests) == 1  # the repair is never dispatched


async def test_usage_and_cost_aggregate_the_original_and_repair_responses() -> None:
    """Scope §6.5 accounting: the successful result prices the real traffic —
    the original malformed response plus the repair response — instead of
    discarding the repair's usage and under-reporting cost."""
    provider = FakeLLMProvider()
    provider.set_next_response(
        _canned("not json at all", usage=TokenUsage(input_tokens=100, output_tokens=50))
    )
    service, _ = _service(provider=provider)
    result = await service.execute(_request())

    assert len(provider.requests) == 2
    assert result.usage.input_tokens > 100  # includes the repair response's tokens
    assert result.usage.output_tokens > 50
    assert result.usage != TokenUsage(input_tokens=100, output_tokens=50)
    assert result.cost.amount > 0


class _FallbackModelRegistry(ModelRegistry):
    """Two fake models of the same provider so a transient failure can fall
    back to the second, region-safe model (the single configured provider)."""

    def __init__(self) -> None:
        self._models = [
            self._model("fake.document-classifier-a", "fake-model-a", priority=10),
            self._model("fake.document-classifier-b", "fake-model-b", priority=20),
        ]

    @staticmethod
    def _model(model_id: str, model: str, *, priority: int) -> ModelDefinition:
        return ModelDefinition(
            id=model_id,
            provider="fake",
            model=model,
            capabilities=[Capability.STRUCTURED_OUTPUT],
            context_window=128_000,
            supported_parameters=[],
            priority=priority,
            pricing=PricingBasis(
                currency="USD",
                input_price_per_million_tokens=Decimal("1.00"),
                output_price_per_million_tokens=Decimal("2.00"),
                effective_date=date(2026, 1, 1),
                owner="tests",
            ),
        )

    def get(self, provider: str, model: str) -> ModelDefinition:
        for definition in self._models:
            if definition.provider == provider and definition.model == model:
                return definition
        raise KeyError(model)

    def all(self) -> list[ModelDefinition]:
        return list(self._models)

    def resolve(
        self, task: TaskDefinition, *, allowed_providers: list[str] | None = None
    ) -> ModelDefinition:
        for definition in self._models:
            if not definition.available:
                continue
            if allowed_providers is not None and definition.provider not in allowed_providers:
                continue
            return definition
        raise ValueError(f"no model satisfies task {task.name}")

    def route(
        self,
        task: TaskDefinition,
        *,
        allowed_providers: list[str] | None = None,
        allowed_model_ids: list[str] | None = None,
        model_override: str | None = None,
        estimated_input_tokens: int = 0,
        maximum_estimated_cost: Decimal | None = None,
        excluded_model_ids: Any = (),
        attachments: Any = (),
        region_of_provider: Any = None,
    ) -> RoutingDecision:
        excluded = set(excluded_model_ids)
        if excluded:
            # Mirror the production router's region-safety rule (Scope §6.3):
            # fallback candidates must stay in the same pinned region; the fake
            # provider is unpinned, so fallback is unrestricted here.
            primary_regions = {region_of_provider.get("fake", "")} - {""}
            if primary_regions:
                raise ValueError("no fallback in the same region")
        for definition in sorted(
            self._models, key=lambda model: (model.id in excluded, model.priority)
        ):
            if definition.id in excluded:
                continue
            if allowed_providers is not None and definition.provider not in allowed_providers:
                continue
            return RoutingDecision(
                model=definition,
                reason="ordered fallback" if excluded else "first eligible configured model",
                fallback_used=bool(excluded),
                estimated_input_tokens=estimated_input_tokens,
                estimated_max_cost=Decimal("0"),
            )
        raise ValueError(f"no model satisfies task {task.name}")


async def test_transient_failure_falls_back_to_a_second_model_when_allowed() -> None:
    """With fallback allowed, a transient failure excludes the failed model and
    the next route picks the eligible fallback model — same provider, region
    preserved, never a retry storm."""
    registries = InMemoryRegistries.default()
    registries.models = _FallbackModelRegistry()
    registries.tasks = InMemoryTaskRegistry(
        {
            "document.classify": TaskDefinition(
                name="document.classify",
                prompt_name="classify",
                prompt_version=1,
                input_variables=["document_id"],
                required_capabilities=[Capability.STRUCTURED_OUTPUT],
                output_schema="demo.ClassificationResult",
                retry_policy=RetryPolicy(max_attempts=2, repair_attempts=0),
                fallback_policy=FallbackPolicy(allowed=True),
            )
        }
    )
    provider = FakeLLMProvider()
    provider.fail_next_call(count=1, error=ProviderUnavailableError)
    service, _ = _service(registries, provider=provider)
    result = await service.execute(_request())

    assert len(provider.requests) == 2
    assert provider.requests[0].model == "fake-model-a"
    assert provider.requests[1].model == "fake-model-b"
    assert result.routing.model == "fake-model-b"
    assert result.routing.fallback_used is True


class _FakeSecondProvider(FakeLLMProvider):
    """A second deterministic fake adapter with a distinct provider id, so the
    router's reviewed cross-provider fallback is executable in tests (Scope
    §6.2 routing + §6.4 retries)."""

    provider_id = "fake2"


def _provider_model(
    *, provider_id: str, model_id: str, model: str, priority: int
) -> ModelDefinition:
    return ModelDefinition(
        id=model_id,
        provider=provider_id,
        model=model,
        capabilities=[Capability.STRUCTURED_OUTPUT],
        context_window=128_000,
        # The task's default parameters must be supported for the
        # capability/cost router to consider the model eligible (Scope §6.2).
        supported_parameters=["max_tokens", "temperature"],
        priority=priority,
        pricing=PricingBasis(
            currency="USD",
            input_price_per_million_tokens=Decimal("1.00"),
            output_price_per_million_tokens=Decimal("2.00"),
            effective_date=date(2026, 1, 1),
            owner="tests",
        ),
    )


def _cross_provider_registries() -> InMemoryRegistries:
    """Registries with one model per of two providers and a fallback-allowed
    task, so the real capability/cost router can re-route across providers."""
    registries = InMemoryRegistries.default()
    registries.models = CapabilityCostModelRegistry(
        [
            _provider_model(
                provider_id="fake",
                model_id="fake.document-classifier",
                model="fake-model-a",
                priority=10,
            ),
            _provider_model(
                provider_id="fake2",
                model_id="fake2.document-classifier",
                model="fake2-model-b",
                priority=20,
            ),
        ]
    )
    registries.tasks = InMemoryTaskRegistry(
        {
            "document.classify": TaskDefinition(
                name="document.classify",
                prompt_name="classify",
                prompt_version=1,
                input_variables=["document_id"],
                required_capabilities=[Capability.STRUCTURED_OUTPUT],
                output_schema="demo.ClassificationResult",
                retry_policy=RetryPolicy(max_attempts=2, repair_attempts=0),
                fallback_policy=FallbackPolicy(allowed=True, prefer_same_provider=False),
            )
        }
    )
    return registries


def _two_provider_service(
    registries: InMemoryRegistries,
    fake_provider: FakeLLMProvider,
    second_provider: _FakeSecondProvider,
) -> AIService:
    return AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        providers={"fake": fake_provider, "fake2": second_provider},
        schema_resolver=_resolver,
    )


async def test_transient_failure_falls_back_across_configured_providers() -> None:
    """The release's configured provider fallback is executable: with two
    providers configured, a transient failure re-routes through the real
    router to the other provider's eligible model in the same region (Scope
    §6.2 routing + §6.4 retries) and dispatches through the resolved adapter."""
    fake_provider = FakeLLMProvider()
    fake_provider.region = "eu"
    second_provider = _FakeSecondProvider()
    second_provider.region = "eu"
    fake_provider.fail_next_call(count=1, error=ProviderUnavailableError)
    service = _two_provider_service(_cross_provider_registries(), fake_provider, second_provider)
    result = await service.execute(_request())

    assert len(fake_provider.requests) == 1
    assert fake_provider.requests[0].model == "fake-model-a"
    assert len(second_provider.requests) == 1
    assert second_provider.requests[0].model == "fake2-model-b"
    assert result.routing.provider == "fake2"
    assert result.routing.model == "fake2-model-b"
    assert result.routing.fallback_used is True
    assert result.routing.region == "eu"


async def test_fallback_never_moves_a_request_across_regions() -> None:
    """A configured fallback can never implicitly change region (Scope §6.3
    regional amendment): with the primary provider pinned to 'eu' and the only
    alternative pinned to 'us', no candidate survives the region constraint —
    and the original transient failure surfaces (retryable by the caller/job)
    instead of being converted into a permanent ModelNotAvailableError."""
    fake_provider = FakeLLMProvider()
    fake_provider.region = "eu"
    second_provider = _FakeSecondProvider()
    second_provider.region = "us"
    fake_provider.fail_next_call(count=1, error=ProviderRateLimitError)
    service = _two_provider_service(_cross_provider_registries(), fake_provider, second_provider)
    with pytest.raises(ProviderRateLimitError):
        await service.execute(_request())
    assert len(fake_provider.requests) == 1
    assert second_provider.requests == []


async def test_unconfigured_resolved_provider_is_rejected() -> None:
    """A model whose provider is not configured in the service fails closed
    with a safe ModelNotAvailableError — the provider map is authoritative and
    the service never silently dispatches through an unconfigured adapter."""
    registries = _cross_provider_registries()
    fake_provider = FakeLLMProvider()
    service = AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        providers={"fake": fake_provider},
        schema_resolver=_resolver,
    )
    with pytest.raises(ModelNotAvailableError):
        await service.execute(_request(), allowed_model_ids=["fake2.document-classifier"])
    assert fake_provider.requests == []


# --- Item 3: input normalisation, redaction and storage resolution ---


async def test_redaction_hook_masks_sensitive_text_before_dispatch() -> None:
    """The redaction hook runs before the prompt is rendered: the provider
    only ever sees the redacted form."""

    def redact(text: str) -> str:
        return text.replace("4221 1111 2222", "XXXX")

    service, provider = _service(_text_task_registries(), redactor=redact)
    result = await service.execute(_request(text="card 4221 1111 2222 expires soon"))
    assert isinstance(result.output, ClassificationResult)
    assert "4221 1111 2222" not in provider.requests[0].prompt
    assert "XXXX" in provider.requests[0].prompt
    assert "4221 1111 2222" not in json.dumps(result.output.model_dump())


async def test_redaction_applies_to_message_content() -> None:
    def redact(text: str) -> str:
        return text.replace("secret-token", "[REDACTED]")

    registries = _text_task_registries()
    registries.tasks = InMemoryTaskRegistry(
        {
            "document.classify": TaskDefinition(
                name="document.classify",
                prompt_name="classify",
                prompt_version=1,
                input_variables=["messages"],
                required_capabilities=[Capability.STRUCTURED_OUTPUT],
                output_schema="demo.ClassificationResult",
                retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
            )
        }
    )
    registries.prompts = InMemoryPromptRegistry(
        {
            ("classify", 1): PromptDefinition(
                name="classify",
                version=1,
                system_instructions="Classify input.",
                input_variables=["messages"],
                user_template="Conversation: {messages}",
                output_contract="demo.ClassificationResult",
            )
        }
    )
    service, provider = _service(registries, redactor=redact)
    result = await service.execute(
        _request(
            text=None,
            messages=[{"role": "user", "content": "handle secret-token now"}],
        )
    )
    assert isinstance(result.output, ClassificationResult)
    assert "secret-token" not in provider.requests[0].prompt
    assert "[REDACTED]" in provider.requests[0].prompt


class _DocumentModelRegistry(ModelRegistry):
    """Registry with one fake model declaring document support."""

    def __init__(self, *, capabilities: list[Capability] | None = None) -> None:
        self._model = ModelDefinition(
            id="fake.document-classifier",
            provider="fake",
            model="fake-model-document.classify",
            capabilities=capabilities or [Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
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
        self, task: TaskDefinition, *, allowed_providers: list[str] | None = None
    ) -> ModelDefinition:
        return self._model


def _storage_registries() -> InMemoryRegistries:
    """Registries whose task/prompt declare a ``storage_reference`` variable
    and whose model can carry the resolved attachment."""
    registries = InMemoryRegistries.default()
    registries.tasks = InMemoryTaskRegistry(
        {
            "document.classify": TaskDefinition(
                name="document.classify",
                prompt_name="classify",
                prompt_version=1,
                input_variables=["storage_reference"],
                required_capabilities=[Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
                output_schema="demo.ClassificationResult",
                retry_policy=RetryPolicy(max_attempts=2, repair_attempts=1),
            )
        }
    )
    registries.prompts = InMemoryPromptRegistry(
        {
            ("classify", 1): PromptDefinition(
                name="classify",
                version=1,
                system_instructions="Extract facts from the supplied document.",
                input_variables=["storage_reference"],
                user_template="Analysing document {storage_reference}.",
                output_contract="demo.ClassificationResult",
            )
        }
    )
    registries.models = _DocumentModelRegistry()
    return registries


async def test_storage_reference_resolves_to_validated_attachment() -> None:
    """A private storage reference is resolved server-side into a bounded
    attachment with a correct SHA-256 digest; only the approved display name
    reaches the prompt, never the reference or the bytes."""
    reference = f"organisations/{_ORG_ID}/scratch/doc-1/original.pdf"
    storage = FakeObjectStorage(bucket="test-bucket")
    content = b"%PDF-1.7 analysis fixture"
    await storage.put(reference, content, content_type="application/pdf")

    service, provider = _service(
        _storage_registries(),
        attachment_resolver=StorageAttachmentResolver(storage),
    )
    result = await service.execute(_storage_request(reference=reference))

    assert isinstance(result.output, ClassificationResult)
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert len(request.attachments) == 1
    attachment = request.attachments[0]
    assert attachment.display_name == "original.pdf"
    assert attachment.content == content
    assert result.output.attachments == [attachment.sha256_digest]
    # Approved metadata only: the private reference never reaches the provider.
    assert reference not in request.prompt
    assert "original.pdf" in request.prompt
    assert content.decode() not in request.prompt


async def test_storage_reference_missing_object_fails_safely() -> None:
    service, provider = _service(
        _storage_registries(),
        attachment_resolver=StorageAttachmentResolver(FakeObjectStorage(bucket="test-bucket")),
    )
    with pytest.raises(AIInputValidationError, match="does not exist"):
        await service.execute(_storage_request())
    assert provider.requests == []


async def test_storage_reference_from_another_organisation_is_denied() -> None:
    """ADR-0017 tenant isolation: the service/job boundary authorises *and*
    resolves the object — a reference in another organisation's namespace is
    denied before any storage metadata is read, even when the object exists."""
    other_org = uuid4()
    reference = f"organisations/{other_org}/scratch/doc-1/original.pdf"
    storage = FakeObjectStorage(bucket="test-bucket")
    await storage.put(reference, b"%PDF-1.7 analysis fixture", content_type="application/pdf")

    service, provider = _service(
        _storage_registries(),
        attachment_resolver=StorageAttachmentResolver(storage),
    )
    with pytest.raises(AIInputValidationError, match="not accessible to this organisation"):
        await service.execute(_storage_request(reference=reference))
    assert provider.requests == []


async def test_storage_reference_unsupported_mime_fails_before_dispatch() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    reference = f"organisations/{_ORG_ID}/scratch/doc-1/evil.bin"
    await storage.put(reference, b"MZ...", content_type="application/octet-stream")
    service, provider = _service(
        _storage_registries(),
        attachment_resolver=StorageAttachmentResolver(storage),
    )
    with pytest.raises(AIInputValidationError, match="unsupported content type"):
        await service.execute(_storage_request(reference=reference))
    assert provider.requests == []


class _TrackingStorage(FakeObjectStorage):
    """Fake storage that records every server-side read, so a test can prove a
    rejected object was never read into memory (Scope §6.4 bounded memory)."""

    def __init__(self) -> None:
        super().__init__(bucket="test-bucket")
        self.read_calls: list[str] = []

    async def read_object(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        self.read_calls.append(object_key)
        return await super().read_object(object_key, max_bytes=max_bytes)


async def test_oversized_object_is_rejected_before_any_bytes_are_read() -> None:
    """The head metadata rejects an oversized object before ``read_object`` is
    called, so its bytes are never allocated into worker memory."""
    storage = _TrackingStorage()
    reference = f"organisations/{_ORG_ID}/scratch/doc-1/huge.pdf"
    await storage.put(reference, b"x" * (MAX_ATTACHMENT_BYTES + 1), content_type="application/pdf")
    service, provider = _service(
        _storage_registries(),
        attachment_resolver=StorageAttachmentResolver(storage),
    )
    with pytest.raises(AIInputValidationError, match="too large"):
        await service.execute(_storage_request(reference=reference))
    assert provider.requests == []
    assert storage.read_calls == []


class _LyingHeadStorage(FakeObjectStorage):
    """Head claims a tiny size while the body is actually oversized: the bounded
    read must still fail without allocating the whole object (Scope §6.4)."""

    async def head_object(self, object_key: str) -> ObjectInfo | None:
        info = await super().head_object(object_key)
        if info is None:
            return None
        return ObjectInfo(
            object_key=info.object_key,
            size_bytes=64,  # lies: claims a tiny object
            content_type=info.content_type,
            checksum=info.checksum,
        )


async def test_head_read_race_still_fails_bounded() -> None:
    """An object that grew between head and read (a head/read race) fails the
    bounded read instead of allocating arbitrary memory (Scope §6.4/§5.8)."""
    storage = _LyingHeadStorage(bucket="test-bucket")
    reference = f"organisations/{_ORG_ID}/scratch/doc-1/racing.pdf"
    await storage.put(reference, b"x" * (MAX_ATTACHMENT_BYTES + 1), content_type="application/pdf")
    service, provider = _service(
        _storage_registries(),
        attachment_resolver=StorageAttachmentResolver(storage),
    )
    with pytest.raises(AIInputValidationError, match="valid attachment"):
        await service.execute(_storage_request(reference=reference))
    assert provider.requests == []


async def test_storage_reference_requires_a_configured_resolver() -> None:
    service, provider = _service()  # no attachment_resolver wired
    with pytest.raises(AIInputValidationError, match="attachment resolver"):
        await service.execute(_storage_request())
    assert provider.requests == []


async def test_explicit_attachments_conflict_with_storage_reference() -> None:
    service, provider = _service(
        _storage_registries(),
        attachment_resolver=StorageAttachmentResolver(FakeObjectStorage(bucket="test-bucket")),
    )
    attachment = Attachment(display_name="a.pdf", mime_type="application/pdf", content=b"%PDF")
    with pytest.raises(AIInputValidationError, match="mutually exclusive"):
        await service.execute(_storage_request(), attachments=[attachment])
    assert provider.requests == []


# --- Item 4: leakage and idempotency ---


async def test_errors_never_leak_provider_content_or_references() -> None:
    """Error messages carry only safe generic text; malformed provider output,
    storage references and attachment bytes never appear."""
    provider = FakeLLMProvider()
    provider.queue_responses(
        _canned("super-secret-invalid-provider-text"),
        _canned("super-secret-invalid-provider-text-again"),
        _canned("super-secret-invalid-provider-text"),
        _canned("super-secret-invalid-provider-text-again"),
    )
    service, _ = _service(provider=provider)
    with pytest.raises(OutputValidationError) as exc_info:
        await service.execute(_request())
    assert "super-secret-invalid-provider-text" not in str(exc_info.value)

    reference = f"organisations/{_ORG_ID}/scratch/doc-1/original.pdf"
    service, provider = _service(
        _storage_registries(),
        attachment_resolver=StorageAttachmentResolver(FakeObjectStorage(bucket="test-bucket")),
    )
    with pytest.raises(AIInputValidationError) as exc_info:
        await service.execute(_storage_request(reference=reference))
    assert reference not in str(exc_info.value)
    assert provider.requests == []


async def test_repair_traffic_is_idempotent_for_the_same_input() -> None:
    """Two executions of the same input with the same malformed first response
    produce identical repair traffic (same prompts, same repair flags, same
    model) and identical results, so a job retry cannot diverge (Scope §6.4;
    durable output idempotency itself is §6.6)."""
    provider = FakeLLMProvider()
    service, _ = _service(provider=provider)
    results: list[AIResult] = []
    cycles: list[list[ProviderRequest]] = []
    for _ in range(2):
        provider.set_next_response(_canned("not json at all"))
        results.append(await service.execute(_request()))
        cycles.append(provider.requests[-2:])
    first, second = cycles
    first_result, second_result = results

    assert first_result.output == second_result.output
    assert first_result.usage == second_result.usage
    assert first_result.cost == second_result.cost
    assert len(provider.requests) == 4
    assert [request.repair for request in first] == [False, True]
    assert [request.repair for request in second] == [False, True]
    assert first[0].prompt == second[0].prompt
    assert first[1].prompt == second[1].prompt
    assert first[1].model == first[0].model
    assert second[1].model == second[0].model
    assert first[1].output_schema == second[1].output_schema
    assert first[1].output_json_schema == second[1].output_json_schema


async def test_transient_retry_traffic_is_idempotent_for_the_same_input() -> None:
    """The same transient failure pattern produces identical retry traffic on
    every execution: the same attempt count, the same prompts and the same
    validated result, so retries stay deterministic (Scope §6.4)."""
    provider = FakeLLMProvider()
    service, _ = _service(provider=provider)
    first_cycles: list[list[ProviderRequest]] = []
    for _ in range(2):
        provider.fail_next_call(count=2, error=ProviderRateLimitError)
        await service.execute(_request())
        first_cycles.append(provider.requests[-3:])
    first, second = first_cycles
    assert len(provider.requests) == 6  # two runs, (2 failures + 1 success) each
    assert [request.repair for request in first] == [False, False, False]
    assert [request.repair for request in second] == [False, False, False]
    assert [request.prompt for request in first] == [request.prompt for request in second]
    assert first[2].output_schema == second[2].output_schema


# --- Adapter wire-format checks for the native path (Scope §6.4) ---


def _payload_provider(
    adapter: OpenAICompatibleAdapter, *, output_json_schema: dict[str, Any] | None
) -> dict[str, Any]:
    request = ProviderRequest(
        task="document.classify",
        model="gpt-4o-mini",
        prompt="Classify this document.",
        output_schema="demo.ClassificationResult",
        output_json_schema=output_json_schema,
        max_tokens=64,
        temperature=0,
    )
    return adapter._build_payload(request)  # type: ignore[reportPrivateUsage]


def test_openai_native_structured_output_uses_json_schema_response_format() -> None:
    from app.ai.providers.openai import OpenAIAdapter

    schema = {"type": "object", "properties": {"category": {"type": "string"}}}
    payload = _payload_provider(OpenAIAdapter(api_key="sk-test"), output_json_schema=schema)
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "structured_output", "schema": schema, "strict": False},
    }


def test_deepseek_falls_back_to_json_mode() -> None:
    from app.ai.providers.deepseek import DeepSeekAdapter

    payload = _payload_provider(
        DeepSeekAdapter(api_key="dk-test"), output_json_schema={"type": "object"}
    )
    assert payload["response_format"] == {"type": "json_object"}


def test_vertex_uses_response_json_schema_for_native_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex native structured output goes through ``responseJsonSchema`` (the
    JSON-Schema field), not ``responseSchema`` (an OpenAPI-schema subset), so
    arbitrary Pydantic JSON Schema — including nested ``$defs`` — is sent
    verbatim (v0.7 Scope §6.4)."""
    from app.ai.providers.vertex import VertexAIAdapter

    class _FakeCredentials:
        token = "test-vertex-token"
        valid = True

    def _fake_google_auth(scopes: Any = None) -> tuple[Any, None]:
        return _FakeCredentials(), None

    monkeypatch.setattr("app.ai.providers.vertex.google_auth_default", _fake_google_auth)

    class _Nested(BaseModel):
        label: str

    class _Outer(BaseModel):
        name: str
        nested: _Nested

    schema = _Outer.model_json_schema()
    assert "$defs" in schema  # proves a JSON-Schema-specific shape

    adapter = VertexAIAdapter(project="p", location="eu-west1", credentials_path="")
    request = ProviderRequest(
        task="document.classify",
        model="gemini-2.0-flash",
        prompt="Classify.",
        output_schema="demo.ClassificationResult",
        output_json_schema=schema,
    )
    payload = adapter._build_payload(request)  # type: ignore[reportPrivateUsage]
    generation_config = payload["generationConfig"]
    assert generation_config["responseMimeType"] == "application/json"
    assert generation_config["responseJsonSchema"] == schema
    assert "responseSchema" not in generation_config

    plain = adapter._build_payload(  # type: ignore[reportPrivateUsage]
        ProviderRequest(
            task="document.classify",
            model="gemini-2.0-flash",
            prompt="Classify.",
            output_schema="demo.ClassificationResult",
        )
    )
    assert plain["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseJsonSchema" not in plain["generationConfig"]


def test_azure_native_output_follows_the_pinned_api_version() -> None:
    """Azure's native structured-output support is deployment-aware: Microsoft
    documented structured outputs arriving in 2024-08-01-preview, so an adapter
    pinned to that version (or newer) requests the ``json_schema`` response
    format while an older pinned version truthfully stays on JSON mode
    (v0.7 Scope §6.4)."""
    from app.ai.providers.azure_openai import AzureOpenAIAdapter

    schema = {"type": "object", "properties": {"category": {"type": "string"}}}

    modern = AzureOpenAIAdapter(
        endpoint="https://demo.openai.azure.com",
        api_key="az-test",
        api_version="2024-08-01-preview",
    )
    assert modern.supports_native_structured_output is True
    payload = _payload_provider(modern, output_json_schema=schema)
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "structured_output", "schema": schema, "strict": False},
    }

    legacy = AzureOpenAIAdapter(
        endpoint="https://demo.openai.azure.com",
        api_key="az-test",
        api_version="2024-02-15-preview",
    )
    assert legacy.supports_native_structured_output is False
    payload = _payload_provider(legacy, output_json_schema=schema)
    assert payload["response_format"] == {"type": "json_object"}
