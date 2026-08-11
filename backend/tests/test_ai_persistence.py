"""AI persistence unit and AIService-recorder integration tests (v0.7 Scope §6.5).

Pure-logic proofs run everywhere (policy-identifier validation, policy merge,
attachment digest); the recorder integration drives ``AIService.execute`` with
an in-memory recorder so the request-time enforcement flow (load policy ->
merge restrictions -> reserve before dispatch -> settle -> record output) is
proven without a database. The real-database proofs (settings persistence,
budget math under the row lock, idempotency, retention sweep, isolation) live
in ``test_ai_persistence_db.py``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, Field
from tests.ai_test_helpers import InMemoryModelRegistry, InMemoryRegistries

from app.ai.attachments import Attachment
from app.ai.errors import (
    AIRequestReplayError,
    AIUnavailableError,
    BudgetExceededError,
    ModelNotAvailableError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from app.ai.persistence.port import AIRequestReservation, OrganisationAIPolicy
from app.ai.persistence.service import (
    _model_registry,  # pyright: ignore[reportPrivateUsage]
    _validate_policy_identifiers,  # pyright: ignore[reportPrivateUsage]
    ai_scratch_prefix,
)
from app.ai.providers.fake import FakeLLMProvider
from app.ai.schemas import AIRequest
from app.ai.service import (
    AIService,
    _attachment_set_digest,  # pyright: ignore[reportPrivateUsage]
    _merge_organisation_policy,  # pyright: ignore[reportPrivateUsage]
)
from app.core.exceptions import ValidationError

_ORG_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


class ClassificationResult(BaseModel):
    """The demo task's output schema (mirrors test_ai_service)."""

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


class RecordingRecorder:
    """In-memory :class:`AIPersistencePort` recording every call for assertions."""

    def __init__(
        self,
        *,
        policy: OrganisationAIPolicy | None = None,
        deny_budget: bool = False,
        replay_existing: bool = False,
    ) -> None:
        self.policy = policy or OrganisationAIPolicy(enabled=True)
        self.deny_budget = deny_budget
        self.replay_existing = replay_existing
        self.reservations: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.settlements: list[dict[str, Any]] = []
        self.outputs: list[dict[str, Any]] = []

    async def load_policy(self, *, organisation_id: uuid.UUID) -> OrganisationAIPolicy:
        return self.policy

    async def reserve(self, **kwargs: Any) -> AIRequestReservation:
        if self.deny_budget:
            raise BudgetExceededError("the organisation's monthly AI budget is exhausted")
        self.reservations.append(kwargs)
        return AIRequestReservation(row_id=uuid.uuid4(), created=not self.replay_existing)

    async def record_attempt(self, **kwargs: Any) -> uuid.UUID:
        self.attempts.append(kwargs)
        return uuid.uuid4()

    async def settle(self, **kwargs: Any) -> None:
        self.settlements.append(kwargs)
        if kwargs.get("output") is not None:
            self.outputs.append(kwargs)


def _service(
    recorder: RecordingRecorder | None = None,
    *,
    provider: FakeLLMProvider | None = None,
) -> AIService:
    registries = InMemoryRegistries.default()
    return AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        provider=provider or FakeLLMProvider(),
        schema_resolver=_resolver,
    )


# --- Pure logic: registry-validated policy identifiers (v0.7 Scope §6.5) ---


def test_scratch_prefix_is_organisation_scoped() -> None:
    """Temporary analyse-only objects live under the org-scoped scratch ns."""
    assert ai_scratch_prefix(_ORG_ID) == f"organisations/{_ORG_ID}/ai/scratch/"


def test_valid_policy_identifiers_pass() -> None:
    """A known provider/model combination is accepted as-is."""
    registry = _model_registry()
    model = registry.all()[0]
    _validate_policy_identifiers(
        allowed_provider_ids=[model.provider],
        allowed_model_ids=[model.id],
        provider_override=None,
        model_override=None,
    )


def test_unknown_provider_id_fails() -> None:
    with pytest.raises(ValidationError):
        _validate_policy_identifiers(
            allowed_provider_ids=["not-a-provider"],
            allowed_model_ids=[],
            provider_override=None,
            model_override=None,
        )


def test_unknown_model_id_fails() -> None:
    with pytest.raises(ValidationError):
        _validate_policy_identifiers(
            allowed_provider_ids=[],
            allowed_model_ids=["not-a-model"],
            provider_override=None,
            model_override=None,
        )


