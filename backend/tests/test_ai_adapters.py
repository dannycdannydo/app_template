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
from typing import Any, cast

import httpx
import pytest

from app.ai.attachments import ALLOWED_ATTACHMENT_MIME_TYPES, Attachment
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
from app.ai.staging import StagedFile

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
                "model": "claude-sonnet-4-6-20260219",
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
        "app.ai.providers._google_credentials.google_auth_default",
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
        "app.ai.providers._google_credentials.google_auth_default",
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


# --- v0.7 attachment amendment: truthful document-support declarations ---


def test_provider_document_support_declarations_are_truthful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapters declare the modalities they actually support (ADR-0017).

    OpenAI/Azure, Anthropic and Vertex now map bounded inline attachments to
    their native request forms (v0.7 Scope §6.3 attachment amendment), so they
    declare ``supports_documents=True``. DeepSeek rejects attachments and the
    local adapter has no reviewed document capability yet, so both must remain
    ``False``: the service/router refuse to dispatch attachments to them, and
    the shared payload builder fails an attachment-bearing request before
    dispatch rather than silently dropping the input.
    """
    monkeypatch.setattr(
        "app.ai.providers._google_credentials.google_auth_default", _fake_google_auth
    )
    assert DeepSeekAdapter(api_key="ds-test").supports_documents is False
    assert (
        LocalOpenAICompatibleAdapter(base_url="http://127.0.0.1:11434/v1").supports_documents
        is False
    )
    assert OpenAIAdapter(api_key="sk-test").supports_documents is True
    assert (
        AzureOpenAIAdapter(
            endpoint="https://my-resource.openai.azure.com",
            api_key="az-test",
            api_version="2024-08-01-preview",
        ).supports_documents
        is True
    )
    assert AnthropicAdapter(api_key="ant-test").supports_documents is True
    assert (
        VertexAIAdapter(
            project="demo-project",
            location="us-central1",
            client=_client(_empty_response_handler),
        ).supports_documents
        is True
    )
    # Truthful MIME capabilities (v0.7 Scope §6.3 attachment amendment): the
    # declared inline sets mirror what each native wire format can carry.
    assert OpenAIAdapter(api_key="sk-test").supported_attachment_mime_types == set(
        ALLOWED_ATTACHMENT_MIME_TYPES
    )
    assert AzureOpenAIAdapter(
        endpoint="https://my-resource.openai.azure.com",
        api_key="az-test",
        api_version="2024-08-01-preview",
    ).supported_attachment_mime_types == set(ALLOWED_ATTACHMENT_MIME_TYPES)
    assert AnthropicAdapter(api_key="ant-test").supported_attachment_mime_types == {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
    assert VertexAIAdapter(
        project="demo-project",
        location="us-central1",
        client=_client(_empty_response_handler),
    ).supported_attachment_mime_types == {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/plain",
    }
    assert DeepSeekAdapter(api_key="ds-test").supported_attachment_mime_types == set()
    assert (
        LocalOpenAICompatibleAdapter(
            base_url="http://127.0.0.1:11434/v1"
        ).supported_attachment_mime_types
        == set()
    )


def test_provider_request_carries_bounded_attachments() -> None:
    from app.ai.attachments import Attachment
    from app.ai.providers.base import ProviderRequest

    request = _request()
    assert request.attachments == []

    attachment = Attachment(
        display_name="lease.pdf", mime_type="application/pdf", content=b"%PDF-1.7 fixture"
    )
    with_attachments = ProviderRequest(
        task=request.task,
        model=request.model,
        prompt=request.prompt,
        attachments=[attachment],
    )
    assert with_attachments.attachments == [attachment]
    assert with_attachments.attachments[0].sha256_digest


# --- v0.7 attachment amendment: native inline mappings ---


def _attachment(*, display_name: str, mime_type: str, content: bytes | None = None) -> Attachment:
    return Attachment(
        display_name=display_name,
        mime_type=mime_type,
        content=content or b"fixture-bytes",
    )


def _content_parts(content: Any) -> list[dict[str, Any]]:
    assert isinstance(content, list), "attachment-bearing requests use content parts"
    return cast(list[dict[str, Any]], content)


async def test_openai_maps_attachments_to_native_inline_parts() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_openai_response(content='{"category": "lease"}'))

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    response = await adapter.complete(
        _request(
            output_schema="demo.ClassificationResult",
            attachments=[
                _attachment(display_name="lease.pdf", mime_type="application/pdf"),
                _attachment(
                    display_name="page.png",
                    mime_type="image/png",
                    content=b"\x89PNG\r\n\x1a\nfixture",
                ),
            ],
        )
    )
    sent = captured[0]
    body = json.loads(sent.content)
    parts = _content_parts(body["messages"][0]["content"])
    assert parts[0]["type"] == "text"
    assert parts[0]["text"].endswith("Respond with a single JSON object.")

    document = parts[1]
    assert document["type"] == "file"
    assert document["file"]["filename"] == "lease.pdf"
    assert document["file"]["file_data"].startswith("data:application/pdf;base64,")

    image = parts[2]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/png;base64,")

    serialized = json.dumps(body)
    # Inline-only: no signed URL, storage reference or credential in the payload.
    assert "https://" not in serialized and "http://" not in serialized
    assert "storage" not in serialized and "signed" not in serialized
    assert response.structured == {"category": "lease"}


async def test_openai_maps_text_document_attachments_to_file_parts() -> None:
    """OpenAI's file part carries plain text too (the file-inputs contract), so
    the full template allowlist is representable across the two part kinds."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_openai_response(content='{"category": "lease"}'))

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    await adapter.complete(
        _request(
            output_schema="demo.ClassificationResult",
            attachments=[_attachment(display_name="notes.md", mime_type="text/markdown")],
        )
    )
    parts = _content_parts(json.loads(captured[0].content)["messages"][0]["content"])
    assert parts[1]["type"] == "file"
    assert parts[1]["file"]["filename"] == "notes.md"
    assert parts[1]["file"]["file_data"].startswith("data:text/markdown;base64,")


