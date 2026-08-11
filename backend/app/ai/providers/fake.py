"""Deterministic FakeLLMProvider for tests (v0.7 Scope §6.1, ADR-0017).

The fake never touches a provider: it records every request and mints a
deterministic response derived from the request itself, so the same input
always yields the same output — the property the service-contract and
routing tests rely on. ``fail_next_call`` arms a number of consecutive calls
to raise a chosen provider error, and ``set_next_response`` queues a canned
response (e.g. malformed output) so the validation path (Scope §6.4) can be
exercised without a provider.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.ai.attachments import OPENAI_INLINE_ATTACHMENT_MIME_TYPES
from app.ai.errors import ProviderUnavailableError
from app.ai.providers.base import LLMProvider, ProviderRequest, ProviderResponse
from app.ai.schemas import TokenUsage

# Deterministic pseudo-token counts so cost/usage assertions are stable: 4
# characters per token is a common rule of thumb and keeps tests exact.
_CHARS_PER_TOKEN = 4


def _deterministic_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _token_count(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


class FakeLLMProvider(LLMProvider):
    """Deterministic, test-only :class:`LLMProvider` implementation.

    Declares document support so attachment routing/dispatch (v0.7 Scope §6.2
    amendment) can be exercised end-to-end without a real provider; the fake
    records attachment digests in its structured output so tests can assert
    the adapter received exactly the resolved attachment set.
    """

    provider_id = "fake"
    supports_structured_output = True
    supports_documents = True
    # The fake accepts the full template allowlist: it has no wire format, so
    # every validated attachment the router permits is recordable.
    supported_attachment_mime_types = OPENAI_INLINE_ATTACHMENT_MIME_TYPES

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self._fail_next = 0
        self._fail_error: type[Exception] = ProviderUnavailableError
        self._queued_response: ProviderResponse | None = None
        self._queued_responses: list[ProviderResponse] = []
        self._queued_failures = 0
        self._queued_failure_error: type[Exception] = ProviderUnavailableError

    def fail_next_call(self, count: int = 1, *, error: type[Exception] | None = None) -> None:
        """Arm the next ``count`` calls to raise ``error`` (default:
        ProviderUnavailableError)."""
        if count < 1:
            raise ValueError("fail_next_call count must be at least 1")
        self._fail_next += count
        if error is not None:
            self._fail_error = error

    def queue_transient_failure(
        self, count: int = 1, *, error: type[Exception] = ProviderUnavailableError
    ) -> None:
        """Queue ``count`` transient failures raised *after* any already-queued
        canned responses are consumed.

        ``fail_next_call`` fires before canned responses; this helper fires
        after them, so a test can exercise a transient provider failure inside
        the repair path (Scope §6.4), where a malformed canned response
        precedes the failing repair call.
        """
        if count < 1:
            raise ValueError("queue_transient_failure count must be at least 1")
        self._queued_failures += count
        self._queued_failure_error = error

    def set_next_response(self, response: ProviderResponse) -> None:
        """Queue one canned response returned by the next call.

        Used to simulate malformed or unexpected provider output that the
        service's validation path must reject (Scope §6.4).
        """
        self._queued_response = response

    def queue_responses(self, *responses: ProviderResponse) -> None:
        """Queue several canned responses returned in order by the next calls.

        Used to simulate, for example, two consecutive malformed outputs so
        the repair path can be exercised to its terminal failure (Scope §6.4).
        """
        self._queued_responses.extend(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if self._fail_next > 0:
            self._fail_next -= 1
            raise self._fail_error("simulated provider failure")
        if self._queued_responses:
            return self._queued_responses.pop(0)
        if self._queued_response is not None:
            response = self._queued_response
            self._queued_response = None
            return response
        if self._queued_failures > 0:
            self._queued_failures -= 1
            raise self._queued_failure_error("simulated provider failure")

        # Deterministic structured output derived from the request: the task,
        # the requested schema, a stable prompt hash and the approved metadata
        # (sorted, so ordering is stable). Validators in tests assert on these
        # fields. Plain text output also varies by task so two tasks never
        # produce identical content.
        structured: dict[str, Any] | None = None
        if request.output_schema:
            if request.output_schema == "app.ai.tasks.schemas.DocumentClassificationResult":
                structured = {
                    "category": "lease",
                    "confidence": 0.99,
                    "summary": "A deterministic, non-sensitive fixture classification.",
                }
            else:
                structured = {
                    "schema": request.output_schema,
                    "task": request.task,
                    "prompt_hash": _deterministic_hash(request.prompt)[:16],
                    "variables": dict(sorted(request.metadata.items())),
                    "attachments": [attachment.sha256_digest for attachment in request.attachments],
                }
            content = json.dumps(structured, sort_keys=True)
        else:
            content = f"{request.task}:{_deterministic_hash(request.prompt)}"

        return ProviderResponse(
            model=request.model,
            content=content,
            structured=structured,
            usage=TokenUsage(
                input_tokens=_token_count(request.prompt),
                output_tokens=_token_count(content),
            ),
            latency_ms=1.0,
            finish_reason="stop",
            region=self.region,
        )