def test_duplicate_ids_fail() -> None:
    registry = _model_registry()
    model = registry.all()[0]
    with pytest.raises(ValidationError):
        _validate_policy_identifiers(
            allowed_provider_ids=[model.provider, model.provider],
            allowed_model_ids=[],
            provider_override=None,
            model_override=None,
        )


def test_override_inside_allowlist_is_required() -> None:
    registry = _model_registry()
    model = registry.all()[0]
    with pytest.raises(ValidationError):
        _validate_policy_identifiers(
            allowed_provider_ids=[model.provider],
            allowed_model_ids=[],
            provider_override="openai",
            model_override=None,
        )


def test_mismatched_forced_model_and_provider_fail() -> None:
    """A forced model must live under the forced provider (never mis-resolve)."""
    registry = _model_registry()
    model = next(item for item in registry.all() if item.provider != "fake")
    with pytest.raises(ValidationError):
        _validate_policy_identifiers(
            allowed_provider_ids=[],
            allowed_model_ids=[],
            provider_override="fake",
            model_override=model.id,
        )


# --- Pure logic: policy merge and attachment digest ---


def test_merge_organisation_policy_intersects_allowlists() -> None:
    policy = OrganisationAIPolicy(enabled=True, allowed_provider_ids=["openai", "anthropic"])
    providers, models, override = _merge_organisation_policy(policy, ["openai"], ["m1"], None)
    assert providers == ["openai"]
    assert models == ["m1"]
    assert override is None


def test_merge_organisation_policy_forces_override() -> None:
    policy = OrganisationAIPolicy(
        enabled=True, allowed_provider_ids=["openai"], model_override="openai.m"
    )
    providers, _, override = _merge_organisation_policy(policy, None, None, "caller.m")
    assert providers == ["openai"]
    assert override == "openai.m"  # the organisation is authoritative


def test_merge_organisation_policy_empty_allowlist_means_unrestricted() -> None:
    policy = OrganisationAIPolicy(enabled=True, allowed_provider_ids=[], allowed_model_ids=[])
    providers, models, override = _merge_organisation_policy(policy, None, None, None)
    assert providers is None
    assert models is None
    assert override is None


def test_attachment_set_digest_is_deterministic_and_bounded() -> None:
    attachment = Attachment(
        display_name="lease.pdf", mime_type="application/pdf", content=b"lease bytes"
    )
    digest = _attachment_set_digest([attachment])
    assert digest == attachment.sha256_digest
    assert _attachment_set_digest([attachment]) == digest
    assert _attachment_set_digest([]) is None


# --- AIService recorder integration (v0.7 Scope §6.5 request-time enforcement) ---


async def test_disabled_organisation_is_rejected_before_any_work() -> None:
    recorder = RecordingRecorder(policy=OrganisationAIPolicy(enabled=False))
    service = _service(recorder)
    with pytest.raises(AIUnavailableError):
        await service.execute(_request(), recorder=recorder)
    assert recorder.reservations == []


async def test_success_reserves_once_settles_and_records_output() -> None:
    recorder = RecordingRecorder()
    service = _service(recorder)
    result = await service.execute(_request(), recorder=recorder)

    assert len(recorder.reservations) == 1
    reservation = recorder.reservations[0]
    assert reservation["request_id"] == result.request_id
    assert reservation["task"] == "document.classify"
    assert reservation["estimated_cost"] >= 0
    assert len(recorder.settlements) == 1
    settlement = recorder.settlements[0]
    assert settlement["status"] == "succeeded"
    assert settlement["error_code"] is None
    assert settlement["usage"].total_tokens > 0
    assert settlement["cost"].amount >= 0
    assert len(recorder.outputs) == 1
    assert recorder.outputs[0]["input_digest"] is None  # no attachments in this request


async def test_pinned_request_id_is_preserved() -> None:
    """A caller-supplied request id survives execution for job idempotency."""
    recorder = RecordingRecorder()
    service = _service(recorder)
    result = await service.execute(_request(), recorder=recorder, request_id="abc123")
    assert result.request_id == "abc123"
    assert recorder.reservations[0]["request_id"] == "abc123"


async def test_existing_pinned_request_refuses_provider_redispatch() -> None:
    """A redelivered execution id never incurs another provider call."""

    provider = FakeLLMProvider()
    recorder = RecordingRecorder(replay_existing=True)
    service = _service(provider=provider)

    with pytest.raises(AIRequestReplayError):
        await service.execute(_request(), recorder=recorder, request_id="already-recorded")

    assert provider.requests == []
    assert recorder.settlements == []