async def test_azure_maps_attachments_to_native_inline_parts() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_openai_response(content='{"category": "lease"}'))

    adapter = AzureOpenAIAdapter(
        endpoint="https://my-resource.openai.azure.com",
        api_key="az-test",
        api_version="2024-08-01-preview",
        client=_client(handler),
    )
    await adapter.complete(
        _request(
            model="gpt-4o-mini",
            output_schema="demo.ClassificationResult",
            attachments=[
                _attachment(
                    display_name="lease.pdf",
                    mime_type="application/pdf",
                ),
                _attachment(
                    display_name="page.png",
                    mime_type="image/png",
                    content=b"\x89PNG\r\n\x1a\nfixture",
                ),
            ],
        )
    )
    sent = captured[0]
    assert (
        sent.url == "https://my-resource.openai.azure.com/openai/deployments/gpt-4o-mini/chat/"
        "completions?api-version=2024-08-01-preview"
    )
    parts = _content_parts(json.loads(sent.content)["messages"][0]["content"])
    # Azure serves the same chat-completions wire format: documents as
    # ``type=file`` parts and images as ``image_url`` data-URI parts.
    assert parts[1]["type"] == "file"
    assert parts[1]["file"]["filename"] == "lease.pdf"
    assert parts[1]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert parts[2]["type"] == "image_url"
    assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_anthropic_maps_attachments_to_native_base64_blocks() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            {
                "content": [{"type": "text", "text": '{"category": "lease"}'}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 4},
                "model": "claude-sonnet-4-6-20260219",
            }
        )

    adapter = AnthropicAdapter(api_key="ant-test", client=_client(handler))
    await adapter.complete(
        _request(
            output_schema="demo.ClassificationResult",
            attachments=[
                _attachment(display_name="lease.pdf", mime_type="application/pdf"),
                _attachment(
                    display_name="page.png",
                    mime_type="image/png",
                    content=b"\x89PNG\r\n\x1a\nfixture",
                ),
            ],
        )
    )
    sent = captured[0]
    body = json.loads(sent.content)
    blocks = _content_parts(body["messages"][0]["content"])
    assert blocks[0]["type"] == "text"
    document = blocks[1]
    assert document["type"] == "document"
    assert document["title"] == "lease.pdf"
    assert document["source"]["type"] == "base64"
    assert document["source"]["media_type"] == "application/pdf"
    assert document["source"]["data"]
    image = blocks[2]
    assert image["type"] == "image"
    assert image["source"]["media_type"] == "image/png"
    assert image["source"]["data"]


