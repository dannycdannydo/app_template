"""Azure OpenAI provider adapter (v0.7 Scope §6.3, ADR-0017).

Azure OpenAI serves the OpenAI chat-completions wire format through a
deployment-scoped URL: ``{endpoint}/openai/deployments/{deployment}/chat/completions``
with the ``api-version`` query parameter and the resource key in an ``api-key``
header. The registry's ``model`` field holds the *deployment name*, never a
public model id, and the adapter reports the deployment name back as the
served model (the response body echoes the underlying model id instead, which
would otherwise trip the service's routed-model check). Azure-specific
endpoint/deployment naming and errors are contained in this module.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.ai.attachments import OPENAI_INLINE_ATTACHMENT_MIME_TYPES
from app.ai.providers.base import ProviderRequest
from app.ai.providers.openai_compatible import OpenAICompatibleAdapter

__all__ = ["AzureOpenAIAdapter"]

#: The first Azure OpenAI api-version that ships OpenAI's native ``json_schema``
#: response format (Microsoft structured-outputs contract). Lexicographic
#: comparison is safe for zero-padded ``YYYY-MM-DD[-preview]`` version strings,
#: so a pinned version >= this constant truthfully declares native support and
#: an older pinned version stays on the JSON-mode prompt contract (Scope §6.4).
_STRUCTURED_OUTPUTS_MIN_API_VERSION = "2024-08-01-preview"


def _api_version_supports_structured_outputs(api_version: str) -> bool:
    return api_version >= _STRUCTURED_OUTPUTS_MIN_API_VERSION


class AzureOpenAIAdapter(OpenAICompatibleAdapter):
    """Azure OpenAI chat-completions adapter over deployment URLs.

    Declares inline document support (v0.7 Scope §6.3 attachment amendment):
    Azure serves the same chat-completions wire format as OpenAI, so images
    use native inline ``image_url`` parts and documents use ``type=file``
    parts (the pinned ``api-version`` gates model/file support and is
    reviewed configuration, never user input). The Azure region is inherent in
    the configured resource endpoint and is never a separate setting
    (regional amendment); the adapter reports no region because it is not
    reliably derivable from the endpoint hostname.
    """

    provider_id = "azure_openai"
    supports_documents = True
    # The class default stays False; each instance derives the truth from its
    # pinned api-version, because Microsoft documented structured outputs
    # arriving in ``2024-08-01-preview`` (Scope §6.4). A deployment pinned to
    # an older version therefore never pretends to support native
    # ``json_schema`` and truthfully uses the JSON-mode prompt contract.
    supports_native_structured_output = False
    supported_attachment_mime_types = OPENAI_INLINE_ATTACHMENT_MIME_TYPES

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        api_version: str,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_version = api_version
        self.supports_native_structured_output = _api_version_supports_structured_outputs(
            api_version
        )
        super().__init__(
            base_url=self._endpoint,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            client=client,
        )

    def _chat_url(self, request: ProviderRequest) -> str:
        # ``request.model`` is the deployment name from the registry (Scope
        # §6.2); the api-version is pinned configuration, never user input.
        return (
            f"{self._endpoint}/openai/deployments/{request.model}/chat/completions"
            f"?api-version={self._api_version}"
        )

    def _auth_headers(self) -> dict[str, str]:
        # Azure authenticates with the resource key in ``api-key``, not a
        # Bearer token.
        return {"api-key": self._api_key}

    def _response_model(self, data: dict[str, Any], request: ProviderRequest) -> str:
        # The response body echoes the underlying model id (e.g. "gpt-4o-mini")
        # while the request was routed by deployment name; the deployment name
        # is the reviewed routing fact and is reported as such.
        return request.model
