"""Typed application configuration (blueprint §27).

One pydantic-settings model is the single source of configuration; there are
no scattered ``os.getenv`` calls. The application fails fast on invalid
production configuration.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Field names map to upper-case environment variables (e.g. ``app_env`` is
    read from ``APP_ENV``). Values are also read from a ``.env`` file in the
    working directory when present.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "app-template"
    app_env: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    database_url: str = Field(
        default="",
        description="Async SQLAlchemy database URL, e.g. postgresql+asyncpg://user:pass@host:5432/db",
    )
    workos_api_key: str = Field(
        default="",
        description="WorkOS secret API key; used to fetch user profiles when provisioning internal users",
    )
    workos_client_id: str = Field(
        default="",
        description="WorkOS client id; doubles as the audience for session tokens and the JWKS path",
    )
    workos_api_base_url: str = Field(
        default="https://api.workos.com/",
        description="WorkOS API base URL; session tokens must be issued by the matching issuer",
    )
    workos_jwt_leeway: float = Field(
        default=30.0,
        description="Clock-skew leeway in seconds when validating WorkOS session tokens",
    )

    @model_validator(mode="after")
    def _validate_config(self) -> Settings:
        if not self.database_url.startswith(("postgresql", "postgres")):
            raise ValueError("database_url must be a PostgreSQL URL")
        if self.app_env == "production" and self.debug:
            raise ValueError("debug must be False in the production environment")
        if self.app_env == "production" and not self.workos_api_key:
            raise ValueError("workos_api_key is required in the production environment")
        if self.app_env == "production" and not self.workos_client_id:
            raise ValueError("workos_client_id is required in the production environment")
        if self.workos_api_base_url and not self.workos_api_base_url.startswith("https://"):
            raise ValueError("workos_api_base_url must use https")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