async def test_vertex_maps_attachments_to_native_inline_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []
    monkeypatch.setattr(
        "app.ai.providers._google_credentials.google_auth_default",
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
    await adapter.complete(
        _request(
            model="gemini-2.0-flash",
            output_schema="demo.ClassificationResult",
            attachments=[_attachment(display_name="lease.pdf", mime_type="application/pdf")],
        )
    )
    sent = captured[0]
    body = json.loads(sent.content)
    parts = body["contents"][0]["parts"]
    assert parts[0]["text"]
    inline = parts[1]["inlineData"]
    assert inline["mimeType"] == "application/pdf"
    assert inline["data"]
    serialized = json.dumps(body)
    assert "https://" not in serialized and "signed" not in serialized


async def test_vertex_maps_text_plain_attachments_to_inline_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex inlineData carries plain text documents too (the blob contract),
    so text/plain is part of the declared MIME set."""
    captured: list[httpx.Request] = []
    monkeypatch.setattr(
        "app.ai.providers._google_credentials.google_auth_default",
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
    await adapter.complete(
        _request(
            model="gemini-2.0-flash",
            output_schema="demo.ClassificationResult",
            attachments=[_attachment(display_name="notes.txt", mime_type="text/plain")],
        )
    )
    parts = json.loads(captured[0].content)["contents"][0]["parts"]
    assert parts[1]["inlineData"]["mimeType"] == "text/plain"
    assert parts[1]["inlineData"]["data"]


async def test_deepseek_and_local_reject_attachments_before_dispatch() -> None:
    from app.ai.errors import AIInputValidationError

    deepseek = DeepSeekAdapter(api_key="ds-test", client=_client(_empty_response_handler))
    with pytest.raises(AIInputValidationError, match="does not support document"):
        await deepseek.complete(
            _request(
                attachments=[_attachment(display_name="lease.pdf", mime_type="application/pdf")]
            )
        )

    local = LocalOpenAICompatibleAdapter(
        base_url="http://127.0.0.1:11434/v1", client=_client(_empty_response_handler)
    )
    with pytest.raises(AIInputValidationError, match="does not support document"):
        await local.complete(
            _request(
                attachments=[_attachment(display_name="lease.pdf", mime_type="application/pdf")]
            )
        )


async def test_anthropic_rejects_plain_text_attachments_before_dispatch() -> None:
    """A MIME type the template allowlist permits globally but Anthropic's
    base64 document source cannot carry (PDF only) fails before dispatch,
    instead of reaching the provider as an invalid document block (v0.7 Scope
    §6.3 attachment amendment)."""
    adapter = AnthropicAdapter(api_key="ant-test", client=_client(_empty_response_handler))
    for mime_type in ("text/plain", "text/csv", "text/markdown", "application/json"):
        with pytest.raises(AIInputValidationError, match="does not support attachment MIME"):
            await adapter.complete(
                _request(attachments=[_attachment(display_name="notes", mime_type=mime_type)])
            )
    # Images and PDF remain supported by the same guard.
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model": "claude-sonnet-4-6-20260219",
            }
        )

    await AnthropicAdapter(api_key="ant-test", client=_client(handler)).complete(
        _request(attachments=[_attachment(display_name="lease.pdf", mime_type="application/pdf")])
    )
    assert len(captured) == 1


async def test_vertex_rejects_mime_types_outside_inline_data_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex inlineData carries images, PDF and plain text; CSV/Markdown/JSON
    are outside that contract and fail before dispatch (v0.7 Scope §6.3
    attachment amendment)."""
    monkeypatch.setattr(
        "app.ai.providers._google_credentials.google_auth_default", _fake_google_auth
    )
    adapter = VertexAIAdapter(
        project="demo-project",
        location="europe-west1",
        client=_client(_empty_response_handler),
    )
    for mime_type in ("text/csv", "text/markdown", "application/json"):
        with pytest.raises(AIInputValidationError, match="does not support attachment MIME"):
            await adapter.complete(
                _request(
                    model="gemini-2.0-flash",
                    attachments=[_attachment(display_name="notes", mime_type=mime_type)],
                )
            )


# --- v0.7 regional amendment: configured region reporting and headers ---


async def test_openai_region_routes_through_the_regional_endpoint() -> None:
    """A validated region selects the regional domain; the request must never
    stay on the global endpoint while being labelled regional (v0.7 Scope §6.3
    regional amendment)."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_openai_response(content="ok"))

    adapter = OpenAIAdapter(api_key="sk-test", region="eu", client=_client(handler))
    response = await adapter.complete(_request())
    assert captured[0].url == "https://eu.api.openai.com/v1/chat/completions"
    assert response.region == "eu"


def test_openai_region_conflicts_with_base_url_override() -> None:
    """A regional label with a global (or foreign) endpoint is a configuration
    conflict: the adapter fails fast instead of mislabelling traffic."""
    with pytest.raises(AIInputValidationError, match="conflicts with region"):
        OpenAIAdapter(api_key="sk-test", region="eu", base_url="https://api.openai.com/v1")
    with pytest.raises(AIInputValidationError, match="conflicts with region"):
        OpenAIAdapter(api_key="sk-test", region="us", base_url="https://eu.api.openai.com/v1")
    # A matching regional override is fine and still reports the region.
    adapter = OpenAIAdapter(api_key="sk-test", region="eu", base_url="https://eu.api.openai.com/v1")
    assert adapter.region == "eu"


async def test_anthropic_sends_inference_geo_field_and_reports_observed_region() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "inference_geo": "us",
                },
                "model": "claude-sonnet-4-6-20260219",
            }
        )

    adapter = AnthropicAdapter(
        api_key="ant-test",
        inference_geography="us",
        client=_client(handler),
    )
    response = await adapter.complete(_request())
    body = json.loads(captured[0].content)
    # inference_geo is a top-level request field, never a header.
    assert "inference_geo" not in captured[0].headers
    assert body["inference_geo"] == "us"
    # The response reports where inference actually ran; the adapter prefers
    # that observed fact over the configured geography for routing metadata.
    assert response.region == "us"


async def test_anthropic_reports_configured_region_when_usage_omits_it() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model": "claude-sonnet-4-6-20260219",
            }
        )

    adapter = AnthropicAdapter(
        api_key="ant-test",
        inference_geography="global",
        client=_client(handler),
    )
    response = await adapter.complete(_request())
    assert json.loads(captured[0].content)["inference_geo"] == "global"
    assert response.region == "global"


async def test_anthropic_default_sends_no_inference_geo_field() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model": "claude-sonnet-4-6-20260219",
            }
        )

    adapter = AnthropicAdapter(api_key="ant-test", client=_client(handler))
    await adapter.complete(_request())
    body = json.loads(captured[0].content)
    assert "inference_geo" not in body
    assert "anthropic-region" not in captured[0].headers


def test_vertex_and_azure_regions_are_derived_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.ai.providers._google_credentials.google_auth_default", _fake_google_auth
    )
    vertex = VertexAIAdapter(
        project="demo-project",
        location="europe-west1",
        client=_client(_empty_response_handler),
    )
    assert vertex.region == "europe-west1"
    azure = AzureOpenAIAdapter(
        endpoint="https://my-resource.openai.azure.com",
        api_key="az-test",
        api_version="2024-08-01-preview",
    )
    assert azure.region == ""  # region is inherent in the endpoint, not a setting
    assert OpenAIAdapter(api_key="sk-test").region == ""
    assert AnthropicAdapter(api_key="ant-test").region == ""
    assert DeepSeekAdapter(api_key="ds-test").region == ""
    assert LocalOpenAICompatibleAdapter(base_url="http://127.0.0.1:11434/v1").region == ""


# --- v0.8 §6.5 OpenAI staged-file Responses-API dispatch ----------------------
#
# A non-inline transfer hands the OpenAI adapter a StagedFile; OpenAI's native
# file-input contract is the Responses API ``input_file`` item — a provider
# file id (provider_upload) or a just-in-time managed download URL for a
# retained private S3 source (managed_signed_url). The adapter switches the
# whole dispatch to POST /responses; every other request keeps chat
# completions. The managed URL is a one-dispatch bearer capability that is
# never returned, persisted, audited or logged (BP §28).


def _canned_responses_response(
    *,
    content: str,
    usage: dict[str, int] | None = None,
    **overrides: object,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "resp-1",
        "object": "response",
        "model": "gpt-4o-mini",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": content}],
            }
        ],
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }
    data.update(overrides)
    return data


def _staged_request(**overrides: object) -> ProviderRequest:
    payload: dict[str, object] = {
        "task": "document.classify",
        "model": "gpt-4o-mini",
        "prompt": "Classify the supplied non-sensitive sample document.",
        "max_tokens": 256,
        "temperature": 0.0,
        "staged_file": StagedFile(
            external_id="file-abc123",
            mime_type="application/pdf",
        ),
    }
    payload.update(overrides)
    return ProviderRequest.model_validate(payload)


async def test_openai_staged_file_id_dispatches_via_responses_api() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_responses_response(content='{"category": "lease"}'))

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    response = await adapter.complete(_staged_request(output_schema="demo.ClassificationResult"))

    assert len(captured) == 1
    sent = captured[0]
    assert sent.url == "https://api.openai.com/v1/responses"
    assert sent.headers["authorization"] == "Bearer sk-test"
    body = json.loads(sent.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["max_output_tokens"] == 256
    assert body["temperature"] == 0.0
    message = body["input"][0]
    assert message["role"] == "user"
    parts = _content_parts(message["content"])
    assert parts[0]["type"] == "input_text"
    assert parts[0]["text"].endswith("Respond with a single JSON object.")
    assert parts[1] == {"type": "input_file", "file_id": "file-abc123"}

    assert response.model == "gpt-4o-mini"
    assert response.structured == {"category": "lease"}
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5
    assert response.finish_reason == "stop"
    assert response.latency_ms >= 0


async def test_openai_staged_file_managed_url_dispatches_input_file_url() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_responses_response(content='{"category": "lease"}'))

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    managed_url = (
        "https://minio.example.test/org-bucket/lease.pdf?"
        "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=secret-bearer-material"
    )
    response = await adapter.complete(
        _staged_request(
            staged_file=StagedFile(
                external_id="organisations/00000000-0000-0000-0000-000000000000/lease.pdf",
                mime_type="application/pdf",
            ),
            managed_url=managed_url,
        )
    )
    body = json.loads(captured[0].content)
    parts = _content_parts(body["input"][0]["content"])
    assert parts[1] == {"type": "input_file", "file_url": managed_url}
    # The external id (immutable source identity) is never sent to the provider.
    assert parts[1].get("file_id") is None
    assert response.finish_reason == "stop"


async def test_openai_reports_routed_model_for_dated_snapshot_echo() -> None:
    """OpenAI echoes a dated snapshot id (``gpt-4o-mini-2024-07-18``) for a
    requested base model; the adapter reports the routed id so the service's
    routing/accounting check never rejects a legitimate dispatch."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            _canned_responses_response(
                content='{"category": "lease"}', model="gpt-4o-mini-2024-07-18"
            )
        )

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    response = await adapter.complete(_staged_request())

    assert response.model == "gpt-4o-mini"


