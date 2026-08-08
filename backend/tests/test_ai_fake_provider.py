"""FakeLLMProvider determinism tests (v0.7 Scope §6.1, ADR-0017).

The fake is the default adapter for the whole suite, so its behaviour must be
provable: same request → same response (determinism), structured output only
when asked, recorded requests for assertion, and the failure-arming helpers
that the retry/validation tests rely on (Scope §6.4).
"""

from __future__ import annotations

import pytest

from app.ai.errors import ProviderUnavailableError
from app.ai.providers.base import ProviderRequest, ProviderResponse
from app.ai.providers.fake import FakeLLMProvider
from app.ai.schemas import TokenUsage


def _request(*, output_schema: str | None = None, task: str = "document.classify") -> ProviderRequest:
    return ProviderRequest(
        task=task,
        prompt="Classify: {document_id}",
        output_schema=output_schema,
        metadata={"document_id": "doc-1"},
    )


async def test_same_request_returns_identical_response() -> None:
    provider = FakeLLMProvider()
    first = await provider.complete(_request())
    second = await provider.complete(_request())
    assert first.content == second.content
    assert first.structured == second.structured
    assert first.usage == second.usage
    assert first.latency_ms == second.latency_ms
    assert first.model == second.model


async def test_different_inputs_produce_different_output() -> None:
    provider = FakeLLMProvider()
    first = await provider.complete(_request())
    second = await provider.complete(_request(task="lease.extract_terms"))
    assert first.content != second.content


async def test_structured_output_is_returned_only_when_requested() -> None:
    provider = FakeLLMProvider()
    structured = await provider.complete(_request(output_schema="demo.ClassificationResult"))
    assert structured.structured is not None
    assert structured.structured["schema"] == "demo.ClassificationResult"
    assert structured.structured["task"] == "document.classify"
    assert structured.structured["variables"] == {"document_id": "doc-1"}

    plain = await provider.complete(_request())
    assert plain.structured is None
    assert isinstance(plain.content, str)


async def test_structured_output_is_deterministic() -> None:
    provider = FakeLLMProvider()
    first = await provider.complete(_request(output_schema="demo.ClassificationResult"))
    second = await provider.complete(_request(output_schema="demo.ClassificationResult"))
    assert first.structured == second.structured


async def test_requests_are_recorded_for_assertion() -> None:
    provider = FakeLLMProvider()
    await provider.complete(_request())
    await provider.complete(_request(task="lease.extract_terms"))
    assert [request.task for request in provider.requests] == [
        "document.classify",
        "lease.extract_terms",
    ]


async def test_model_name_is_deterministic_per_task() -> None:
    provider = FakeLLMProvider()
    response = await provider.complete(_request())
    assert response.model == "fake-model-document.classify"


async def test_fail_next_call_raises_then_recovers() -> None:
    provider = FakeLLMProvider()
    provider.fail_next_call(count=2)
    with pytest.raises(ProviderUnavailableError):
        await provider.complete(_request())
    with pytest.raises(ProviderUnavailableError):
        await provider.complete(_request())
    # The third call succeeds again.
    response = await provider.complete(_request())
    assert response.content


async def test_fail_next_call_accepts_a_custom_error() -> None:
    provider = FakeLLMProvider()
    provider.fail_next_call(error=RuntimeError)
    with pytest.raises(RuntimeError):
        await provider.complete(_request())


def test_fail_next_call_rejects_zero_count() -> None:
    provider = FakeLLMProvider()
    with pytest.raises(ValueError):
        provider.fail_next_call(count=0)


async def test_queued_response_is_returned_exactly_once() -> None:
    provider = FakeLLMProvider()
    canned = _canned(content="canned", usage=TokenUsage(input_tokens=1, output_tokens=1))
    provider.set_next_response(canned)
    assert (await provider.complete(_request())).content == "canned"
    # The queue is drained: the next call returns the deterministic response.
    assert (await provider.complete(_request())).content != "canned"


def _canned(*, content: str, usage: TokenUsage) -> ProviderResponse:
    return ProviderResponse(
        model="fake-model-document.classify",
        content=content,
        usage=usage,
        latency_ms=1.0,
        finish_reason="stop",
    )
