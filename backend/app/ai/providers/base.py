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

from app.ai.attachments import MAX_ATTACHMENT_COUNT, Attachment
from app.ai.schemas import TokenUsage


class ProviderRequest(BaseModel):
    """A normalised, provider-neutral request sent to one adapter.

    ``prompt`` is the rendered prompt (system instructions + variables, safe
    template rendering — Scope §6.2). ``output_schema`` is a hint the adapter
    uses to request native structured output where supported; the adapter must
    never receive source content beyond the rendered prompt or approved
    metadata. ``max_tokens`` / ``temperature`` are optional parameter
    overrides bounded by the task's parameter defaults. ``attachments`` carries
    the validated bounded inline attachments (v0.7 Scope §6.2 amendment) when
    the task routes to a model with the ``documents`` capability; adapters that
    declare ``supports_documents`` map them to their native inline request form
    (Scope §6.3). Attachment bytes exist only for this request.
    """

    task: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1)
    output_schema: str | None = Field(default=None, max_length=512)
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    metadata: dict[str, str] = Field(default_factory=dict)
    # The lambda default avoids a Pyright strict-mode "partially unknown" false
    # positive that a bare ``list`` factory produces for model-element types.
    attachments: list[Attachment] = Field(
        default_factory=lambda: [], max_length=MAX_ATTACHMENT_COUNT
    )


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
    #: Whether the adapter can carry bounded inline document attachments in
    #: their native request form (v0.7 Scope §6.2/§6.3 amendment). The service
    #: refuses to dispatch attachments to an adapter that does not declare
    #: support — DeepSeek and local remain ``False`` until a reviewed
    #: capability exists (ADR-0017) — so unsupported documents always fail
    #: before dispatch rather than being silently dropped.
    supports_documents: bool = False

    @abstractmethod
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Execute one request and return the normalised response.

        Raises a provider error from ``app.ai.errors`` on failure; must not
        leak provider-specific exception types or content to callers.
        """

    async def aclose(self) -> None:
        """Release adapter-owned resources (HTTP clients); a no-op by default."""
        return None
