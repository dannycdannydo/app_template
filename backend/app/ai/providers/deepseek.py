"""DeepSeek provider adapter (v0.7 Scope §6.3, ADR-0017).

DeepSeek exposes an OpenAI-compatible chat-completions API, but it remains its
own adapter with its own ``provider_id`` and default endpoint: "API-compatible"
does not mean interchangeable, and routing must never silently swap DeepSeek
for OpenAI without reviewed configuration (v0.7 Scope §6.3). Model ids, pricing
and availability are registry data; nothing in this module names a model.

Two boundaries are declared truthfully rather than faked (attachment and
regional amendments, v0.7 Scope §6.3): DeepSeek **rejects** document
attachments (``supports_documents`` stays False and the shared payload builder
fails an attachment-bearing request before dispatch), and it offers no
template-controlled regional pinning, so the adapter reports no region and
routing treats it as an unpinned provider.
"""

from __future__ import annotations

from app.ai.providers.openai_compatible import OpenAICompatibleAdapter

__all__ = ["DeepSeekAdapter"]


class DeepSeekAdapter(OpenAICompatibleAdapter):
    """DeepSeek chat-completions adapter (OpenAI-compatible wire format)."""

    provider_id = "deepseek"
    default_base_url = "https://api.deepseek.com"
