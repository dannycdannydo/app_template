"""Typed application configuration (blueprint §27).

One pydantic-settings model is the single source of configuration; there are
no scattered ``os.getenv`` calls. The application fails fast on invalid
production configuration.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
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
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis URL for distributed API rate limiting",
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
        description="WorkOS API base URL used for the JWKS and Management API",
    )
    workos_jwt_issuer: str = Field(
        default="https://api.workos.com/",
        description="Exact trusted WorkOS access-token iss claim",
    )
    workos_jwt_leeway: float = Field(
        default=30.0,
        description="Clock-skew leeway in seconds when validating WorkOS session tokens",
    )
    bootstrap_platform_admin_email: str = Field(
        default="",
        description=(
            "Email of the user the platform bootstrap grants platform_admin to on first "
            "verified login; empty disables the bootstrap (Scope §6.4)"
        ),
    )
    workos_webhook_secret: str = Field(
        default="",
        description=(
            "WorkOS webhook endpoint secret used to verify the signature of inbound "
            "webhook deliveries (Scope §6.8, BP §30). Empty disables webhook "
            "processing: the endpoint then rejects every delivery (fail-closed); "
            "login-time reconciliation stays authoritative either way"
        ),
    )
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173"],
        description="Exact browser origins allowed to call the API",
    )
    trusted_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "test"],
        description="Host headers accepted by the API; configure real public hosts in production",
    )

    @field_validator("cors_allowed_origins")
    @classmethod
    def _validate_cors_allowed_origins(cls, origins: list[str]) -> list[str]:
        if not origins or any(not origin for origin in origins):
            raise ValueError("cors_allowed_origins must contain at least one origin")
        if any(origin == "*" for origin in origins):
            raise ValueError("cors_allowed_origins must not contain a wildcard origin")
        return [origin.rstrip("/") for origin in origins]

    @field_validator("trusted_hosts")
    @classmethod
    def _validate_trusted_hosts(cls, hosts: list[str]) -> list[str]:
        if not hosts or any(not host or "://" in host for host in hosts):
            raise ValueError("trusted_hosts must contain hostnames only")
        if any(host == "*" for host in hosts):
            raise ValueError("trusted_hosts must not contain a wildcard host")
        return hosts

    @field_validator("bootstrap_platform_admin_email")
    @classmethod
    def _validate_bootstrap_platform_admin_email(cls, email: str) -> str:
        """Normalise and validate the bootstrap email when one is configured.

        The value is trimmed and lower-cased so the login-time comparison is
        stable; a malformed value fails fast in every environment, and
        production is never left with a bootstrap email the code cannot act
        on. An empty value (bootstrap disabled) is always allowed.
        """
        value = email.strip().lower()
        if not value:
            return ""
        local, separator, domain = value.partition("@")
        if not separator or not local or not domain or "@" in domain or " " in value:
            raise ValueError("bootstrap_platform_admin_email must be a valid email address")
        return value

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
        if self.app_env == "production" and self.trusted_hosts == [
            "localhost",
            "127.0.0.1",
            "test",
        ]:
            raise ValueError(
                "trusted_hosts must be explicitly configured in the production environment"
            )
        if self.app_env == "production" and not self.redis_url.startswith("rediss://"):
            raise ValueError("redis_url must use rediss in the production environment")
        if self.app_env == "production" and any(
            not origin.startswith("https://") for origin in self.cors_allowed_origins
        ):
            raise ValueError("cors_allowed_origins must use https in the production environment")
        if self.workos_api_base_url and not self.workos_api_base_url.startswith("https://"):
            raise ValueError("workos_api_base_url must use https")
        if not self.workos_jwt_issuer:
            raise ValueError("workos_jwt_issuer is required")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
