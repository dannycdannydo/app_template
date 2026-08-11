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
    uses to request structured output where supported; the adapter must never
    receive source content beyond the rendered prompt or approved metadata.
    ``output_json_schema`` is the JSON Schema the service generated from the
    task's declared Pydantic output model (Scope §6.4); adapters that
    truthfully declare ``supports_native_structured_output`` map it to their
    native structured-output form (OpenAI ``json_schema``, Vertex
    ``responseJsonSchema``), others fall back to the documented JSON-mode prompt
    contract. ``max_tokens`` / ``temperature`` are optional parameter
    overrides bounded by the task's parameter defaults. ``repair`` marks a
    bounded repair request issued after the first response failed Pydantic
    validation (Scope §6.4); the prompt then carries the repair instruction and
    the adapter reuses the exact same structured-output contract. ``attachments``
    carries the validated bounded inline attachments (v0.7 Scope §6.2
    amendment) when the task routes to a model with the ``documents``
    capability; adapters that declare ``supports_documents`` map them to their
    native inline request form (Scope §6.3). Attachment bytes exist only for
    this request.
    """

    task: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1)
    output_schema: str | None = Field(default=None, max_length=512)
    output_json_schema: dict[str, Any] | None = Field(default=None)
    max_tokens: int | None = Field(default=None, ge=1, le=128_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    metadata: dict[str, str] = Field(default_factory=dict)
    repair: bool = False
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
    identifier actually used, ``latency_ms`` the measured duration. ``region``
    is the adapter's configured deployment region (v0.7 Scope §6.3 regional
    amendment); it is recorded in routing metadata without increasing label
    cardinality — the adapter reports it, it is never derived from content.
    """

    model: str = Field(min_length=1)
    content: str
    structured: dict[str, Any] | None = None
    usage: TokenUsage
    latency_ms: float = Field(ge=0)
    finish_reason: str = ""
    #: Configured deployment region reported for routing metadata; empty when
    #: the provider has no template-controlled region pinning (DeepSeek, local,
    #: fake) or the region is inherent in another setting (Azure endpoint).
    region: str = ""


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
    #: Whether the adapter can request **native** structured output from its
    #: wire format — OpenAI ``json_schema`` response format, Vertex
    #: ``responseJsonSchema`` — given the JSON Schema the service generates from
    #: the Pydantic output model (Scope §6.4). ``False`` adapters use the
    #: documented JSON-mode prompt contract instead and the service's Pydantic
    #: validation remains the safety net either way. Adapters declare only
    #: what their provider actually supports: Anthropic (tool-use based native
    #: output is not implemented here), DeepSeek/local (JSON mode only) keep
    #: ``False``; Azure derives the declaration from its pinned api-version
    #: (structured outputs arrived in ``2024-08-01-preview``), so an older
    #: pinned version never pretends to support native ``json_schema``.
    supports_native_structured_output: bool = False
    #: Whether the adapter can carry bounded inline document attachments in
    #: their native request form (v0.7 Scope §6.2/§6.3 amendment). The service
    #: refuses to dispatch attachments to an adapter that does not declare
    #: support — DeepSeek and local remain ``False`` (ADR-0017) — so
    #: unsupported documents always fail before dispatch rather than being
    #: silently dropped.
    supports_documents: bool = False
    #: The attachment MIME types this adapter can carry natively in its wire
    #: format (v0.7 Scope §6.3 attachment amendment). Empty means no
    #: attachments; document-capable adapters declare exactly the reviewed set
    #: from :mod:`app.ai.attachments`, and the registry model declarations must
    #: stay subsets of it. The shared router check runs first; this declaration
    #: backs the adapter's own pre-dispatch guard so a directly constructed
    #: adapter fails closed on an unsupported MIME type.
    supported_attachment_mime_types: frozenset[str] = frozenset()
    #: The adapter's configured deployment region (v0.7 Scope §6.3 regional
    #: amendment): OpenAI's validated region setting, Anthropic's inference
    #: geography, Vertex's location, or empty where the provider has no
    #: template-controlled pinning (DeepSeek, local, fake) or the region is
    #: inherent in the endpoint (Azure). Instances set this in ``__init__``;
    #: it is reported in ``ProviderResponse.region``.
    region: str = ""

    @abstractmethod
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Execute one request and return the normalised response.

        Raises a provider error from ``app.ai.errors`` on failure; must not
        leak provider-specific exception types or content to callers.
        """

    async def aclose(self) -> None:
        """Release adapter-owned resources (HTTP clients); a no-op by default."""
        return None
