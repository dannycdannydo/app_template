"""OpenAI provider adapter (v0.7 Scope §6.3, ADR-0017).

OpenAI is reached through the OpenAI chat-completions API at
``https://api.openai.com/v1`` (or a configured base URL). The OpenAI SDK is
deliberately not a dependency: the adapter is a thin, pinned HTTP REST client
so the fake-provider test default and the opt-in contract tests share one
wire format and the import boundary stays airtight (BP §33, ADR-0017). All
OpenAI-specific request/response details live in this module.

Regional processing (v0.7 Scope §6.3 regional amendment): OpenAI's regional
data-residency projects are served by the corresponding regional domain —
``https://eu.api.openai.com/v1`` or ``https://us.api.openai.com/v1`` — never
by a header on the global endpoint (official contract:
https://developers.openai.com/api/docs/guides/your-data#data-residency-controls).
A configured region therefore derives the endpoint, and an explicit base URL
override must name the same regional domain or the adapter fails fast; a
request must never be labelled regional while being routed through the global
endpoint.

Non-inline staged files (v0.8 Scope §2.4, §6.5): when the execution seam hands
the adapter a :class:`~app.ai.staging.StagedFile`, OpenAI's native file-input
contract is the **Responses API** ``input_file`` item — a provider file id
(``provider_upload``) or, for a retained private S3 source, a just-in-time
managed download URL (``file_url``) minted at dispatch time (verified
2026-08-11: chat completions does not support file URLs and the PDF guide
routes file ids through ``input_file``; ``app/ai/contracts/providers.yaml``).
The adapter therefore switches the whole dispatch to ``POST /responses`` when
a staged file is present, keeping every other request on the shared
chat-completions path. The managed URL is a one-dispatch bearer capability:
it is never returned, persisted, audited or logged, and no error message or
log line embeds it (BP §28).
"""

from __future__ import annotations

import re
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import structlog

from app.ai.attachments import OPENAI_INLINE_ATTACHMENT_MIME_TYPES
from app.ai.errors import AIInputValidationError, ProviderResponseError
from app.ai.providers.base import ProviderRequest, ProviderResponse
from app.ai.providers.http_transport import (
    FINISH_CONTENT_FILTER,
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_UNKNOWN,
    post_json,
    safe_int,
)
from app.ai.providers.openai_compatible import (
    JSON_INSTRUCTION,
    NATIVE_OUTPUT_NAME,
    OpenAICompatibleAdapter,
    parse_structured_json,
)
from app.ai.schemas import TokenUsage

#: Module logger. Dispatch-shape booleans and failure categories only — never
#: prompts, document content, credentials, URLs or raw provider responses
#: (BP §28, ADR-0017).
logger = structlog.get_logger()

__all__ = ["OpenAIAdapter"]

#: Regional chat-completions domains, keyed by the validated region values
#: from settings (v0.7 Scope §6.3). A region setting alone must never change
#: the wire request; the endpoint itself moves to the regional domain.
REGIONAL_API_HOSTS = {"us": "us.api.openai.com", "eu": "eu.api.openai.com"}

#: The reviewed snapshot shape the Responses API echoes for a requested model
#: (``gpt-4o-mini-2024-07-18`` for a ``gpt-4o-mini`` request): a dated suffix.
#: Only this exact shape is normalized to the routed id; any other suffix
#: retains the echoed id so a provider model mismatch surfaces in accounting.
_SNAPSHOT_MODEL_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")

#: The Responses API ``input_file`` item carries a provider file id for a
#: ``provider_upload`` reference. A file id is opaque provider state; a
#: URL-shaped external id is never dispatched (defense in depth, Scope §2.2).
_RESPONSES_FILE_ID = "input_file"


def _openai_responses_file_parts(request: ProviderRequest) -> list[dict[str, Any]]:
    """Native Responses-API ``input_file`` content items (v0.8 Scope §2.4).

    A staged ``provider_upload`` reference maps to ``input_file.file_id``; a
    retained private S3 source maps to ``input_file.file_url`` carrying the
    just-in-time minted managed URL (never a caller-supplied URL — the URL is
    service-minted from an authorised immutable object, Scope §2.1/§2.2). The
    managed URL exists only in this in-memory request for one dispatch.
    """
    staged = request.staged_file
    if request.managed_url:
        # The managed URL wins: a retained source never sends a file id.
        return [{"type": _RESPONSES_FILE_ID, "file_url": request.managed_url}]
    if staged is None:
        raise AIInputValidationError("a staged file reference is required for file input")
    external_id = staged.external_id
    if "://" in external_id or external_id.startswith("gs:"):
        # A staged file carrying a URL or cloud URI as its external id is
        # never dispatched as a file id (Scope §2.2 caller-URL prohibition).
        raise AIInputValidationError("the staged file reference has an invalid shape")
    return [{"type": _RESPONSES_FILE_ID, "file_id": external_id}]


