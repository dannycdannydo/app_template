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
requests structured JSON output. Adapters that truthfully declare
``supports_native_structured_output`` (OpenAI, and Azure deployments pinned to
an api-version at or after ``2024-08-01-preview``, using the JSON Schema the
service generated from the Pydantic output model) request OpenAI's native
``response_format={"type": "json_schema", ...}`` (Scope §6.4); all others
(DeepSeek, local, older Azure api-versions) fall back to JSON mode
(``response_format={"type": "json_object"}``) plus an explicit JSON
instruction to the user message, which OpenAI JSON mode requires. The
adapter best-effort parses the JSON object; the service always validates
against the declared Pydantic contract.
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

import httpx

from app.ai.attachments import Attachment
from app.ai.errors import AIInputValidationError, ProviderResponseError
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
JSON_INSTRUCTION = "\n\nRespond with a single JSON object."
# Native JSON-schema structured output name (Scope §6.4). ``strict=False`` is
# deliberate: the service generates the schema from the feature's Pydantic
# model, which may contain optional fields a strict subset would reject, and
# the service always re-validates the provider output against that model, so
# the schema is a strong shape hint rather than the safety boundary.
NATIVE_OUTPUT_NAME = "structured_output"


# Native inline attachment forms (v0.7 Scope §6.3 attachment amendment,
# ADR-0017). OpenAI/Azure chat-completions accept images as data-URI
# ``image_url`` parts and documents as ``type=file`` parts carrying a
# data-URI ``file_data`` string; both stay in-memory base64 and never
# reference storage credentials, signed URLs or object paths. ``input_file``
# is the Responses API form and is deliberately not used here (official
# contract: https://developers.openai.com/api/docs/guides/file-inputs).
def _openai_attachment_parts(
    attachments: list[Attachment],
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for attachment in attachments:
        encoded = base64.b64encode(attachment.content).decode("ascii")
        if attachment.is_image:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{attachment.mime_type};base64,{encoded}"},
                }
            )
        else:
            parts.append(
                {
                    "type": "file",
                    "file": {
                        "filename": attachment.display_name,
                        "file_data": f"data:{attachment.mime_type};base64,{encoded}",
                    },
                }
            )
    return parts


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
        region: str = "",
    ) -> None:
        self._base_url = (base_url or self.default_base_url).rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        # Configured deployment region (v0.7 Scope §6.3 regional amendment);
        # reported in ``ProviderResponse.region`` for routing metadata.
        self.region = region

    def _chat_url(self, request: ProviderRequest) -> str:
        return f"{self._base_url}/chat/completions"

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    def _build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        if request.attachments and not self.supports_documents:
            # Defense in depth: the service/router already reject attachments
            # for adapters without document support (Scope §6.3), but a directly
            # constructed adapter must fail before dispatch rather than silently
            # drop the input (ADR-0017 "no fake interchangeability").
            raise AIInputValidationError(
                f"provider {self.provider_id!r} does not support document attachments"
            )
        if request.staged_file is not None or request.managed_url is not None:
            # Defense in depth: the chat-completions wire format carried by
            # this adapter base (OpenAI-compatible servers, DeepSeek, Azure,
            # local) has no v0.8 staged-file path. The OpenAI adapter routes a
            # staged file to the Responses API itself; every other adapter must
            # fail before dispatch rather than silently drop the input (Scope
            # §2.4 fail-closed matrix).
            raise AIInputValidationError(
                f"provider {self.provider_id!r} does not support staged file inputs"
            )
        if request.attachments:
            # Pre-dispatch MIME guard (v0.7 Scope §6.3 attachment amendment):
            # even a directly constructed adapter fails closed on a MIME type
            # its native wire format cannot carry, instead of sending an
            # invalid part to the provider.
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
        content: str | list[dict[str, Any]]
        if request.attachments:
            content = [
                {
                    "type": "text",
                    "text": request.prompt + (JSON_INSTRUCTION if request.output_schema else ""),
                },
                *_openai_attachment_parts(list(request.attachments)),
            ]
        else:
            content = request.prompt + JSON_INSTRUCTION if request.output_schema else request.prompt
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "user", "content": content}],
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.output_schema:
            # Native JSON-schema structured output when the adapter truthfully
            # supports it and the service supplied the generated schema (Scope
            # §6.4); otherwise the JSON-mode prompt contract. The service
            # re-validates against the Pydantic model either way.
            if self.supports_native_structured_output and request.output_json_schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": NATIVE_OUTPUT_NAME,
                        "schema": request.output_json_schema,
                        "strict": False,
                    },
                }
            else:
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
            region=self.region,
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
