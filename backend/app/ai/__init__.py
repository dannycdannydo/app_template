"""Provider-neutral AI application layer (v0.7 Scope §6.1, ADR-0017).

Application code imports :class:`AIService` and the request/result schemas
from here and never a provider SDK, model id or provider-specific type. The
concrete registries (Scope §6.2) and provider factory (Scope §6.3) are wired
by the application; the deterministic :class:`FakeLLMProvider` is the default
adapter under test. Google Gemini is reached through Vertex AI only
(ADR-0018).
"""

from typing import TYPE_CHECKING

from app.ai.attachments import Attachment
from app.ai.errors import AIError
from app.ai.providers import FakeLLMProvider, LLMProvider
from app.ai.schemas import AIRequest, AIResult, ChatMessage, TokenUsage

if TYPE_CHECKING:
    from app.ai.service import AIService

__all__ = [
    "AIError",
    "AIRequest",
    "AIResult",
    "AIService",
    "Attachment",
    "ChatMessage",
    "FakeLLMProvider",
    "LLMProvider",
    "TokenUsage",
]


def __getattr__(name: str) -> object:
    """Load the service only when the public export is requested.

    Model registration imports ``app.ai.persistence.models`` during database
    bootstrap.  Keeping ``AIService`` lazy prevents that metadata import from
    re-entering audit-model import through the service graph.
    """
    if name == "AIService":
        from app.ai.service import AIService

        return AIService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
