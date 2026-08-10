"""Anthropic Claude provider adapter (v0.7 Scope §6.3, ADR-0017).

Claude is reached through the Anthropic Messages API (``/v1/messages``) with
the ``x-api-key`` and ``anthropic-version`` headers. The Anthropic SDK is
deliberately not a dependency; this adapter is a pinned HTTP REST client, so
the fake-provider default and the opt-in contract tests share one wire format
and the import boundary stays airtight (BP §33). Anthropic-specific quirks
live here: the required ``max_tokens`` field, block-based content responses,
``stop_reason`` values and the overloaded-529 error code (retryable).
"""

from __future__ import annotations

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
)
from app.ai.providers.openai_compatible import parse_structured_json
from app.ai.schemas import TokenUsage

# Bounded default for Anthropic's required max_tokens field when the task
# does not declare one; the service normally passes the task's parameter
# default (Scope §6.2).
_DEFAULT_MAX_TOKENS = 1024
_API_VERSION = "2023-06-01"
# Anthropic reports "overloaded" as HTTP 529, a transient retryable state.
_UNAVAILABLE_STATUSES = frozenset({500, 502, 503, 504, 529})
_JSON_INSTRUCTION = "\n\nRespond with a single JSON object."


class AnthropicAdapter(LLMProvider):
    """Anthropic Messages API adapter."""

    provider_id = "anthropic"
    supports_structured_output = True
    default_base_url = "https://api.anthropic.com"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or self.default_base_url).rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def _auth_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
        }

    def _build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        content = request.prompt + _JSON_INSTRUCTION if request.output_schema else request.prompt
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens or _DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": content}],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    def _response_model(self, data: dict[str, Any], request: ProviderRequest) -> str:
        # Anthropic echoes the concrete model id (e.g. "claude-3-5-haiku-20241022")
        # even when the request used an alias; the alias from the registry is
        # the reviewed routing fact and is reported as such.
        return request.model

    def _parse_response(
        self,
        request: ProviderRequest,
        data: dict[str, Any],
        latency_ms: float,
    ) -> ProviderResponse:
        content_blocks = data.get("content")
        if not isinstance(content_blocks, list):
            raise ProviderResponseError("provider returned no usable content blocks")
        text_parts: list[str] = []
        for block in content_blocks:  # pyright: ignore[reportUnknownVariableType]
            block_dict = cast(dict[str, Any], block)  # pyright: ignore[reportUnknownVariableType]
            if block_dict.get("type") == "text":
                text_parts.append(str(block_dict.get("text") or ""))
        text = "".join(text_parts)
        usage_raw = data.get("usage")
        if isinstance(usage_raw, dict):
            usage_data = cast(dict[str, Any], usage_raw)
            usage = TokenUsage(
                input_tokens=int(usage_data.get("input_tokens") or 0),
                output_tokens=int(usage_data.get("output_tokens") or 0),
            )
        else:
            usage = TokenUsage(input_tokens=0, output_tokens=0)
        stop_reason = str(data.get("stop_reason") or "").lower()
        if stop_reason in ("end_turn", "stop_sequence"):
            finish_reason = FINISH_STOP
        elif stop_reason == "max_tokens":
            finish_reason = FINISH_LENGTH
        elif stop_reason == "refusal":
            finish_reason = FINISH_CONTENT_FILTER
        else:
            finish_reason = FINISH_UNKNOWN
        return ProviderResponse(
            model=self._response_model(data, request),
            content=text,
            structured=parse_structured_json(text) if request.output_schema else None,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        data, latency_ms = await post_json(
            self._client,
            f"{self._base_url}/v1/messages",
            headers=self._auth_headers(),
            payload=self._build_payload(request),
            unavailable_statuses=_UNAVAILABLE_STATUSES,
        )
        return self._parse_response(request, data, latency_ms)

    async def aclose(self) -> None:
        await self._client.aclose()
