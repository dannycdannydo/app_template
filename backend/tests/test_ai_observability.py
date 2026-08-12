"""AI observability tests (v0.7 Scope §6.7, blueprint §28).

The AI layer records aggregate Prometheus metrics — requests, latency, tokens,
cost, validation failures, bounded retries and reviewed fallbacks — labelled
only with low-cardinality registry ids (task/provider/model). The durable
``ai_requests``/``ai_outputs`` rows stay the per-request source of truth; these
counters are the aggregate signal. Budget denials are counted by the
persistence service and covered by the real-database tests.

Every assertion is a before/after comparison against the ``/metrics`` output
because counters are process-wide (the same pattern as
``test_observability_metrics.py``). Content, organisation ids and request ids
must never appear as labels.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager, suppress
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from httpx import ASGITransport, AsyncClient, Response
from pydantic import BaseModel, Field
from structlog.typing import EventDict
from tests.ai_test_helpers import InMemoryRegistries

from app.ai.errors import (
    AIInputValidationError,
    AIUnavailableError,
    OutputSchemaError,
    ProviderResponseError,
)
from app.ai.persistence.port import AIRequestReservation, OrganisationAIPolicy
from app.ai.providers.base import ProviderResponse
from app.ai.providers.fake import FakeLLMProvider
from app.ai.schemas import AIRequest, TokenUsage
from app.ai.service import AIService
from app.main import create_app

_ALL_AI_METRICS = (
    "ai_requests_total",
    "ai_request_duration_seconds",
    "ai_tokens_total",
    "ai_cost_total",
    "ai_validation_failures_total",
    "ai_retries_total",
    "ai_fallbacks_total",
    "ai_budget_denials_total",
)


class ClassificationResult(BaseModel):
    """Pydantic output schema the demo task validates against."""

    task: str
    prompt_hash: str
    variables: dict[str, str]
    attachments: list[str] = Field(default_factory=list)


_ORG_ID = uuid4()
_USER_ID = uuid4()


@contextmanager
def _capture_logs() -> Generator[list[EventDict]]:
    """Enter structlog's capture with contextvars merged into every entry."""
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        yield logs


class _DisabledOrgRecorder:
    """A policy-only :class:`AIPersistencePort` that reports AI disabled.

    The disabled-organisation gate rejects before any reserve/settle, so the
    remaining port methods must never be reached.
    """

    async def load_policy(self, *, organisation_id: Any) -> OrganisationAIPolicy:
        return OrganisationAIPolicy(enabled=False)

    async def reserve(self, **kwargs: Any) -> AIRequestReservation:
        raise AssertionError("a disabled organisation must never reserve")

    async def record_attempt(self, **kwargs: Any) -> UUID:
        raise AssertionError("a disabled organisation must never dispatch")

    async def settle(self, **kwargs: Any) -> None:
        raise AssertionError("a disabled organisation must never settle")


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
        allow_unmanaged_execution=True,
    )
    return service, provider


def _client_for(app: Any) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


def _metric_value(body: str, metric: str, labels: dict[str, str]) -> float:
    """Extract one sample's value from Prometheus text output (0.0 if absent).

    Label names are rendered in the alphabetically sorted order Prometheus
    uses, independent of the dict's insertion order.
    """
    label_part = "{" + ",".join(f'{key}="{labels[key]}"' for key in sorted(labels)) + "}"
    prefix = metric + label_part
    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line.split()[-1])
    return 0.0


async def _fetch_metrics() -> str:
    async with _client_for(create_app()) as client:
        response: Response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return response.text


async def _execution_metrics(service: AIService, *, failure: bool = False) -> dict[str, str]:
    """Run one execution and return the before/after /metrics bodies."""
    before = await _fetch_metrics()
    if failure:
        with suppress(Exception):
            await service.execute(_request())
    else:
        await service.execute(_request())
    after = await _fetch_metrics()
    return {"before": before, "after": after}


_FAKE_LABELS = {
    "task": "document.classify",
    "provider": "fake",
    "model": "fake-model-document.classify",
}


async def test_ai_metric_families_are_exposed() -> None:
    body = await _fetch_metrics()
    for metric in _ALL_AI_METRICS:
        assert f"# TYPE {metric} " in body
        assert f"# HELP {metric} " in body


