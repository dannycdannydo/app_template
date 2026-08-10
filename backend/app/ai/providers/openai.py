"""OpenAI provider adapter (v0.7 Scope §6.3, ADR-0017).

OpenAI is reached through the OpenAI chat-completions API at
``https://api.openai.com/v1`` (or a configured base URL). The OpenAI SDK is
deliberately not a dependency: the adapter is a thin, pinned HTTP REST client
so the fake-provider test default and the opt-in contract tests share one
wire format and the import boundary stays airtight (BP §33, ADR-0017). All
OpenAI-specific request/response details live in this module.
"""

from __future__ import annotations

from app.ai.providers.openai_compatible import OpenAICompatibleAdapter

__all__ = ["OpenAIAdapter"]


class OpenAIAdapter(OpenAICompatibleAdapter):
    """OpenAI chat-completions adapter."""

    provider_id = "openai"
    default_base_url = "https://api.openai.com/v1"