class OpenAIAdapter(OpenAICompatibleAdapter):
    """OpenAI chat-completions adapter.

    Declares inline document support (v0.7 Scope §6.3 attachment amendment):
    images map to native ``image_url`` data-URI parts and documents to
    ``type=file`` parts in the shared chat-completions payload builder. A
    configured region derives the regional endpoint; an explicit base URL
    override must match that region's domain (fail-fast, never mislabelled
    regional routing).

    A request carrying a staged file (v0.8 Scope §2.4) is dispatched through
    the Responses API instead: ``provider_upload`` references use
    ``input_file.file_id`` and retained sources use ``input_file.file_url``
    with a just-in-time managed URL. Every other request keeps the shared
    chat-completions path.
    """

    provider_id = "openai"
    default_base_url = "https://api.openai.com/v1"
    supports_documents = True
    supports_native_structured_output = True
    supported_attachment_mime_types = OPENAI_INLINE_ATTACHMENT_MIME_TYPES

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        region: str = "",
    ) -> None:
        if region and not base_url:
            # A validated region selects the regional endpoint directly: the
            # regional domain is the only place regional processing exists.
            base_url = f"https://{region}.api.openai.com/v1"
        elif region and base_url:
            host = urlsplit(base_url).hostname or ""
            expected = REGIONAL_API_HOSTS[region]
            if host != expected:
                raise AIInputValidationError(
                    f"AI_OPENAI_BASE_URL host {host!r} conflicts with region {region!r}; "
                    f"regional requests must use https://{expected}/v1"
                )
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            client=client,
            region=region,
        )

    # --- Responses-API staged-file dispatch (v0.8 Scope §2.4, §6.5) ----------

    def _responses_url(self) -> str:
        return f"{self._base_url}/responses"

    def _build_responses_payload(self, request: ProviderRequest) -> dict[str, Any]:
        if request.attachments:
            # A dispatch carries either the inline set or exactly one staged
            # file, never both (ProviderRequest contract, Scope §2.4).
            raise AIInputValidationError(
                "a staged file and inline attachments are mutually exclusive"
            )
        if request.staged_file is None and request.managed_url is None:
            # The Responses-API document path needs one file input: a provider
            # file id (provider_upload) or a just-in-time managed URL for a
            # retained source (managed_signed_url, Scope §2.3/§6.5).
            raise AIInputValidationError(
                "the Responses-API dispatch path requires a staged file or a managed URL"
            )
        if request.staged_file is not None and request.staged_file.mime_type not in (
            self.supported_attachment_mime_types
        ):
            raise AIInputValidationError(
                f"provider {self.provider_id!r} does not support staged file MIME type "
                f"{request.staged_file.mime_type!r}"
            )
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    request.prompt + JSON_INSTRUCTION if request.output_schema else request.prompt
                ),
            },
            *_openai_responses_file_parts(request),
        ]
        payload: dict[str, Any] = {
            "model": request.model,
            "input": [{"role": "user", "content": content}],
        }
        if request.max_tokens is not None:
            # The Responses API bounds output with ``max_output_tokens``, not
            # ``max_tokens`` (verified 2026-08-11).
            payload["max_output_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.output_schema:
            if self.supports_native_structured_output and request.output_json_schema:
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": NATIVE_OUTPUT_NAME,
                        "schema": request.output_json_schema,
                        "strict": False,
                    }
                }
            else:
                payload["text"] = {"format": {"type": "json_object"}}
        return payload

    def _parse_responses_response(
        self,
        request: ProviderRequest,
        data: dict[str, Any],
        latency_ms: float,
    ) -> ProviderResponse:
        output = data.get("output")
        if not isinstance(output, list):
            # A 200 without an ``output`` array is almost always the provider's
            # error envelope or an incomplete file-backed dispatch. Log only
            # low-cardinality classification metadata — the HTTP-equivalent
            # status, the OpenAI error ``code``/``type`` and the top-level
            # keys — never the message, prompts or content (BP §28).
            error_code: str | None = None
            error_envelope = data.get("error")
            if isinstance(error_envelope, dict):
                envelope: dict[str, Any] = cast(dict[str, Any], error_envelope)
                raw_code = envelope.get("code") or envelope.get("type")
                if isinstance(raw_code, str):
                    error_code = raw_code
            logger.warning(
                "ai.openai.responses.unparseable",
                response_status=data.get("status"),
                error_code=error_code,
                top_level_keys=sorted(data),
            )
            raise ProviderResponseError("provider returned no usable output")
        content_parts: list[str] = []
        refused = False
        for item_value in output:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(item_value, dict):
                continue
            item = cast(dict[str, Any], item_value)  # pyright: ignore[reportUnknownVariableType]
            if item.get("type") == "refusal":
                refused = True
                continue
            if item.get("type") != "message":
                continue
            content_raw = item.get("content")
            if not isinstance(content_raw, list):
                continue
            for part_value in content_raw:  # pyright: ignore[reportUnknownVariableType]
                if not isinstance(part_value, dict):
                    continue
                part = cast(dict[str, Any], part_value)  # pyright: ignore[reportUnknownVariableType]
                if part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        content_parts.append(text)
        content = "".join(content_parts)
        usage_raw = data.get("usage")
        if isinstance(usage_raw, dict):
            usage_data = cast(dict[str, Any], usage_raw)
            usage = TokenUsage(
                input_tokens=safe_int(usage_data.get("input_tokens")),
                output_tokens=safe_int(usage_data.get("output_tokens")),
            )
        else:
            usage = TokenUsage(input_tokens=0, output_tokens=0)
        finish_reason = self._responses_finish_reason(data, refused=refused)
        return ProviderResponse(
            model=self._response_model(data, request),
            content=content,
            structured=parse_structured_json(content) if request.output_schema else None,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            region=self.region,
        )

    @staticmethod
    def _responses_finish_reason(data: dict[str, Any], *, refused: bool) -> str:
        """Map the Responses-API terminal state onto the shared vocabulary."""
        incomplete = data.get("incomplete_details")
        if isinstance(incomplete, dict):
            incomplete_data = cast(dict[str, Any], incomplete)
            reason = str(incomplete_data.get("reason") or "").lower()
            if reason == "max_output_tokens":
                return FINISH_LENGTH
            if reason == "content_filter" or refused:
                return FINISH_CONTENT_FILTER
            return FINISH_UNKNOWN
        if refused:
            return FINISH_CONTENT_FILTER
        return FINISH_STOP

    def _response_model(self, data: dict[str, Any], request: ProviderRequest) -> str:
        """The model identifier reported for routing/accounting.

        OpenAI echoes the requested id, or a dated snapshot of it — e.g. the
        ``gpt-4o-mini-2024-07-18`` snapshot for a ``gpt-4o-mini`` request —
        which is the same reviewed deployment. Only that reviewed snapshot
        shape (a ``-YYYY-MM-DD`` suffix) is normalized to the routed id; any
        other echoed id is retained so a genuinely different model surfaces as
        a mismatch in the service's routing/accounting check instead of being
        silently accepted.
        """
        echoed = str(data.get("model") or "")
        if echoed == request.model:
            return echoed
        if echoed.startswith(f"{request.model}-") and _SNAPSHOT_MODEL_SUFFIX.search(echoed):
            return request.model
        return echoed or request.model

    async def _complete_responses(self, request: ProviderRequest) -> ProviderResponse:
        # Dispatch-shape diagnostic (safe booleans only, BP §28): confirms
        # which file-input form reached the wire — a provider file id
        # (provider_upload) or a just-in-time managed URL (managed_signed_url).
        logger.info(
            "ai.openai.responses.dispatch",
            has_staged_file=request.staged_file is not None,
            has_managed_url=request.managed_url is not None,
        )
        data, latency_ms = await post_json(
            self._client,
            self._responses_url(),
            headers=self._auth_headers(),
            payload=self._build_responses_payload(request),
        )
        return self._parse_responses_response(request, data, latency_ms)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Dispatch one request.

        A request carrying a staged file or a managed URL goes through the
        Responses API (v0.8 file-input contract); every other request uses the
        shared chat-completions path.
        """
        if request.staged_file is not None or request.managed_url is not None:
            return await self._complete_responses(request)
        return await super().complete(request)