async def test_openai_reports_unrelated_echoed_model() -> None:
    """A provider echoing a genuinely different model is not normalised away —
    the service's mismatch guard must still be able to reject it."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_responses_response(content="ok", model="gpt-4o"))

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    response = await adapter.complete(_staged_request())

    assert response.model == "gpt-4o"


async def test_openai_retains_non_snapshot_model_suffix() -> None:
    """Only the reviewed dated snapshot shape (``-YYYY-MM-DD``) is normalised
    to the routed id; an arbitrary suffix is retained so a provider model
    mismatch in accounting/routing metadata is never hidden."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(
            _canned_responses_response(content="ok", model="gpt-4o-mini-2026-02-13-extra")
        )

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    response = await adapter.complete(_staged_request())

    assert response.model == "gpt-4o-mini-2026-02-13-extra"


async def test_openai_managed_url_never_leaks_into_errors_or_logs() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response({"error": {"message": "boom"}}, status=429)

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    managed_url = "https://minio.example.test/lease.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=secret"
    with pytest.raises(ProviderRateLimitError) as excinfo:
        await adapter.complete(
            _staged_request(
                staged_file=StagedFile(
                    external_id="organisations/00000000-0000-0000-0000-000000000000/lease.pdf",
                    mime_type="application/pdf",
                ),
                managed_url=managed_url,
            )
        )
    assert excinfo.value.retryable is True
    assert "X-Amz-Signature" not in str(excinfo.value)
    assert "minio" not in str(excinfo.value)


