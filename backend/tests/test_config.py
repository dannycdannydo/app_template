"""Tests for typed configuration and fail-fast validation (§27)."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


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


def test_production_requires_workos_credentials() -> None:
    with pytest.raises(ValidationError, match="workos_api_key"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_client_id="client_1",
        )
    with pytest.raises(ValidationError, match="workos_client_id"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
        )
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://x",
        workos_api_key="sk_test",
        workos_client_id="client_1",
        cors_allowed_origins=["https://app.example.test"],
        trusted_hosts=["api.example.test"],
        redis_url="rediss://redis.example.test:6380/0",
    )
    assert settings.workos_api_base_url == "https://api.workos.com/"
    assert settings.workos_jwt_leeway == 30.0


def test_development_does_not_require_workos_credentials() -> None:
    settings = Settings(app_env="development", database_url="postgresql+asyncpg://x")
    assert settings.workos_api_key == ""


def test_cors_requires_explicit_non_wildcard_origins() -> None:
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        cors_allowed_origins=["http://localhost:5173/"],
    )
    assert settings.cors_allowed_origins == ["http://localhost:5173"]

    with pytest.raises(ValidationError, match="wildcard"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            cors_allowed_origins=["*"],
        )

    with pytest.raises(ValidationError, match="must use https"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["http://localhost:5173"],
            trusted_hosts=["api.example.test"],
            redis_url="rediss://redis.example.test:6380/0",
        )


def test_rejects_insecure_workos_base_url() -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            workos_api_base_url="http://api.workos.com/",
        )


def test_trusted_hosts_are_explicit_and_non_wildcard() -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            trusted_hosts=["*"],
        )

    with pytest.raises(ValidationError, match="explicitly configured"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            redis_url="rediss://redis.example.test:6380/0",
        )
