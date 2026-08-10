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

from app.ai.providers.base import ProviderRequest
from app.ai.providers.openai_compatible import OpenAICompatibleAdapter

__all__ = ["AzureOpenAIAdapter"]


class AzureOpenAIAdapter(OpenAICompatibleAdapter):
    """Azure OpenAI chat-completions adapter over deployment URLs."""

    provider_id = "azure_openai"

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
