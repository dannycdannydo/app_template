"""OpenAI-compatible chat-completions adapter base (v0.7 Scope §6.3).

OpenAI, DeepSeek, Azure OpenAI and local OpenAI-compatible servers (Ollama,
vLLM, SGLang) all speak the OpenAI chat-completions wire format, but they are
deliberately *not* interchangeable: each gets its own adapter class and
``provider_id`` so routing and capability declarations stay honest (Scope
§6.3, ADR-0017). This module implements the shared protocol mechanics — JSON
POST via :mod:`app.ai.providers.http_transport`, payload construction, usage
and finish-reason parsing — and each subclass overrides only its
provider-specific URL, headers, error surface and model echo behaviour.

Structured output: when ``ProviderRequest.output_schema`` is set the adapter
requests JSON mode (``response_format={"type": "json_object"}``) and appends
an explicit JSON instruction to the user message, which OpenAI JSON mode
requires; the full native JSON-schema path is a Scope §6.4 concern. The
adapter best-effort parses the JSON object; the service always validates
against the declared Pydantic contract.
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx

from app.ai.errors import ProviderResponseError
from app.ai.providers.base import LLMProvider, ProviderRequest, ProviderResponse
from app.ai.providers.http_transport import (
    FINISH_CONTENT_FILTER,
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_UNKNOWN,
    post_json,
    safe_int,
)
from app.ai.schemas import TokenUsage

# OpenAI's JSON mode requires the word "json" to appear in the messages; the
# rendered task prompt is a reviewed asset that must not depend on it, so the
# adapter appends this explicit instruction when structured output is asked.
_JSON_INSTRUCTION = "\n\nRespond with a single JSON object."


def _map_finish_reason(reason: Any) -> str:
    normalized = str(reason or "").lower()
    if normalized in ("stop", "end_turn", "stop_sequence"):
        return FINISH_STOP
    if normalized in ("length", "max_tokens"):
        return FINISH_LENGTH
    if normalized == "content_filter":
        return FINISH_CONTENT_FILTER
    return FINISH_UNKNOWN


def parse_structured_json(content: str) -> dict[str, Any] | None:
    """Best-effort JSON-object extraction from provider text output.

    Returns ``None`` when the content is empty or does not parse; the service
    runs the full extraction/validation path (Scope §6.4) before anything is
    accepted, so a ``None`` here is never treated as success.
    """
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)
    return None


class OpenAICompatibleAdapter(LLMProvider):
    """Shared chat-completions protocol implementation.

    Subclasses set ``provider_id``, ``default_base_url`` and override only the
    provider-specific seams: ``_chat_url``, ``_auth_headers``, ``_build_payload``
    and ``_response_model``.
    """

    provider_id = "openai"
    supports_structured_output = True
    default_base_url = "https://api.openai.com/v1"

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or self.default_base_url).rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def _chat_url(self, request: ProviderRequest) -> str:
        return f"{self._base_url}/chat/completions"

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    def _build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        content = request.prompt + _JSON_INSTRUCTION if request.output_schema else request.prompt
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "user", "content": content}],
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.output_schema:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _response_model(self, data: dict[str, Any], request: ProviderRequest) -> str:
        """The model identifier the provider reports having served.

        OpenAI/DeepSeek/local servers echo the requested model id; subclasses
        whose provider reports a different identifier (Azure deployments,
        Anthropic aliases) override this to return ``request.model``.
        """
        echoed = data.get("model")
        return str(echoed) if echoed else request.model

    def _parse_response(
        self,
        request: ProviderRequest,
        data: dict[str, Any],
        latency_ms: float,
    ) -> ProviderResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderResponseError("provider returned no usable choices")
        choice = cast(dict[str, Any], choices[0])
        message_raw = choice.get("message")
        content = ""
        if isinstance(message_raw, dict):
            content = str(cast(dict[str, Any], message_raw).get("content") or "")
        usage_raw = data.get("usage")
        if isinstance(usage_raw, dict):
            usage_data = cast(dict[str, Any], usage_raw)
            usage = TokenUsage(
                input_tokens=safe_int(usage_data.get("prompt_tokens")),
                output_tokens=safe_int(usage_data.get("completion_tokens")),
            )
        else:
            usage = TokenUsage(input_tokens=0, output_tokens=0)
        return ProviderResponse(
            model=self._response_model(data, request),
            content=content,
            structured=parse_structured_json(content) if request.output_schema else None,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=_map_finish_reason(choice.get("finish_reason")),
        )

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        data, latency_ms = await post_json(
            self._client,
            self._chat_url(request),
            headers=self._auth_headers(),
            payload=self._build_payload(request),
        )
        return self._parse_response(request, data, latency_ms)

    async def aclose(self) -> None:
        await self._client.aclose()
