"""Opt-in live AI provider contract tests (v0.7 Scope §4, §6.3, ADR-0018).

Marked ``ai_contracts`` and excluded from the default suite; run explicitly
with ``make test-ai-contracts`` (``uv run pytest -m ai_contracts``). Each
test skips cleanly with an explicit message when its dedicated non-production
credentials are absent, so the target is a no-op in CI until provider secrets
are deliberately configured.

Credentials use a dedicated ``AI_CONTRACTS_*`` namespace (never the
operational ``AI_*`` settings): the scope requires contract tests to run only
against dedicated non-production accounts/projects. Google tests use a
dedicated Google Cloud project/location and Vertex AI credentials only —
never a Gemini API key (ADR-0018).
"""

from __future__ import annotations

import os

import pytest

from app.ai.providers.anthropic import AnthropicAdapter
from app.ai.providers.azure_openai import AzureOpenAIAdapter
from app.ai.providers.base import LLMProvider, ProviderRequest
from app.ai.providers.deepseek import DeepSeekAdapter
from app.ai.providers.local import LocalOpenAICompatibleAdapter
from app.ai.providers.openai import OpenAIAdapter
from app.ai.providers.vertex import VertexAIAdapter


def _request(model: str) -> ProviderRequest:
    return ProviderRequest(
        task="document.classify",
        model=model,
        prompt="Reply with the single word OK and nothing else.",
        max_tokens=1,
        temperature=0,
    )


async def _probe(adapter: LLMProvider, model: str) -> None:
    """Run one tiny completion and assert the normalised response surface."""
    response = await adapter.complete(_request(model))
    assert isinstance(response.content, str)
    assert response.latency_ms >= 0
    assert response.usage.input_tokens >= 0
    assert response.usage.output_tokens >= 0
    await adapter.aclose()


@pytest.mark.ai_contracts
async def test_openai_live_contract() -> None:
    api_key = os.environ.get("AI_CONTRACTS_OPENAI_API_KEY")
    if not api_key:
        pytest.skip(
            "AI_CONTRACTS_OPENAI_API_KEY not configured; skipping live OpenAI contract test"
        )
    model = os.environ.get("AI_CONTRACTS_OPENAI_MODEL", "gpt-4o-mini")
    await _probe(OpenAIAdapter(api_key=api_key), model)


@pytest.mark.ai_contracts
async def test_anthropic_live_contract() -> None:
    api_key = os.environ.get("AI_CONTRACTS_ANTHROPIC_API_KEY")
    if not api_key:
        pytest.skip(
            "AI_CONTRACTS_ANTHROPIC_API_KEY not configured; skipping live Anthropic contract test"
        )
    model = os.environ.get("AI_CONTRACTS_ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
    await _probe(AnthropicAdapter(api_key=api_key), model)


@pytest.mark.ai_contracts
async def test_deepseek_live_contract() -> None:
    api_key = os.environ.get("AI_CONTRACTS_DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip(
            "AI_CONTRACTS_DEEPSEEK_API_KEY not configured; skipping live DeepSeek contract test"
        )
    model = os.environ.get("AI_CONTRACTS_DEEPSEEK_MODEL", "deepseek-chat")
    await _probe(DeepSeekAdapter(api_key=api_key), model)


@pytest.mark.ai_contracts
async def test_azure_openai_live_contract() -> None:
    endpoint = os.environ.get("AI_CONTRACTS_AZURE_ENDPOINT")
    api_key = os.environ.get("AI_CONTRACTS_AZURE_API_KEY")
    if not endpoint or not api_key:
        pytest.skip(
            "AI_CONTRACTS_AZURE_ENDPOINT/AI_CONTRACTS_AZURE_API_KEY not configured; "
            "skipping live Azure OpenAI contract test"
        )
    deployment = os.environ.get("AI_CONTRACTS_AZURE_DEPLOYMENT", "gpt-4o-mini")
    api_version = os.environ.get("AI_CONTRACTS_AZURE_API_VERSION", "2024-08-01-preview")
    await _probe(
        AzureOpenAIAdapter(endpoint=endpoint, api_key=api_key, api_version=api_version),
        deployment,
    )


@pytest.mark.ai_contracts
async def test_vertex_live_contract() -> None:
    project = os.environ.get("AI_CONTRACTS_VERTEX_PROJECT")
    location = os.environ.get("AI_CONTRACTS_VERTEX_LOCATION")
    if not project or not location:
        pytest.skip(
            "AI_CONTRACTS_VERTEX_PROJECT/AI_CONTRACTS_VERTEX_LOCATION not configured; "
            "skipping live Vertex AI contract test (ADR-0018: Vertex credentials only)"
        )
    model = os.environ.get("AI_CONTRACTS_VERTEX_MODEL", "gemini-2.0-flash")
    adapter = VertexAIAdapter(
        project=project,
        location=location,
        credentials_path=os.environ.get("AI_CONTRACTS_VERTEX_CREDENTIALS_PATH", ""),
    )
    await _probe(adapter, model)


@pytest.mark.ai_contracts
async def test_local_live_contract() -> None:
    base_url = os.environ.get("AI_CONTRACTS_LOCAL_BASE_URL")
    if not base_url:
        pytest.skip(
            "AI_CONTRACTS_LOCAL_BASE_URL not configured; skipping live local provider contract test"
        )
    model = os.environ.get("AI_CONTRACTS_LOCAL_MODEL", "qwen2.5:0.5b")
    await _probe(LocalOpenAICompatibleAdapter(base_url=base_url), model)