async def test_organisation_allowlist_restricts_routing() -> None:
    """A policy that forbids the only available model yields the safe error."""
    recorder = RecordingRecorder(
        policy=OrganisationAIPolicy(enabled=True, allowed_provider_ids=["openai"])
    )
    service = _service(recorder)
    with pytest.raises(ModelNotAvailableError):
        await service.execute(_request(), recorder=recorder)
    # No reservation and no settlement: the execution never dispatched.
    assert recorder.reservations == []
    assert recorder.settlements == []


async def test_budget_denial_propagates_without_settlement() -> None:
    recorder = RecordingRecorder(deny_budget=True)
    service = _service(recorder)
    with pytest.raises(BudgetExceededError):
        await service.execute(_request(), recorder=recorder)
    assert recorder.reservations == []
    assert recorder.settlements == []


class _FailingProvider(FakeLLMProvider):
    """FakeLLMProvider whose dispatch raises an unexpected adapter error."""

    async def complete(self, request: Any) -> Any:
        raise Exception("provider exploded")


async def test_terminal_failure_settles_failed_with_error_code() -> None:
    registries = InMemoryRegistries.default()
    service = AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        provider=_FailingProvider(),
        schema_resolver=_resolver,
    )
    recorder = RecordingRecorder()
    with pytest.raises(ProviderResponseError):
        await service.execute(_request(), recorder=recorder)
    assert len(recorder.reservations) == 1
    assert len(recorder.settlements) == 1
    assert recorder.settlements[0]["status"] == "failed"
    assert recorder.settlements[0]["error_code"] == "provider_response_invalid"
    assert recorder.outputs == []


async def test_attachment_digest_reaches_reservation_and_output() -> None:
    attachment = Attachment(
        display_name="lease.pdf", mime_type="application/pdf", content=b"lease bytes"
    )
    # The demo in-memory model must declare the documents capability and
    # ceilings for the router to route the attachment (v0.7 Scope §6.2 amendment).
    registries = InMemoryRegistries.default()
    model = _documents_capable_model(registries.models.all()[0])
    registries.models = InMemoryModelRegistry([model])
    service = AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        provider=FakeLLMProvider(),
        schema_resolver=_resolver,
    )
    recorder = RecordingRecorder()
    result = await service.execute(
        _request(),
        recorder=recorder,
        attachments=[attachment],
    )
    assert result.output is not None
    assert recorder.reservations[0]["input_digest"] == attachment.sha256_digest
    assert recorder.outputs[0]["input_digest"] == attachment.sha256_digest


def _documents_capable_model(model: Any) -> Any:
    """Return a copy of ``model`` declaring the documents capability."""
    from app.ai.attachments import (
        ALLOWED_ATTACHMENT_MIME_TYPES,
        MAX_ATTACHMENT_BYTES,
        MAX_TOTAL_ATTACHMENT_BYTES,
    )
    from app.ai.registry import Capability, ModelDefinition

    data = model.model_dump()
    data["capabilities"] = list({*data["capabilities"], Capability.DOCUMENTS})
    data["max_attachment_bytes"] = MAX_ATTACHMENT_BYTES
    data["max_total_attachment_bytes"] = MAX_TOTAL_ATTACHMENT_BYTES
    data["attachment_mime_types"] = sorted(ALLOWED_ATTACHMENT_MIME_TYPES)
    return ModelDefinition.model_validate(data)


async def test_settlement_metadata_carries_no_content() -> None:
    """Settlement arguments never carry prompt or document content."""
    recorder = RecordingRecorder()
    service = _service(recorder)
    await service.execute(_request(), recorder=recorder)
    serialised = str(recorder.settlements[0])
    assert "classify this lease document" not in serialised


# --- Fail-closed enforcement (v0.7 Scope §6.5) ---


async def test_execute_without_recorder_fails_closed() -> None:
    """The documented entry point cannot bypass policy: executing without the
    persistence/policy port raises instead of dispatching unenforced."""
    registries = InMemoryRegistries.default()
    service = AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        provider=FakeLLMProvider(),
        schema_resolver=_resolver,
    )
    with pytest.raises(RuntimeError, match="requires a persistence/policy port"):
        await service.execute(_request())


async def test_success_reserves_bounded_execution_estimate() -> None:
    """The budget gate reserves the bounded worst case for the retry/repair
    policy (max_attempts + repair_attempts dispatches), not just one attempt."""
    recorder = RecordingRecorder()
    service = _service(recorder)
    result = await service.execute(_request(), recorder=recorder)
    reservation = recorder.reservations[0]
    assert reservation["execution_maximum_estimated_cost"] > reservation["estimated_cost"]
    assert result.request_id == reservation["request_id"]


