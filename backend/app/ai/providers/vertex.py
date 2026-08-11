"""Google Vertex AI Gemini provider adapter (v0.7 Scope §6.3, ADR-0018).

Gemini is reached **exclusively** through the Vertex AI API: the regional
``generateContent`` REST endpoint for the configured Google Cloud project and
location, authenticated with Application Default Credentials (workload
identity on managed platforms, ``GOOGLE_APPLICATION_CREDENTIALS`` locally) or
an explicit service-account key file supplied through the approved deployment
secret mechanism. There is deliberately no ``GEMINI_API_KEY`` setting, no
Google AI Studio / Gemini Developer API endpoint and no browser-facing
credential anywhere (ADR-0018; a repository test asserts the absence).

The google-auth SDK is imported only in this module (BP §33); token refresh
uses the urllib3 transport already provided by the storage SDK, avoiding an
extra HTTP client dependency. Location is explicit configuration so
deployments pin a data-residency region; cross-region failover is out of
scope (Scope §3).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, cast

import httpx
import urllib3

# Vertex-specific credential imports, allowed only inside app/ai/providers/
# (ADR-0017 import-boundary rule). google-auth ships partial type stubs; the
# credentials are treated as an opaque server-side token holder, so the
# strict-mode ignores are targeted to the untyped seams only.
from google.auth import default as google_auth_default  # pyright: ignore[reportUnknownVariableType]
from google.auth.transport import urllib3 as google_auth_urllib3
from google.oauth2 import service_account  # pyright: ignore[reportUnknownVariableType]

from app.ai.attachments import VERTEX_INLINE_ATTACHMENT_MIME_TYPES, Attachment
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
from app.ai.providers.openai_compatible import parse_structured_json
from app.ai.schemas import TokenUsage

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# Google returns 403 for both permission errors and quota exhaustion; both are
# permanent from the adapter's perspective (quota is reviewed configuration,
# not something one retry fixes), so the default 4xx mapping applies.
_FINISH_STOP_TOKENS = frozenset({"STOP"})
_FINISH_LENGTH_TOKENS = frozenset({"MAX_TOKENS"})
_FINISH_CONTENT_FILTER_TOKENS = frozenset({"SAFETY", "RECITATION"})


def _finish_reason(reason: str) -> str:
    token = reason.upper()
    if token in _FINISH_STOP_TOKENS:
        return FINISH_STOP
    if token in _FINISH_LENGTH_TOKENS:
        return FINISH_LENGTH
    if token in _FINISH_CONTENT_FILTER_TOKENS:
        return FINISH_CONTENT_FILTER
    return FINISH_UNKNOWN


# Native inline attachment parts (v0.7 Scope §6.3 attachment amendment,
# ADR-0017): Vertex ``generateContent`` accepts base64 ``inlineData`` parts
# (REST JSON naming: ``inlineData.mimeType`` / ``inlineData.data`` — never the
# proto ``inline_data``/``mime_type`` keys) for images, PDF and plain text.
# Bytes stay in memory and no storage credential, signed URL or object path
# ever reaches the adapter.
def _vertex_inline_parts(attachments: list[Attachment]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for attachment in attachments:
        parts.append(
            {
                "inlineData": {
                    "mimeType": attachment.mime_type,
                    "data": base64.b64encode(attachment.content).decode("ascii"),
                }
            }
        )
    return parts


class VertexAIAdapter(LLMProvider):
    """Vertex AI Gemini ``generateContent`` adapter."""

    provider_id = "vertex"
    supports_structured_output = True
    supports_documents = True
    supported_attachment_mime_types = VERTEX_INLINE_ATTACHMENT_MIME_TYPES

    def __init__(
        self,
        *,
        project: str,
        location: str,
        credentials_path: str = "",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not project:
            raise AIInputValidationError("vertex project is required")
        if not location:
            raise AIInputValidationError("vertex location is required")
        self._project = project
        self._location = location
        # The location is the declared data-residency region (ADR-0018) and is
        # reported in ``ProviderResponse.region`` for routing metadata.
        self.region = location
        self._credentials: Any = self._load_credentials(credentials_path)
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @staticmethod
    def _load_credentials(credentials_path: str) -> Any:
        if credentials_path:
            path = Path(credentials_path).expanduser()
            if not path.is_file():
                raise AIInputValidationError(
                    f"vertex credentials file is not readable: {credentials_path}"
                )
            credentials: Any = cast(
                Any,
                service_account.Credentials.from_service_account_file(  # pyright: ignore[reportUnknownMemberType]
                    str(path), scopes=[_CLOUD_PLATFORM_SCOPE]
                ),
            )
            return credentials
        adc_result: Any = google_auth_default(  # pyright: ignore[reportUnknownVariableType]
            scopes=[_CLOUD_PLATFORM_SCOPE]
        )
        return adc_result[0]

    def _auth_headers(self) -> dict[str, str]:
        credentials: Any = self._credentials
        if credentials is None:
            raise ProviderResponseError("vertex credentials are unavailable")
        if credentials.token is None or not credentials.valid:
            pool: Any = urllib3.PoolManager()
            credentials.refresh(google_auth_urllib3.Request(pool))
        return {"Authorization": f"Bearer {credentials.token}"}

    def _generate_url(self, request: ProviderRequest) -> str:
        # Regional endpoint: location is explicit configuration (data
        # residency, ADR-0018). ``request.model`` is the registry's model id,
        # e.g. "gemini-2.0-flash".
        return (
            f"https://{self._location}-aiplatform.googleapis.com/v1/projects/"
            f"{self._project}/locations/{self._location}/publishers/google/models/"
            f"{request.model}:generateContent"
        )

    def _build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        if request.attachments:
            # Pre-dispatch MIME guard (v0.7 Scope §6.3 attachment amendment):
            # Vertex ``inlineData`` carries images, PDF and plain text; a MIME
            # type outside that set fails here before any HTTP dispatch.
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
        generation_config: dict[str, Any] = {}
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.output_schema:
            generation_config["responseMimeType"] = "application/json"
        parts: list[dict[str, Any]] = [{"text": request.prompt}]
        if request.attachments:
            parts.extend(_vertex_inline_parts(list(request.attachments)))
        payload: dict[str, Any] = {"contents": [{"role": "user", "parts": parts}]}
        if generation_config:
            payload["generationConfig"] = generation_config
        return payload

    def _parse_response(
        self,
        request: ProviderRequest,
        data: dict[str, Any],
        latency_ms: float,
    ) -> ProviderResponse:
        candidates = data.get("candidates")
        if (
            not isinstance(candidates, list)
            or not candidates
            or not isinstance(candidates[0], dict)
        ):
            raise ProviderResponseError("provider returned no candidates")
        candidate = cast(dict[str, Any], candidates[0])
        content_raw = candidate.get("content")
        parts = (
            cast(dict[str, Any], content_raw).get("parts")
            if isinstance(content_raw, dict)
            else None
        )
        text = ""
        if isinstance(parts, list):
            text_parts: list[str] = []
            for part in parts:  # pyright: ignore[reportUnknownVariableType]
                part_dict = cast(dict[str, Any], part)  # pyright: ignore[reportUnknownVariableType]
                text_parts.append(str(part_dict.get("text") or ""))
            text = "".join(text_parts)
        usage_raw = data.get("usageMetadata")
        if isinstance(usage_raw, dict):
            usage_data = cast(dict[str, Any], usage_raw)
            usage = TokenUsage(
                input_tokens=safe_int(usage_data.get("promptTokenCount")),
                output_tokens=safe_int(usage_data.get("candidatesTokenCount")),
            )
        else:
            usage = TokenUsage(input_tokens=0, output_tokens=0)
        return ProviderResponse(
            model=request.model,
            content=text,
            structured=parse_structured_json(text) if request.output_schema else None,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=_finish_reason(str(candidate.get("finishReason") or "")),
            region=self.region,
        )

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        data, latency_ms = await post_json(
            self._client,
            self._generate_url(request),
            headers=self._auth_headers(),
            payload=self._build_payload(request),
        )
        return self._parse_response(request, data, latency_ms)

    async def aclose(self) -> None:
        await self._client.aclose()
