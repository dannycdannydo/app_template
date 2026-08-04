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

    @model_validator(mode="after")
    def _validate_config(self) -> Settings:
        if not self.database_url.startswith(("postgresql", "postgres")):
            raise ValueError("database_url must be a PostgreSQL URL")
        if self.app_env == "production" and self.debug:
            raise ValueError("debug must be False in the production environment")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
