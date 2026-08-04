"""Shared pytest fixtures and configuration hooks for the backend test suite.

Keep this file free of application imports so it stays usable as the
application shell is built out in later releases.
"""

from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root() -> Path:
    """Absolute path to the backend project root (contains pyproject.toml)."""
    return BACKEND_ROOT