async def test_successful_execution_records_requests_latency_tokens_cost() -> None:
    service, _ = _service()
    bodies = await _execution_metrics(service)

    labels = {**_FAKE_LABELS, "status": "succeeded"}
    assert _metric_value(bodies["after"], "ai_requests_total", labels) == (
        _metric_value(bodies["before"], "ai_requests_total", labels) + 1
    )
    histogram_labels = {
        "task": "document.classify",
        "provider": "fake",
        "model": "fake-model-document.classify",
    }
    assert (
        _metric_value(bodies["after"], "ai_request_duration_seconds_count", histogram_labels)
        == _metric_value(bodies["before"], "ai_request_duration_seconds_count", histogram_labels)
        + 1
    )
    assert _metric_value(
        bodies["after"], "ai_tokens_total", {**_FAKE_LABELS, "direction": "input"}
    ) > _metric_value(bodies["before"], "ai_tokens_total", {**_FAKE_LABELS, "direction": "input"})
    assert _metric_value(
        bodies["after"], "ai_tokens_total", {**_FAKE_LABELS, "direction": "output"}
    ) > _metric_value(bodies["before"], "ai_tokens_total", {**_FAKE_LABELS, "direction": "output"})
    assert _metric_value(bodies["after"], "ai_cost_total", _FAKE_LABELS) > _metric_value(
        bodies["before"], "ai_cost_total", _FAKE_LABELS
    )


async def test_terminal_failure_records_failed_request_without_retries() -> None:
    """A permanent provider error is one failed dispatch, never a retry."""
    provider = FakeLLMProvider()
    provider.fail_next_call(1, error=ProviderResponseError)
    service, _ = _service(provider=provider)
    bodies = await _execution_metrics(service, failure=True)

    failed = {**_FAKE_LABELS, "status": "failed"}
    succeeded = {**_FAKE_LABELS, "status": "succeeded"}
    assert _metric_value(bodies["after"], "ai_requests_total", failed) == (
        _metric_value(bodies["before"], "ai_requests_total", failed) + 1
    )
    assert _metric_value(bodies["after"], "ai_requests_total", succeeded) == _metric_value(
        bodies["before"], "ai_requests_total", succeeded
    )
    assert _metric_value(bodies["after"], "ai_retries_total", _FAKE_LABELS) == _metric_value(
        bodies["before"], "ai_retries_total", _FAKE_LABELS
    )


async def test_transient_failures_record_failed_attempts_and_bounded_retries() -> None:
    """Three transient failures exhaust max_attempts=3: 3 failed dispatches,
    2 bounded retries."""
    provider = FakeLLMProvider()
    provider.fail_next_call(3)  # ProviderUnavailableError (retryable)
    service, _ = _service(provider=provider)
    bodies = await _execution_metrics(service, failure=True)

    failed = {**_FAKE_LABELS, "status": "failed"}
    assert _metric_value(bodies["after"], "ai_requests_total", failed) == (
        _metric_value(bodies["before"], "ai_requests_total", failed) + 3
    )
    assert _metric_value(bodies["after"], "ai_retries_total", _FAKE_LABELS) == (
        _metric_value(bodies["before"], "ai_retries_total", _FAKE_LABELS) + 2
    )


def _malformed_response() -> ProviderResponse:
    return ProviderResponse(
        model="fake-model-document.classify",
        content="this is not JSON",
        structured=None,
        usage=TokenUsage(input_tokens=4, output_tokens=4),
        latency_ms=1.0,
    )


async def test_repair_success_records_validation_failure_and_retried_attempt() -> None:
    """A malformed output followed by a successful repair: the first attempt is
    a failed dispatch with a validation failure, the repair attempt wins."""
    provider = FakeLLMProvider()
    provider.set_next_response(_malformed_response())
    service, _ = _service(provider=provider)
    bodies = await _execution_metrics(service)

    failed = {**_FAKE_LABELS, "status": "failed"}
    succeeded = {**_FAKE_LABELS, "status": "succeeded"}
    assert _metric_value(bodies["after"], "ai_requests_total", failed) == (
        _metric_value(bodies["before"], "ai_requests_total", failed) + 1
    )
    assert _metric_value(bodies["after"], "ai_requests_total", succeeded) == (
        _metric_value(bodies["before"], "ai_requests_total", succeeded) + 1
    )
    assert (
        _metric_value(bodies["after"], "ai_validation_failures_total", _FAKE_LABELS)
        == _metric_value(bodies["before"], "ai_validation_failures_total", _FAKE_LABELS) + 1
    )
    # The repair is a second dispatch, counted as one bounded retry.
    assert _metric_value(bodies["after"], "ai_retries_total", _FAKE_LABELS) == (
        _metric_value(bodies["before"], "ai_retries_total", _FAKE_LABELS) + 1
    )


