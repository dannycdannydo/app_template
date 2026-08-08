"""Provider adapters for the AI layer (v0.7 Scope §6.1/§6.3, ADR-0017).

Only modules inside ``app/ai/providers/`` may import provider SDKs. The
``LLMProvider`` contract and the deterministic ``FakeLLMProvider`` (the
default test adapter) ship in Scope §6.1; the real adapters (OpenAI,
Anthropic, DeepSeek, Azure OpenAI, Vertex AI Gemini, local OpenAI-compatible)
follow in Scope §6.3 behind the same contract.
"""

from app.ai.providers.base import LLMProvider, ProviderRequest, ProviderResponse
from app.ai.providers.fake import FakeLLMProvider

__all__ = [
    "FakeLLMProvider",
    "LLMProvider",
    "ProviderRequest",
    "ProviderResponse",
]
