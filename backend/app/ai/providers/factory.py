"""AI provider factory wired from typed settings (v0.7 Scope §6.3, BP §27).

``get_provider_factory`` mirrors the storage/email factory pattern: a
process-wide singleton read from ``Settings`` once. Only *enabled* providers
are constructible; an enabled provider whose required configuration is
missing fails fast with an actionable error (Scope §6.3, §6.7: production
never boots with a misconfigured adapter). The deterministic
:class:`~app.ai.providers.fake.FakeLLMProvider` remains the default test
adapter; provider credentials live in settings only and never reach the API
or frontend (ADR-0017, ADR-0018).
"""

from __future__ import annotations

from functools import lru_cache

from app.ai.providers.anthropic import AnthropicAdapter
from app.ai.providers.azure_openai import AzureOpenAIAdapter
from app.ai.providers.base import LLMProvider
from app.ai.providers.deepseek import DeepSeekAdapter
from app.ai.providers.fake import FakeLLMProvider
from app.ai.providers.local import LocalOpenAICompatibleAdapter
from app.ai.providers.openai import OpenAIAdapter
from app.ai.providers.vertex import VertexAIAdapter
from app.core.config import AI_KNOWN_PROVIDER_IDS, Settings, get_settings

# The complete set of adapter provider ids the factory knows, shared with the
# typed settings validator so configuration and construction can never
# disagree (BP §27). A provider that is not in this set can never be enabled,
# even through a mistyped setting.
KNOWN_PROVIDER_IDS = AI_KNOWN_PROVIDER_IDS
# Stable enumeration order for enabled_provider_ids (a frozenset has no
# iteration order).
_ORDERED_PROVIDER_IDS = (
    "fake",
    "openai",
    "anthropic",
    "deepseek",
    "azure_openai",
    "vertex",
    "local",
)


class ProviderFactory:
    """Constructs :class:`LLMProvider` adapters from application settings."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._enabled = set(settings.ai_enabled_providers)

    @property
    def enabled_provider_ids(self) -> list[str]:
        """The enabled provider ids in a stable order."""
        return [
            provider_id for provider_id in _ORDERED_PROVIDER_IDS if provider_id in self._enabled
        ]

    def create(self, provider_id: str) -> LLMProvider:
        """Construct one adapter; raises :class:`ValueError` when disabled,
        unknown or missing required configuration."""
        if provider_id not in KNOWN_PROVIDER_IDS:
            raise ValueError(
                f"unknown AI provider {provider_id!r}; known providers: {sorted(KNOWN_PROVIDER_IDS)}"
            )
        if provider_id not in self._enabled:
            raise ValueError(f"AI provider {provider_id!r} is not enabled in ai_enabled_providers")
        settings = self._settings
        if provider_id == "fake":
            return FakeLLMProvider()
        if provider_id == "openai":
            self._require(settings.ai_openai_api_key, "openai", "AI_OPENAI_API_KEY")
            return OpenAIAdapter(
                base_url=settings.ai_openai_base_url,
                api_key=settings.ai_openai_api_key,
                timeout_seconds=settings.ai_http_timeout_seconds,
                region=settings.ai_openai_region,
            )
        if provider_id == "anthropic":
            self._require(settings.ai_anthropic_api_key, "anthropic", "AI_ANTHROPIC_API_KEY")
            return AnthropicAdapter(
                api_key=settings.ai_anthropic_api_key,
                base_url=settings.ai_anthropic_base_url,
                timeout_seconds=settings.ai_http_timeout_seconds,
                inference_geography=settings.ai_anthropic_inference_geography,
            )
        if provider_id == "deepseek":
            self._require(settings.ai_deepseek_api_key, "deepseek", "AI_DEEPSEEK_API_KEY")
            return DeepSeekAdapter(
                base_url=settings.ai_deepseek_base_url,
                api_key=settings.ai_deepseek_api_key,
                timeout_seconds=settings.ai_http_timeout_seconds,
            )
        if provider_id == "azure_openai":
            self._require(
                settings.ai_azure_openai_api_key, "azure_openai", "AI_AZURE_OPENAI_API_KEY"
            )
            self._require(
                settings.ai_azure_openai_endpoint, "azure_openai", "AI_AZURE_OPENAI_ENDPOINT"
            )
            return AzureOpenAIAdapter(
                endpoint=settings.ai_azure_openai_endpoint,
                api_key=settings.ai_azure_openai_api_key,
                api_version=settings.ai_azure_openai_api_version,
                timeout_seconds=settings.ai_http_timeout_seconds,
            )
        if provider_id == "vertex":
            self._require(settings.ai_vertex_project, "vertex", "AI_VERTEX_PROJECT")
            self._require(settings.ai_vertex_location, "vertex", "AI_VERTEX_LOCATION")
            return VertexAIAdapter(
                project=settings.ai_vertex_project,
                location=settings.ai_vertex_location,
                credentials_path=settings.ai_vertex_credentials_path,
                timeout_seconds=settings.ai_http_timeout_seconds,
            )
        if provider_id == "local":
            self._require(settings.ai_local_base_url, "local", "AI_LOCAL_BASE_URL")
            return LocalOpenAICompatibleAdapter(
                base_url=settings.ai_local_base_url,
                api_key=settings.ai_local_api_key,
                timeout_seconds=settings.ai_http_timeout_seconds,
            )
        raise ValueError(f"unknown AI provider {provider_id!r}")

    @staticmethod
    def _require(value: str, provider_id: str, setting_name: str) -> None:
        if not value:
            raise ValueError(
                f"AI provider {provider_id!r} is enabled but {setting_name} is not configured"
            )


@lru_cache
def get_provider_factory() -> ProviderFactory:
    """Return the process-wide :class:`ProviderFactory` selected by settings."""
    return ProviderFactory(get_settings())


@lru_cache
def get_provider(provider_id: str) -> LLMProvider:
    """Return the process-wide adapter instance for one enabled provider."""
    return get_provider_factory().create(provider_id)
