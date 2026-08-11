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
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from app.ai.attachments import OPENAI_INLINE_ATTACHMENT_MIME_TYPES
from app.ai.errors import AIInputValidationError
from app.ai.providers.openai_compatible import OpenAICompatibleAdapter

__all__ = ["OpenAIAdapter"]

#: Regional chat-completions domains, keyed by the validated region values
#: from settings (v0.7 Scope §6.3). A region setting alone must never change
#: the wire request; the endpoint itself moves to the regional domain.
_REGIONAL_API_HOSTS = {"us": "us.api.openai.com", "eu": "eu.api.openai.com"}


class OpenAIAdapter(OpenAICompatibleAdapter):
    """OpenAI chat-completions adapter.

    Declares inline document support (v0.7 Scope §6.3 attachment amendment):
    images map to native ``image_url`` data-URI parts and documents to
    ``type=file`` parts in the shared chat-completions payload builder. A
    configured region derives the regional endpoint; an explicit base URL
    override must match that region's domain (fail-fast, never mislabelled
    regional routing).
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
            expected = _REGIONAL_API_HOSTS[region]
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
