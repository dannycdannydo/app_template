"""Tests for typed configuration and fail-fast validation (§27)."""

import pytest
from app.core.config import Settings
from pydantic import ValidationError


def test_settings_load_from_environment_defaults() -> None:
    settings = Settings(app_env="test", database_url="postgresql+asyncpg://x")
    assert settings.app_name == "app-template"
    assert settings.debug is False
    assert settings.log_level == "INFO"


def test_production_rejects_debug() -> None:
    with pytest.raises(ValidationError, match="debug"):
        Settings(app_env="production", debug=True, database_url="postgresql+asyncpg://x")


def test_rejects_non_postgres_database_url() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(app_env="development", debug=False, database_url="sqlite:///app.db")


def test_rejects_missing_database_url() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(app_env="development", database_url="")