async def test_no_content_ever_becomes_a_metric_label() -> None:
    """Labels are registry ids only: the document text and metadata never
    appear anywhere in the /metrics output."""
    service, _ = _service()
    bodies = await _execution_metrics(service)
    after = bodies["after"]
    # The document text and metadata are present in the rendered prompt but
    # must never surface in the exposition format.
    assert "classify this lease document" not in after
    assert "doc-1" not in after


# --- AI request logging (v0.7 Scope §6.7, blueprint §28) ---------------------


async def test_disabled_organisation_emits_request_started_then_failed() -> None:
    """A safe pre-dispatch failure still terminates the started request.

    The disabled-organisation policy gate raises inside the execution tail, so
    ``ai.request.failed`` is emitted for the started request with no attempts —
    the synchronous API path has no worker failure log to compensate
    (v0.7 Scope §6.7)."""
    service, _ = _service()
    with _capture_logs() as logs, pytest.raises(AIUnavailableError):
        await service.execute(_request(), recorder=_DisabledOrgRecorder())

    started = [entry for entry in logs if entry["event"] == "ai.request.started"]
    failed = [entry for entry in logs if entry["event"] == "ai.request.failed"]
    assert started and failed
    assert started[0]["ai_request_id"] == failed[0]["ai_request_id"]
    assert failed[0]["task"] == "document.classify"
    assert failed[0]["error_code"] == "ai_unavailable"
    assert failed[0]["attempts"] == 0
    assert failed[0]["provider"] is None
    assert failed[0]["model"] is None


async def test_input_validation_failure_emits_request_started_then_failed() -> None:
    """A safe pre-dispatch render/validation failure is also a lifecycle.

    The in-memory demo prompt requires the ``document_id`` input variable;
    dropping it fails before any provider dispatch."""
    service, _ = _service()
    with _capture_logs() as logs, pytest.raises(AIInputValidationError):
        await service.execute(_request(metadata={}))

    started = [entry for entry in logs if entry["event"] == "ai.request.started"]
    failed = [entry for entry in logs if entry["event"] == "ai.request.failed"]
    assert started and failed
    assert started[0]["ai_request_id"] == failed[0]["ai_request_id"]
    assert failed[0]["error_code"] == "ai_input_invalid"
    assert failed[0]["attempts"] == 0


async def test_success_log_binds_required_fields_without_content() -> None:
    """The success tail binds the required fields and no document content."""
    service, _ = _service()
    with _capture_logs() as logs:
        await service.execute(_request())

    started = [entry for entry in logs if entry["event"] == "ai.request.started"]
    succeeded = [entry for entry in logs if entry["event"] == "ai.request.succeeded"]
    assert started and succeeded
    for entry in [*started, *succeeded]:
        assert entry["ai_request_id"]
        assert entry["task"] == "document.classify"
    assert succeeded[0]["provider"] == "fake"
    assert succeeded[0]["model"] == "fake-model-document.classify"
    assert succeeded[0]["prompt_name"]
    assert all("classify this lease document" not in str(entry) for entry in logs)
    assert all("doc-1" not in str(entry) for entry in logs)


async def test_ai_request_logs_never_contain_prompt_metadata_or_provider_output() -> None:
    """BP §28 never-log list for the AI surface: the terminal failure line
    carries the safe error code only — never prompt text, request metadata
    values or provider output."""
    provider = FakeLLMProvider()
    provider.fail_next_call(1, error=ProviderResponseError)
    service, _ = _service(provider=provider)
    with _capture_logs() as logs, suppress(Exception):
        await service.execute(_request())

    events = [entry["event"] for entry in logs]
    assert "ai.request.started" in events
    assert "ai.request.failed" in events
    assert "ai.request.succeeded" not in events
    for entry in logs:
        line = str(entry)
        assert "classify this lease document" not in line
        assert "doc-1" not in line
        assert "this is not JSON" not in line
    for entry in logs:
        if entry["event"].startswith("ai.request."):
            assert entry["ai_request_id"]
            assert entry["task"] == "document.classify"
