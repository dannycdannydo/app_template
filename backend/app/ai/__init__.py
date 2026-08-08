"""Provider-neutral AI application layer (v0.7 Scope §6.1, ADR-0017).

Application code imports :class:`AIService` and the request/result schemas
from here and never a provider SDK, model id or provider-specific type. The
concrete registries (Scope §6.2) and provider factory (Scope §6.3) are wired
by the application; the deterministic :class:`FakeLLMProvider` is the default
adapter under test. Google Gemini is reached through Vertex AI only
(ADR-0018).
"""

from app.ai.errors import AIError
from app.ai.providers import FakeLLMProvider, LLMProvider
from app.ai.schemas import AIRequest, AIResult, ChatMessage, TokenUsage
from app.ai.service import AIService

__all__ = [
    "AIError",
    "AIRequest",
    "AIResult",
    "AIService",
    "ChatMessage",
    "FakeLLMProvider",
    "LLMProvider",
    "TokenUsage",
]
