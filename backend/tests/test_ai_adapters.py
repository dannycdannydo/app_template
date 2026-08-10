"""Provider adapter contract tests (v0.7 Scope §6.3, ADR-0017/ADR-0018).

Every real adapter satisfies the same ``LLMProvider`` contract: normalised
``ProviderResponse`` fields (model/content/structured/usage/latency/
finish_reason), the safe error taxonomy with correct retryability, and
provider-specific URL/header/payload construction. These tests exercise the
wire format with ``httpx.MockTransport`` so the default suite stays
provider-free (Scope §4); the opt-in ``ai_contracts`` tests in
``test_ai_contracts.py`` hit real providers with dedicated non-production
credentials. Repository-level checks for the Google boundary (no Gemini
Developer API / ``GEMINI_API_KEY``, ADR-0018) live here too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.ai.errors import (
    AIInputValidationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.ai.providers.anthropic import AnthropicAdapter
from app.ai.providers.azure_openai import AzureOpenAIAdapter
from app.ai.providers.base import ProviderRequest
from app.ai.providers.deepseek import DeepSeekAdapter
from app.ai.providers.local import LocalOpenAICompatibleAdapter
from app.ai.providers.openai import OpenAIAdapter
from app.ai.providers.vertex import VertexAIAdapter

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _request(**overrides: object) -> ProviderRequest:
    payload: dict[str, object] = {
        "task": "document.classify",
        "model": "gpt-4o-mini",
        "prompt": "Classify the supplied non-sensitive sample document.",
        "max_tokens": 256,
        "temperature": 0.0,
    }
    payload.update(overrides)
    return ProviderRequest.model_validate(payload)


def _canned_openai_response(*, content: str, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "model": "gpt-4o-mini",
    }


def _json_response(payload: dict[str, Any], *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://test"))


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- OpenAI ---


async def test_openai_contract_normalised_response() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_openai_response(content='{"category": "lease"}'))

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    response = await adapter.complete(_request(output_schema="demo.ClassificationResult"))

    assert adapter.provider_id == "openai"
    assert adapter.supports_structured_output is True
    assert len(captured) == 1
    sent = captured[0]
    assert sent.url == "https://api.openai.com/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer sk-test"
    body = json.loads(sent.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"].endswith("Respond with a single JSON object.")
    assert body["max_tokens"] == 256
    assert body["temperature"] == 0.0

    assert response.model == "gpt-4o-mini"
    assert response.structured == {"category": "lease"}
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5
    assert response.finish_reason == "stop"
    assert response.latency_ms >= 0


async def test_openai_usage_defaults_and_finish_reason_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]}
        )

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    response = await adapter.complete(_request())
    assert response.finish_reason == "content_filter"
    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 0
    assert response.content == ""


async def test_openai_error_mapping_and_no_content_leakage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"error": {"message": "super-secret-provider-detail"}}, status=429)

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    with pytest.raises(ProviderRateLimitError) as excinfo:
        await adapter.complete(_request())
    assert excinfo.value.retryable is True
    assert "super-secret-provider-detail" not in str(excinfo.value)


async def test_openai_transport_error_and_timeout_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    with pytest.raises(ProviderUnavailableError) as excinfo:
        await adapter.complete(_request())
    assert excinfo.value.retryable is True

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(timeout_handler))
    with pytest.raises(ProviderTimeoutError) as excinfo:
        await adapter.complete(_request())
    assert excinfo.value.retryable is True


async def test_openai_4xx_is_a_permanent_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"error": {"message": "bad request"}}, status=400)

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    with pytest.raises(ProviderResponseError) as excinfo:
        await adapter.complete(_request())
    assert excinfo.value.retryable is False


# --- DeepSeek ---


async def test_deepseek_is_its_own_adapter_with_its_own_endpoint() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_openai_response(content="plain text"))

    adapter = DeepSeekAdapter(api_key="ds-test", client=_client(handler))
    response = await adapter.complete(_request())

    assert adapter.provider_id == "deepseek"
    assert captured[0].url == "https://api.deepseek.com/chat/completions"
    assert captured[0].headers["authorization"] == "Bearer ds-test"
    assert response.finish_reason == "stop"


# --- Azure OpenAI ---


async def test_azure_uses_deployment_url_and_api_key_header() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            {"choices": [{"message": {"content": "ok"}}], "usage": {}, "model": "gpt-4o-mini"}
        )

    adapter = AzureOpenAIAdapter(
        endpoint="https://my-resource.openai.azure.com",
        api_key="az-test",
        api_version="2024-08-01-preview",
        client=_client(handler),
    )
    request = _request(model="gpt-4o-mini")
    response = await adapter.complete(request)

    sent = captured[0]
    assert (
        sent.url
        == "https://my-resource.openai.azure.com/openai/deployments/gpt-4o-mini/chat/completions"
        "?api-version=2024-08-01-preview"
    )
    assert sent.headers["api-key"] == "az-test"
    # The deployment name (the reviewed routing fact) is reported back, not
    # the underlying model id the body echoes.
    assert response.model == "gpt-4o-mini"


# --- Local OpenAI-compatible ---


async def test_local_adapter_contract_and_endpoint() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_openai_response(content="local answer"))

    adapter = LocalOpenAICompatibleAdapter(
        base_url="http://127.0.0.1:11434/v1", client=_client(handler)
    )
    response = await adapter.complete(_request())
    assert adapter.provider_id == "local"
    assert captured[0].url == "http://127.0.0.1:11434/v1/chat/completions"
    assert response.content == "local answer"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://ollama.example.com",
        "http://8.8.8.8",
        "http://vllm.internal.example.com",
        "",
        "ftp://127.0.0.1",
    ],
)
def test_local_adapter_rejects_unsafe_endpoints(endpoint: str) -> None:
    with pytest.raises(AIInputValidationError):
        LocalOpenAICompatibleAdapter(base_url=endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://ollama.example.com",
        "http://localhost:11434/v1",
        "http://10.0.0.5:8000/v1",
        "http://192.168.1.10:8000/v1",
        "http://172.16.5.5:8000/v1",
        "http://ollama:11434/v1",
        "http://vllm.local:8000/v1",
    ],
)
def test_local_adapter_accepts_safe_endpoints(endpoint: str) -> None:
    adapter = LocalOpenAICompatibleAdapter(base_url=endpoint)
    assert adapter.provider_id == "local"


# --- Anthropic ---


async def test_anthropic_contract_normalised_response() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            {
                "content": [{"type": "text", "text": '{"category": "lease"}'}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 4},
                "model": "claude-3-5-haiku-20241022",
            }
        )

    adapter = AnthropicAdapter(api_key="ant-test", client=_client(handler))
    response = await adapter.complete(_request(output_schema="demo.ClassificationResult"))

    sent = captured[0]
    assert sent.url == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == "ant-test"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(sent.content)
    assert body["max_tokens"] == 256
    assert body["messages"][0]["content"].endswith("Respond with a single JSON object.")

    assert response.model == "gpt-4o-mini"  # the routed alias is reported back
    assert response.structured == {"category": "lease"}
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 4
    assert response.finish_reason == "stop"


async def test_anthropic_overloaded_529_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"error": {"type": "overloaded_error"}}, status=529)

    adapter = AnthropicAdapter(api_key="ant-test", client=_client(handler))
    with pytest.raises(ProviderUnavailableError) as excinfo:
        await adapter.complete(_request())
    assert excinfo.value.retryable is True


# --- Vertex AI ---


class _FakeCredentials:
    """Token holder standing in for google-auth credentials (valid, no refresh)."""

    token = "test-vertex-token"
    valid = True


def _fake_google_auth(scopes: Any = None) -> tuple[Any, None]:
    """Stand-in for ``google.auth.default``: valid creds, no network."""
    return _FakeCredentials(), None


def _empty_response_handler(request: httpx.Request) -> httpx.Response:
    return _json_response({})


async def test_vertex_contract_uses_regional_endpoint_and_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []
    monkeypatch.setattr(
        "app.ai.providers.vertex.google_auth_default",
        _fake_google_auth,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": '{"category": "lease"}'}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 3},
            }
        )

    adapter = VertexAIAdapter(
        project="demo-project",
        location="europe-west1",
        client=_client(handler),
    )
    request = _request(model="gemini-2.0-flash", output_schema="demo.ClassificationResult")
    response = await adapter.complete(request)

    sent = captured[0]
    assert (
        sent.url
        == "https://europe-west1-aiplatform.googleapis.com/v1/projects/demo-project/locations/"
        "europe-west1/publishers/google/models/gemini-2.0-flash:generateContent"
    )
    assert sent.headers["authorization"] == "Bearer test-vertex-token"
    body = json.loads(sent.content)
    assert body["generationConfig"] == {
        "maxOutputTokens": 256,
        "temperature": 0.0,
        "responseMimeType": "application/json",
    }
    assert body["contents"][0]["parts"][0]["text"]

    assert response.model == "gemini-2.0-flash"
    assert response.structured == {"category": "lease"}
    assert response.usage.input_tokens == 9
    assert response.usage.output_tokens == 3
    assert response.finish_reason == "stop"


async def test_vertex_requires_project_and_location() -> None:
    with pytest.raises(AIInputValidationError, match="project"):
        VertexAIAdapter(
            project="", location="europe-west1", client=_client(_empty_response_handler)
        )
    with pytest.raises(AIInputValidationError, match="location"):
        VertexAIAdapter(project="demo", location="", client=_client(_empty_response_handler))


async def test_vertex_content_filter_finish_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.ai.providers.vertex.google_auth_default",
        _fake_google_auth,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"candidates": [{"content": {"parts": [{"text": ""}]}, "finishReason": "SAFETY"}]}
        )

    adapter = VertexAIAdapter(
        project="demo-project",
        location="us-central1",
        client=_client(handler),
    )
    response = await adapter.complete(_request(model="gemini-2.0-flash"))
    assert response.finish_reason == "content_filter"


# --- Google boundary (ADR-0018) ---


def test_no_gemini_developer_api_setting_exists() -> None:
    """No Gemini Developer API endpoint or key usage exists in app code.

    ADR-0018: Gemini is Vertex AI only. Prose in adapter docstrings documents
    that boundary and is allowed to name the key; what must never exist is an
    implementation reference: a developer-API endpoint URL or a
    ``GEMINI_API_KEY`` environment lookup anywhere in the application.
    """
    for path in sorted((BACKEND_ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "generativelanguage.googleapis.com" not in text, f"{path} uses the developer API"
        assert "ai.google.dev" not in text, f"{path} references Google AI Studio"
        for forbidden in (
            'os.getenv("GEMINI_API_KEY")',
            'os.environ["GEMINI_API_KEY"]',
            'os.environ.get("GEMINI_API_KEY")',
            "os.getenv('GEMINI_API_KEY')",
            "os.environ['GEMINI_API_KEY']",
            "os.environ.get('GEMINI_API_KEY')",
        ):
            assert forbidden not in text, f"{path} reads the Gemini Developer API key"


def test_settings_have_no_gemini_api_key_field() -> None:
    from app.core.config import Settings

    assert "gemini_api_key" not in Settings.model_fields
    assert "ai_gemini_api_key" not in Settings.model_fields
