"""Local OpenAI-compatible provider adapter (v0.7 Scope §6.3, ADR-0017).

Targets a privately reachable OpenAI-compatible server (Ollama, vLLM or
SGLang) through the same chat-completions wire format. The endpoint safety
rules in :mod:`app.core.endpoint_safety` are enforced here again
(defense in depth) so a directly constructed adapter can never point at a
public HTTP endpoint; the typed settings validator applies the identical rule
(BP §27). This adapter is backend-only by construction — no browser or
frontend code ever sees its URL or key.

Two boundaries are declared truthfully rather than faked (attachment and
regional amendments, v0.7 Scope §6.3): local has no reviewed document
capability, so ``supports_documents`` stays False and the shared payload
builder fails an attachment-bearing request before dispatch; and it inherits
its operator-controlled location, so it reports no template-controlled region.
"""

from __future__ import annotations

import httpx

from app.ai.errors import AIInputValidationError
from app.ai.providers.openai_compatible import OpenAICompatibleAdapter
from app.core.endpoint_safety import validate_local_endpoint

__all__ = ["LocalOpenAICompatibleAdapter"]


class LocalOpenAICompatibleAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter for a privately reachable local server."""

    provider_id = "local"
    default_base_url = ""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        try:
            safe_endpoint = validate_local_endpoint(base_url)
        except ValueError as exc:
            raise AIInputValidationError(str(exc)) from exc
        super().__init__(
            base_url=safe_endpoint,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            client=client,
        )
