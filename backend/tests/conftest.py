"""Shared pytest fixtures and configuration hooks for the backend test suite.

Keep this file free of application imports so it stays usable as the
application shell is built out in later releases. Environment defaults are set
here (before any application code is imported) so tests never depend on a
developer's shell environment or a live database.
"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/app_template_test"
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root() -> Path:
    """Absolute path to the backend project root (contains pyproject.toml)."""
    return BACKEND_ROOT
