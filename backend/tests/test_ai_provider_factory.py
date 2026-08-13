"""AI provider factory tests (v0.7 Scope §6.3, BP §27, ADR-0017).

The factory is the settings-driven construction seam: only enabled providers
are constructible, an enabled provider with missing configuration fails fast
with an actionable error, and the deterministic fake stays the default test
adapter. Settings-level fail-fast validation lives in test_config.py; these
tests exercise the factory's own checks (defense in depth) via
``Settings.model_construct`` so an unvalidated-but-enabled provider cannot
silently construct a broken adapter.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.ai.providers.anthropic import AnthropicAdapter
from app.ai.providers.azure_openai import AzureOpenAIAdapter
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
from app.core.config import Settings

_DB = "postgresql+asyncpg://x"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        app_env="test",
        database_url=_DB,
        ai_enabled_providers=["fake"],
        **overrides,
    )


def _unvalidated(**overrides: Any) -> Settings:
    """Settings bypassing pydantic validation (factory-only checks)."""
    return Settings.model_construct(
        app_env="test",
        database_url=_DB,
        **overrides,
    )


def test_known_provider_ids_match_the_config_allowlist() -> None:
    assert {
        "fake",
        "openai",
        "anthropic",
        "deepseek",
        "azure_openai",
        "vertex",
        "local",
    } == KNOWN_PROVIDER_IDS


def test_factory_builds_fake_by_default() -> None:
    factory = ProviderFactory(_settings())
    assert factory.enabled_provider_ids == ["fake"]
    assert isinstance(factory.create("fake"), FakeLLMProvider)


def test_factory_rejects_unknown_provider() -> None:
    factory = ProviderFactory(_settings())
    with pytest.raises(ValueError, match="unknown AI provider"):
        factory.create("unknown-provider")


def test_factory_rejects_disabled_provider() -> None:
    factory = ProviderFactory(_settings())
    with pytest.raises(ValueError, match="not enabled"):
        factory.create("openai")


def test_factory_rejects_enabled_provider_with_missing_config() -> None:
    settings = _unvalidated(ai_enabled_providers=["openai"], ai_openai_api_key="")
    factory = ProviderFactory(settings)
    with pytest.raises(ValueError, match="AI_OPENAI_API_KEY"):
        factory.create("openai")


@pytest.mark.parametrize(
    ("provider_id", "overrides", "expected_type"),
    [
        ("openai", {"ai_openai_api_key": "sk-test"}, OpenAIAdapter),
        ("anthropic", {"ai_anthropic_api_key": "ant-test"}, AnthropicAdapter),
        ("deepseek", {"ai_deepseek_api_key": "ds-test"}, DeepSeekAdapter),
        (
            "azure_openai",
            {
                "ai_azure_openai_api_key": "az-test",
                "ai_azure_openai_endpoint": "https://my-resource.openai.azure.com",
            },
            AzureOpenAIAdapter,
        ),
        (
            "vertex",
            {"ai_vertex_project": "demo-project", "ai_vertex_location": "europe-west1"},
            VertexAIAdapter,
        ),
        (
            "local",
            {"ai_local_base_url": "http://127.0.0.1:11434/v1"},
            LocalOpenAICompatibleAdapter,
        ),
    ],
)
def test_factory_constructs_each_enabled_provider(
    provider_id: str,
    overrides: dict[str, object],
    expected_type: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if provider_id == "vertex":
        # Vertex construction would attempt Application Default Credentials
        # (metadata server / GOOGLE_APPLICATION_CREDENTIALS); stand in a fake.
        class _FakeCredentials:
            token = "test-token"
            valid = True

        def _fake_google_auth(scopes: Any = None) -> tuple[Any, None]:
            return _FakeCredentials(), None

        monkeypatch.setattr(
            "app.ai.providers._google_credentials.google_auth_default",
            _fake_google_auth,
        )
    settings = _unvalidated(ai_enabled_providers=[provider_id], **overrides)
    factory = ProviderFactory(settings)
    adapter = factory.create(provider_id)
    assert isinstance(adapter, expected_type)
    assert adapter.provider_id == provider_id


def test_factory_orders_enabled_providers_stably() -> None:
    settings = _unvalidated(
        ai_enabled_providers=["vertex", "fake", "openai"],
        ai_openai_api_key="sk-test",
        ai_vertex_project="p",
        ai_vertex_location="l",
    )
    factory = ProviderFactory(settings)
    assert factory.enabled_provider_ids == ["fake", "openai", "vertex"]


def test_factory_requires_azure_endpoint_when_azure_enabled() -> None:
    settings = _unvalidated(
        ai_enabled_providers=["azure_openai"],
        ai_azure_openai_api_key="az-test",
        ai_azure_openai_endpoint="",
    )
    factory = ProviderFactory(settings)
    with pytest.raises(ValueError, match="AI_AZURE_OPENAI_ENDPOINT"):
        factory.create("azure_openai")


def test_factory_wires_openai_region_and_anthropic_inference_geography() -> None:
    """Regional settings reach the adapters (v0.7 Scope §6.3 amendment)."""
    settings = _unvalidated(
        ai_enabled_providers=["openai", "anthropic"],
        ai_openai_api_key="sk-test",
        ai_openai_region="eu",
        ai_anthropic_api_key="ant-test",
        ai_anthropic_inference_geography="us",
    )
    factory = ProviderFactory(settings)
    openai = factory.create("openai")
    assert openai.region == "eu"
    # The regional endpoint derivation is asserted in test_ai_adapters
    # (test_openai_region_routes_through_the_regional_endpoint); the factory's
    # contract here is that the setting reaches the adapter.
    anthropic = factory.create("anthropic")
    assert anthropic.region == "us"


def test_factory_defaults_regions_when_unset() -> None:
    settings = _unvalidated(
        ai_enabled_providers=["openai", "anthropic"],
        ai_openai_api_key="sk-test",
        ai_anthropic_api_key="ant-test",
    )
    factory = ProviderFactory(settings)
    assert factory.create("openai").region == ""
    assert factory.create("anthropic").region == ""


def test_factory_requires_vertex_project_when_vertex_enabled() -> None:
    settings = _unvalidated(
        ai_enabled_providers=["vertex"],
        ai_vertex_project="",
        ai_vertex_location="europe-west1",
    )
    factory = ProviderFactory(settings)
    with pytest.raises(ValueError, match="AI_VERTEX_PROJECT"):
        factory.create("vertex")


def test_get_provider_factory_is_a_process_singleton() -> None:
    assert get_provider_factory() is get_provider_factory()


def test_get_provider_is_cached_per_provider_id() -> None:
    assert get_provider("fake") is get_provider("fake")
    assert isinstance(get_provider("fake"), FakeLLMProvider)
