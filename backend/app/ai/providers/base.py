"""Provider boundary for the AI layer (v0.7 Scope §6.1/§6.3, ADR-0017).

``LLMProvider`` is the only seam between the AI layer and an LLM provider.
Adapters implement it (OpenAI, Anthropic, DeepSeek, Azure OpenAI, Vertex AI
Gemini, local OpenAI-compatible — Scope §6.3); the deterministic fake lives in
``app.ai.providers.fake``. No module outside ``app/ai/providers/`` may import
a provider SDK (enforced by the import-boundary test, Scope §6.1). Provider
SDKs, provider-specific HTTP formats, authentication, streaming mechanics,
token reporting and model quirks are confined to the adapters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from app.ai.schemas import TokenUsage


class ProviderRequest(BaseModel):
    """A normalised, provider-neutral request sent to one adapter.

    ``prompt`` is the rendered prompt (system instructions + variables, safe
    template rendering — Scope §6.2). ``output_schema`` is a hint the adapter
    uses to request native structured output where supported; the adapter must
    never receive source content beyond the rendered prompt or approved
    metadata. ``max_tokens`` / ``temperature`` are optional parameter
    overrides bounded by the task's parameter defaults.
    """

    task: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1)
    output_schema: str | None = Field(default=None, max_length=512)
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    metadata: dict[str, str] = Field(default_factory=dict)


class ProviderResponse(BaseModel):
    """A normalised, provider-neutral response from one adapter.

    ``content`` is the raw text; ``structured`` is the parsed JSON object when
    the adapter produced structured output (native or JSON mode), otherwise
    ``None``. ``usage`` carries provider-reported token counts when available
    (the fake adapter and well-behaved providers report them; adapters must
    never fabricate counts from content). ``model`` is the provider's model
    identifier actually used, ``latency_ms`` the measured duration.
    """

    model: str = Field(min_length=1)
    content: str
    structured: dict[str, Any] | None = None
    usage: TokenUsage
    latency_ms: float = Field(ge=0)
    finish_reason: str = ""


class LLMProvider(ABC):
    """Minimal contract every LLM provider adapter implements.

    Adapters translate provider SDK or HTTP errors into the normalised
    taxonomy in ``app.ai.errors`` (ProviderUnavailableError,
    ProviderRateLimitError, ProviderTimeoutError, ProviderResponseError) so
    the service's retry/repair policy (Scope §6.4) can act without knowing
    the provider. Each adapter declares ``supports_structured_output`` for the
    native/JSON-mode structured path (Scope §6.3/§6.4) and the model registry
    (Scope §6.2) declares the per-model capabilities; adapters never pretend
    every provider is interchangeable.
    """

    provider_id: str
    #: Whether the adapter can request structured JSON output (native or JSON
    #: mode) for the task's declared output schema (Scope §6.3). The service
    #: still validates every result against the Pydantic contract (Scope §6.4).
    supports_structured_output: bool = False

    @abstractmethod
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Execute one request and return the normalised response.

        Raises a provider error from ``app.ai.errors`` on failure; must not
        leak provider-specific exception types or content to callers.
        """

    async def aclose(self) -> None:
        """Release adapter-owned resources (HTTP clients); a no-op by default."""
        return None
