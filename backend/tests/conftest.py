"""Shared pytest fixtures and configuration hooks for the backend test suite.

Keep this file free of application imports so it stays usable as the
application shell is built out in later releases. Environment defaults are set
here (before any application code is imported) so tests never depend on a
developer's shell environment or a live database.
"""

import os
from pathlib import Path

import pytest

# The suite must always run in the test profile, whatever the surrounding shell
# exports: with APP_ENV left as ``development`` the real Redis rate limiter is
# constructed, and its asyncio client is bound to an event loop that pytest
# closes between tests, turning every protected request into a 500. The
# database URL is forced to the dedicated test database for the same reason —
# a developer's exported DATABASE_URL would otherwise point the real-database
# integration tests (which migrate and downgrade) at a development database.
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://app:app@localhost:5432/app_template_test"
# The storage adapter must never touch a real provider in the suite: pin the
# in-memory fake and an explicit test bucket (Scope §6.1) so ``make check``
# needs no MinIO. STORAGE_* credentials a developer exported are harmless here
# because the fake ignores them.
os.environ["STORAGE_PROVIDER"] = "fake"
os.environ["STORAGE_BUCKET"] = "test-bucket"
# The email adapter must never touch a real relay in the suite: pin the
# in-memory fake (Scope §6.2) so ``make check`` needs no Mailhog. SMTP_*
# credentials a developer exported are harmless here because the fake ignores
# them.
os.environ["EMAIL_PROVIDER"] = "fake"
# The AI layer must never touch a real provider in the suite: pin the
# deterministic fake adapter and a short shared HTTP timeout (Scope §6.3) so
# ``make check`` needs no provider account. AI_* provider credentials a
# developer exported are dropped below for the same reason the WorkOS ones
# are: the settings model reads them from the environment and an exported key
# would silently enable a real adapter in a test run.
os.environ["AI_ENABLED_PROVIDERS"] = '["fake"]'
os.environ["AI_HTTP_TIMEOUT_SECONDS"] = "5"
# WorkOS credentials are developer-shell exports that must never leak into the
# suite: config tests assert the empty development defaults and the production
# fail-fast validation, so drop them before any Settings model is constructed.
# Tests that need them pass explicit values. The bootstrap platform-admin
# credentials are dropped for the same reason: when exported, the login-time
# bootstrap hook queries the database on every request, which shifts the fake
# session's lookup queue and silently breaks every request-flow test that
# staged exactly user + membership.
for _var in (
    "WORKOS_API_KEY",
    "WORKOS_CLIENT_ID",
    "WORKOS_API_BASE_URL",
    "WORKOS_JWT_ISSUER",
    "WORKOS_JWT_LEEWAY",
    "BOOTSTRAP_PLATFORM_ADMIN_EMAIL",
    "BOOTSTRAP_PLATFORM_ADMIN_PASSWORD",
    "BOOTSTRAP_PLATFORM_ADMIN_ORG",
    # AI provider credentials/endpoints: exported values must never leak into
    # the suite (Scope §6.3); config tests pass explicit values where needed.
    "AI_OPENAI_API_KEY",
    "AI_OPENAI_BASE_URL",
    "AI_ANTHROPIC_API_KEY",
    "AI_ANTHROPIC_BASE_URL",
    "AI_DEEPSEEK_API_KEY",
    "AI_DEEPSEEK_BASE_URL",
    "AI_AZURE_OPENAI_API_KEY",
    "AI_AZURE_OPENAI_ENDPOINT",
    "AI_AZURE_OPENAI_API_VERSION",
    "AI_VERTEX_PROJECT",
    "AI_VERTEX_LOCATION",
    "AI_VERTEX_CREDENTIALS_PATH",
    "AI_VERTEX_TEMP_GCS_BUCKET",
    "AI_ENABLED_TRANSFER_MODES",
    "AI_LOCAL_BASE_URL",
    "AI_LOCAL_API_KEY",
    # HTTP(S)/proxy variables: httpx.AsyncClient reads these on construction
    # and raises on a non-standard scheme (e.g. ALL_PROXY=socks://...), which
    # would break adapter/factory tests in a proxied developer shell (Scope
    # §6.3). Drop them so the suite is hermetic regardless of the shell.
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
):
    os.environ.pop(_var, None)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root() -> Path:
    """Absolute path to the backend project root (contains pyproject.toml)."""
    return BACKEND_ROOT
