"""Provider adapters for the AI layer (v0.7 Scope §6.1/§6.3, ADR-0017).

Only modules inside ``app/ai/providers/`` may import provider SDKs or touch
provider-specific HTTP formats; the import-boundary test (Scope §6.1) enforces
this structurally. The ``LLMProvider`` contract and the deterministic
``FakeLLMProvider`` (the default test adapter) ship in Scope §6.1; the real
adapters — OpenAI, Anthropic, DeepSeek, Azure OpenAI, Vertex AI Gemini and the
local OpenAI-compatible server — ship in Scope §6.3 behind the same contract
and are constructed by the settings-driven factory. Google Gemini is Vertex AI
only (ADR-0018).
"""

from app.ai.providers.anthropic import AnthropicAdapter
from app.ai.providers.azure_openai import AzureOpenAIAdapter
from app.ai.providers.base import LLMProvider, ProviderRequest, ProviderResponse
from app.ai.providers.deepseek import DeepSeekAdapter
from app.ai.providers.factory import (
    KNOWN_PROVIDER_IDS,
    ProviderFactory,
    get_provider,
    get_provider_factory,
)
from app.ai.providers.fake import FakeLLMProvider
from app.ai.providers.local import LocalOpenAICompatibleAdapter
from app.ai.providers.openai import OpenAIAdapter
from app.ai.providers.vertex import VertexAIAdapter

__all__ = [
    "KNOWN_PROVIDER_IDS",
    "AnthropicAdapter",
    "AzureOpenAIAdapter",
    "DeepSeekAdapter",
    "FakeLLMProvider",
    "LLMProvider",
    "LocalOpenAICompatibleAdapter",
    "OpenAIAdapter",
    "ProviderFactory",
    "ProviderRequest",
    "ProviderResponse",
    "VertexAIAdapter",
    "get_provider",
    "get_provider_factory",
]
