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

import base64
from typing import Any, cast

import httpx

from app.ai.attachments import ANTHROPIC_INLINE_ATTACHMENT_MIME_TYPES, Attachment
from app.ai.errors import AIInputValidationError, ProviderResponseError
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
# Anthropic inference geography is a top-level ``inference_geo`` field on
# ``POST /v1/messages`` (never a header); only ``global`` and ``us`` exist and
# only Claude 4.6+ models accept the field, so the registry pins a compatible
# model (v0.7 Scope §6.3 regional amendment; official contract:
# https://platform.claude.com/docs/en/manage-claude/data-residency).


# Native inline attachment blocks (v0.7 Scope §6.3 attachment amendment,
# ADR-0017): images map to base64 ``image`` blocks and documents to base64
# ``document`` blocks. Anthropic's base64 ``document`` source carries PDF
# only; plain-text formats need a different representation and are rejected
# before dispatch by the adapter's MIME guard (official contract:
# https://platform.claude.com/docs/en/build-with-claude/pdf-support). Bytes
# stay in-memory and never reference storage credentials, signed URLs or
# object paths.
def _anthropic_attachment_blocks(attachments: list[Attachment]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for attachment in attachments:
        encoded = base64.b64encode(attachment.content).decode("ascii")
        if attachment.is_image:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": attachment.mime_type,
                        "data": encoded,
                    },
                }
            )
        else:
            blocks.append(
                {
                    "type": "document",
                    "title": attachment.display_name,
                    "source": {
                        "type": "base64",
                        "media_type": attachment.mime_type,
                        "data": encoded,
                    },
                }
            )
    return blocks


class AnthropicAdapter(LLMProvider):
    """Anthropic Messages API adapter."""

    provider_id = "anthropic"
    supports_structured_output = True
    supports_documents = True
    supported_attachment_mime_types = ANTHROPIC_INLINE_ATTACHMENT_MIME_TYPES
    default_base_url = "https://api.anthropic.com"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        inference_geography: str = "",
    ) -> None:
        self._base_url = (base_url or self.default_base_url).rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        # Validated inference geography (v0.7 Scope §6.3 regional amendment);
        # empty means the provider default (global) and sends no field.
        self.region = inference_geography

    def _auth_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
        }

    def _build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        if request.attachments:
            # Pre-dispatch MIME guard (v0.7 Scope §6.3 attachment amendment):
            # Anthropic's base64 document source carries PDF only, so a text
            # attachment that the template allowlist permits globally must
            # fail here before any HTTP dispatch.
            unsupported = sorted(
                {
                    attachment.mime_type
                    for attachment in request.attachments
                    if attachment.mime_type not in self.supported_attachment_mime_types
                }
            )
            if unsupported:
                raise AIInputValidationError(
                    f"provider {self.provider_id!r} does not support attachment MIME "
                    f"type(s): {', '.join(unsupported)}"
                )
        text = request.prompt + _JSON_INSTRUCTION if request.output_schema else request.prompt
        if request.attachments:
            content: Any = [
                {"type": "text", "text": text},
                *_anthropic_attachment_blocks(list(request.attachments)),
            ]
        else:
            content = text
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens or _DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": content}],
        }
        if self.region:
            # Top-level field, not a header (official contract): requests
            # must use a Claude 4.6+ model, which the registry pins.
            payload["inference_geo"] = self.region
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    def _response_model(self, data: dict[str, Any], request: ProviderRequest) -> str:
        # Anthropic echoes the concrete model id (e.g. "claude-sonnet-4-6-2026…")
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
            # The response reports where inference actually ran; prefer that
            # observed fact over the configured geography for routing metadata
            # (v0.7 Scope §6.3 regional amendment).
            reported_region = str(usage_data.get("inference_geo") or "")
        else:
            usage = TokenUsage(input_tokens=0, output_tokens=0)
            reported_region = ""
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
            region=reported_region or self.region,
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