async def test_openai_staged_file_and_attachments_are_mutually_exclusive() -> None:
    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(AIInputValidationError):
        await adapter.complete(
            _staged_request(
                attachments=[_attachment(display_name="lease.pdf", mime_type="application/pdf")]
            )
        )


async def test_openai_managed_url_alone_dispatches_input_file_url() -> None:
    """A managed URL alone is a valid Responses-API dispatch (the retained
    managed-signed-url source), carrying ``input_file.file_url`` (Scope §2.3)."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_responses_response(content='{"category": "lease"}'))

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    managed_url = "https://minio.example.test/lease.pdf?X-Amz-Signature=x"
    response = await adapter.complete(
        _request(
            managed_url=managed_url,
        )
    )
    body = json.loads(captured[0].content)
    parts = _content_parts(body["input"][0]["content"])
    assert parts[1] == {"type": "input_file", "file_url": managed_url}
    assert response.finish_reason == "stop"


@pytest.mark.parametrize(
    "external_id",
    ["https://example.test/lease.pdf", "gs://bucket/obj", "s3://bucket/obj"],
)
async def test_openai_staged_file_with_url_shaped_external_id_is_rejected(
    external_id: str,
) -> None:
    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(AIInputValidationError):
        await adapter.complete(
            _staged_request(
                staged_file=StagedFile(external_id=external_id, mime_type="application/pdf"),
            )
        )


async def test_openai_responses_native_json_schema_structured_output() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_responses_response(content='{"category": "lease"}'))

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    schema = {
        "type": "object",
        "properties": {"category": {"type": "string"}},
        "required": ["category"],
    }
    await adapter.complete(
        _staged_request(output_schema="demo.ClassificationResult", output_json_schema=schema)
    )
    body = json.loads(captured[0].content)
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "structured_output",
        "schema": schema,
        "strict": False,
    }


async def test_openai_responses_json_mode_appends_instruction() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_canned_responses_response(content='{"category": "lease"}'))

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    await adapter.complete(_staged_request(output_schema="demo.ClassificationResult"))
    body = json.loads(captured[0].content)
    assert body["text"] == {"format": {"type": "json_object"}}
    assert body["input"][0]["content"][0]["text"].endswith("Respond with a single JSON object.")


async def test_openai_responses_finish_reason_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            _canned_responses_response(
                content="partial", incomplete_details={"reason": "max_output_tokens"}
            )
        )

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(handler))
    response = await adapter.complete(_staged_request())
    assert response.finish_reason == "length"

    def refusal_handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            _canned_responses_response(
                content="", output=[{"type": "refusal", "refusal": "refused"}]
            )
        )

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(refusal_handler))
    response = await adapter.complete(_staged_request())
    assert response.finish_reason == "content_filter"


async def test_openai_responses_error_mapping_is_safe() -> None:
    def rate_limited(request: httpx.Request) -> httpx.Response:
        return _json_response({"error": {"message": "secret"}}, status=429)

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(rate_limited))
    with pytest.raises(ProviderRateLimitError) as excinfo:
        await adapter.complete(_staged_request())
    assert excinfo.value.retryable is True
    assert "secret" not in str(excinfo.value)

    def server_error(request: httpx.Request) -> httpx.Response:
        return _json_response({}, status=503)

    adapter = OpenAIAdapter(api_key="sk-test", client=_client(server_error))
    with pytest.raises(ProviderUnavailableError) as excinfo:
        await adapter.complete(_staged_request())
    assert excinfo.value.retryable is True


async def test_openai_compatible_adapters_fail_closed_on_staged_file() -> None:
    """Azure, DeepSeek and local share the chat-completions base and have no
    v0.8 staged-file path; they must fail before dispatch (Scope §2.4)."""
    for adapter in (
        AzureOpenAIAdapter(
            endpoint="https://my-resource.openai.azure.com",
            api_key="az-test",
            api_version="2024-08-01-preview",
            client=_client(_empty_response_handler),
        ),
        DeepSeekAdapter(api_key="ds-test", client=_client(_empty_response_handler)),
        LocalOpenAICompatibleAdapter(
            base_url="http://127.0.0.1:11434/v1", client=_client(_empty_response_handler)
        ),
    ):
        with pytest.raises(AIInputValidationError):
            await adapter.complete(_staged_request())
        await adapter.aclose()
