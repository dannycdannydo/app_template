"""Shared HTTP transport and error mapping for provider adapters.

v0.7 Scope §6.3, ADR-0017: the real adapters are thin HTTP REST clients
(OpenAI-compatible chat completions, Anthropic messages, Vertex AI
generateContent) confined to ``app/ai/providers/``. This module owns the
generic mechanics every adapter needs — one JSON POST with latency
measurement, httpx exception translation and status-code mapping into the
normalised error taxonomy — while each adapter keeps its provider-specific
URLs, headers, payloads and response parsing in its own module (BP §23).

Error messages here are deliberately generic: provider bodies may contain
prompt or document fragments and must never surface in an exception message,
a log line or Sentry (BP §28 never-log list, ADR-0017).
"""

from __future__ import annotations

import time
from typing import Any, cast

import httpx

from app.ai.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

# Statuses every adapter maps the same way: 429 is a rate limit (retryable),
# 5xx an unavailable server (retryable); everything else 4xx is a permanent
# adapter/configuration error that retrying the identical request cannot fix.
_RETRYABLE_STATUSES = frozenset({429})
_UNAVAILABLE_STATUSES = frozenset({500, 502, 503, 504})


def safe_int(value: Any) -> int:
    """Parse a provider token count defensively; malformed values map to 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def translate_http_exception(exc: httpx.HTTPError) -> ProviderError:
    """Translate an httpx transport exception into the normalised taxonomy.

    Timeouts and transport failures are retryable; anything else (a malformed
    request produced by us) is a permanent response error.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError("provider request timed out")
    if isinstance(exc, httpx.TransportError):
        return ProviderUnavailableError("provider endpoint is unreachable")
    return ProviderResponseError("provider request failed")


def raise_for_provider_status(
    response: httpx.Response,
    *,
    retryable_statuses: frozenset[int] = _RETRYABLE_STATUSES,
    unavailable_statuses: frozenset[int] = _UNAVAILABLE_STATUSES,
) -> None:
    """Map a non-2xx provider status into the safe error taxonomy.

    Adapters may widen ``retryable_statuses``/``unavailable_statuses`` for
    provider-specific codes (e.g. Anthropic 529 overloaded). Status codes are
    safe to include; response bodies are not.
    """
    if response.is_success:
        return
    status = response.status_code
    if status == 429 or status in retryable_statuses:
        raise ProviderRateLimitError("provider rate limited the request")
    if status in unavailable_statuses:
        raise ProviderUnavailableError("provider returned a server error")
    raise ProviderResponseError(f"provider rejected the request (HTTP {status})")


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    retryable_statuses: frozenset[int] = _RETRYABLE_STATUSES,
    unavailable_statuses: frozenset[int] = _UNAVAILABLE_STATUSES,
) -> tuple[dict[str, Any], float]:
    """POST one JSON payload and return ``(parsed_json, latency_ms)``.

    Raises a :class:`~app.ai.errors.ProviderError` subclass on any transport
    failure, non-2xx status or unparseable body; the parsed JSON is typed as
    ``dict`` for adapter response parsing.
    """
    started = time.perf_counter()
    try:
        response = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise translate_http_exception(exc) from exc
    latency_ms = (time.perf_counter() - started) * 1000.0
    raise_for_provider_status(
        response,
        retryable_statuses=retryable_statuses,
        unavailable_statuses=unavailable_statuses,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderResponseError("provider returned an unparseable response body") from exc
    if not isinstance(body, dict):
        raise ProviderResponseError("provider returned a malformed response body")
    return cast(dict[str, Any], body), latency_ms


# Safe finish-reason vocabulary shared by every adapter so the service and the
# durable ai_requests record (Scope §6.5) never store provider-specific strings.
FINISH_STOP = "stop"
FINISH_LENGTH = "length"
FINISH_CONTENT_FILTER = "content_filter"
FINISH_UNKNOWN = "unknown"