async def test_repair_success_persists_each_provider_dispatch() -> None:
    """The malformed response and repair call get distinct durable rows."""
    from app.ai.providers.base import ProviderResponse
    from app.ai.schemas import TokenUsage

    provider = FakeLLMProvider()
    provider.queue_responses(
        ProviderResponse(
            model="fake-model-document.classify",
            content="not json at all",
            structured=None,
            usage=TokenUsage(input_tokens=10, output_tokens=10),
            latency_ms=1.0,
            finish_reason="stop",
        )
    )
    recorder = RecordingRecorder()
    service = _service(provider=provider)
    result = await service.execute(_request(), recorder=recorder)

    assert len(recorder.reservations) == 1
    assert len(recorder.attempts) == 1
    assert recorder.attempts[0]["attempt_number"] == 2
    assert len(recorder.settlements) == 2
    succeeded, malformed = recorder.settlements  # first reservation settles last
    assert succeeded["status"] == "succeeded"
    assert malformed["status"] == "failed"
    assert malformed["usage"].input_tokens == 10
    assert result.usage.total_tokens == sum(
        settlement["usage"].total_tokens for settlement in recorder.settlements
    )


async def test_malformed_then_success_persists_each_attempt() -> None:
    """A malformed output that exhausts the repair budget consumes a bounded
    retry; both attempted provider executions are persisted and settled with
    their own outcome and error code (v0.7 Scope §2)."""
    from app.ai.providers.base import ProviderResponse
    from app.ai.schemas import TokenUsage

    provider = FakeLLMProvider()
    provider.queue_responses(
        ProviderResponse(
            model="fake-model-document.classify",
            content="not json at all",
            structured=None,
            usage=TokenUsage(input_tokens=10, output_tokens=10),
            latency_ms=1.0,
            finish_reason="stop",
        ),
        # The repair is also malformed: the attempt consumes one retry.
        ProviderResponse(
            model="fake-model-document.classify",
            content="still not json",
            structured=None,
            usage=TokenUsage(input_tokens=5, output_tokens=5),
            latency_ms=1.0,
            finish_reason="stop",
        ),
    )
    recorder = RecordingRecorder()
    service = _service(provider=provider)
    result = await service.execute(_request(), recorder=recorder)

    assert len(recorder.reservations) == 1  # first attempt via reserve
    assert len(recorder.attempts) == 2  # repair + task retry
    assert [item["attempt_number"] for item in recorder.attempts] == [2, 3]
    assert len(recorder.settlements) == 3
    repair_failed, succeeded, malformed = recorder.settlements
    assert malformed["status"] == "failed"
    assert malformed["error_code"] == "output_validation_failed"
    assert malformed["usage"].input_tokens == 10
    assert repair_failed["status"] == "failed"
    assert repair_failed["usage"].input_tokens == 5
    assert succeeded["status"] == "succeeded"
    assert succeeded["error_code"] is None
    assert succeeded["usage"].input_tokens > 0
    # The result aggregates every actual attempt.
    assert result.usage.input_tokens == sum(
        settlement["usage"].input_tokens for settlement in recorder.settlements
    )


async def test_fallback_across_differently_priced_models_prices_each_attempt() -> None:
    """A transient failure inside a repair falls back to a differently priced
    model; each attempt's row is settled with its own model's rates instead of
    the last model pricing every token (v0.7 Scope §2/§6.5)."""
    from datetime import date

    from app.ai.providers.base import ProviderResponse
    from app.ai.registry import (
        Capability,
        CapabilityCostModelRegistry,
        FallbackPolicy,
        LatencyTier,
        ModelDefinition,
        PricingBasis,
        QualityTier,
    )
    from app.ai.schemas import TokenUsage

    premium = ModelDefinition(
        id="fake.premium",
        provider="fake",
        model="fake-model-premium",
        capabilities=[Capability.STRUCTURED_OUTPUT],
        context_window=128_000,
        supported_parameters=["max_tokens"],
        quality_tier=QualityTier.STANDARD,
        latency_tier=LatencyTier.BALANCED,
        priority=1,
        pricing=PricingBasis(
            currency="USD",
            input_price_per_million_tokens=Decimal("2.00"),
            output_price_per_million_tokens=Decimal("4.00"),
            effective_date=date(2026, 1, 1),
            owner="template tests",
        ),
    )
    cheap = ModelDefinition(
        id="fake.cheap",
        provider="fake",
        model="fake-model-cheap",
        capabilities=[Capability.STRUCTURED_OUTPUT],
        context_window=128_000,
        supported_parameters=["max_tokens"],
        quality_tier=QualityTier.STANDARD,
        latency_tier=LatencyTier.BALANCED,
        priority=2,
        pricing=PricingBasis(
            currency="USD",
            input_price_per_million_tokens=Decimal("0.10"),
            output_price_per_million_tokens=Decimal("0.20"),
            effective_date=date(2026, 1, 1),
            owner="template tests",
        ),
    )
    registries = InMemoryRegistries.default()
    task = registries.tasks.get("document.classify").model_copy(
        update={
            "model_preferences": ["fake.premium", "fake.cheap"],
            "fallback_policy": FallbackPolicy(
                allowed=True, prefer_same_provider=True, allow_local=False
            ),
        }
    )
    registries.tasks.register(task)
    registries.models = CapabilityCostModelRegistry([premium, cheap])

    provider = FakeLLMProvider()
    provider.queue_responses(
        # Attempt 1: the premium model returns malformed output (billed).
        ProviderResponse(
            model="fake-model-premium",
            content="not json at all",
            structured=None,
            usage=TokenUsage(input_tokens=100, output_tokens=100),
            latency_ms=1.0,
            finish_reason="stop",
        )
    )
    # The repair on the same model fails transiently: the task's reviewed
    # fallback re-routes attempt 2 to the cheap model.
    provider.queue_transient_failure(1, error=ProviderUnavailableError)

    service = AIService(
        task_registry=registries.tasks,
        prompt_registry=registries.prompts,
        model_registry=registries.models,
        provider=provider,
        schema_resolver=_resolver,
    )
    recorder = RecordingRecorder()
    result = await service.execute(_request(), recorder=recorder)

    assert len(recorder.reservations) == 1
    assert len(recorder.attempts) == 2
    assert len(recorder.settlements) == 3
    repair_failed, succeeded, malformed = recorder.settlements
    assert repair_failed["status"] == "failed"
    assert repair_failed["error_code"] == "provider_unavailable"
    assert malformed["routing_model"] == "fake-model-premium"
    assert malformed["usage"].input_tokens == 100
    # Priced with the premium model's rates (2.00/4.00 per million).
    assert malformed["cost"].amount == Decimal("0.000600")
    assert succeeded["status"] == "succeeded"
    assert succeeded["routing_model"] == "fake-model-cheap"
    assert succeeded["cost"].amount > Decimal("0")
    assert succeeded["cost"].amount < malformed["cost"].amount
    # The result aggregates and prices each attempt with its own model.
    assert result.cost.amount == sum(
        settlement["cost"].amount for settlement in recorder.settlements
    )


async def test_output_content_retained_only_when_task_and_org_opt_in() -> None:
    """Output content is stored only when both the task-level opt-in and the
    organisation retention policy permit it (v0.7 Scope §2); otherwise the
    record is references/digests only."""

    async def run(*, task_retains: bool, org_retention_days: int | None) -> dict[str, Any]:
        registries = InMemoryRegistries.default()
        if task_retains:
            task = registries.tasks.get("document.classify").model_copy(
                update={"retains_output_content": True}
            )
            registries.tasks.register(task)
        recorder = RecordingRecorder(
            policy=OrganisationAIPolicy(enabled=True, retention_policy_days=org_retention_days)
        )
        service = AIService(
            task_registry=registries.tasks,
            prompt_registry=registries.prompts,
            model_registry=registries.models,
            provider=FakeLLMProvider(),
            schema_resolver=_resolver,
        )
        await service.execute(_request(), recorder=recorder)
        return recorder.settlements[0]

    assert (await run(task_retains=False, org_retention_days=None))["retain_content"] is False
    assert (await run(task_retains=False, org_retention_days=30))["retain_content"] is False
    assert (await run(task_retains=True, org_retention_days=None))["retain_content"] is False
    assert (await run(task_retains=True, org_retention_days=30))["retain_content"] is True


async def test_default_output_record_receives_privacy_safe_digest() -> None:
    recorder = RecordingRecorder()
    service = _service()

    await service.execute(_request(), recorder=recorder)

    settlement = recorder.outputs[0]
    assert settlement["retain_content"] is False
    assert len(settlement["output_digest"]) == 64
    assert settlement["output_reference"] is None
