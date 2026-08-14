"""Typed application configuration (blueprint §27).

One pydantic-settings model is the single source of configuration; there are
no scattered ``os.getenv`` calls. The application fails fast on invalid
production configuration.
"""

from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.endpoint_safety import validate_local_endpoint

# Every AI provider adapter the typed settings can enable (v0.7 Scope §6.3).
# The factory re-exports this set so configuration and construction can never
# disagree about what a valid provider id is.
AI_KNOWN_PROVIDER_IDS = frozenset(
    {"fake", "openai", "anthropic", "deepseek", "azure_openai", "vertex", "local"}
)

# Validated OpenAI data-processing regions (v0.7 Scope §6.3 regional
# amendment, ADR-0017). OpenAI regional projects are served by the
# corresponding regional domain (``eu.api.openai.com`` / ``us.api.openai.com``),
# so the region derives the endpoint; an explicit base URL override must name
# the same regional domain or configuration fails fast. Empty means "the
# provider default (global endpoint)".
AI_OPENAI_SUPPORTED_REGIONS = frozenset({"us", "eu"})

# Validated Anthropic inference geographies (v0.7 Scope §6.3 regional
# amendment, ADR-0017). ``inference_geo`` is a top-level ``POST /v1/messages``
# field (never a header) and only ``global`` and ``us`` exist; empty means
# "the provider default (global)". Only Claude 4.6+ models accept the field,
# so the registry pins a compatible model.
AI_ANTHROPIC_SUPPORTED_INFERENCE_GEOGRAPHIES = frozenset({"global", "us"})

# Required configuration per enabled provider: (env var name, settings field).
# An enabled provider missing any of these fails fast at configuration time in
# every environment (Scope §6.3/§6.7), never at request time. Keys and Google
# credential material stay server-side secrets.
_AI_PROVIDER_REQUIRED_SETTINGS: dict[str, list[tuple[str, str]]] = {
    "fake": [],
    "openai": [("AI_OPENAI_API_KEY", "ai_openai_api_key")],
    "anthropic": [("AI_ANTHROPIC_API_KEY", "ai_anthropic_api_key")],
    "deepseek": [("AI_DEEPSEEK_API_KEY", "ai_deepseek_api_key")],
    "azure_openai": [
        ("AI_AZURE_OPENAI_ENDPOINT", "ai_azure_openai_endpoint"),
        ("AI_AZURE_OPENAI_API_KEY", "ai_azure_openai_api_key"),
    ],
    "vertex": [
        ("AI_VERTEX_PROJECT", "ai_vertex_project"),
        ("AI_VERTEX_LOCATION", "ai_vertex_location"),
    ],
    "local": [("AI_LOCAL_BASE_URL", "ai_local_base_url")],
}


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
    bootstrap_platform_admin_password: str = Field(
        default="",
        description=(
            "Password for the bootstrap platform admin, used only by the "
            "scripts.provision_bootstrap_admin command to pre-create the WorkOS "
            "user (signups disabled). Empty means the command refuses to run; never "
            "shared with the frontend"
        ),
    )
    bootstrap_platform_admin_org: str = Field(
        default="",
        description=(
            "Name of the organisation the one-time platform bootstrap also creates "
            "and makes the bootstrap admin an owner of, so the default admin is never "
            "left without an organisation (org-scoped screens otherwise have no "
            "tenant to act within). Empty disables org creation (Scope §6.4)"
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
    storage_provider: str = Field(
        default="s3",
        description=(
            "Object storage adapter: 's3' (S3-compatible, the default) or 'fake' "
            "(in-memory, test-only). The 'fake' provider is rejected in production."
        ),
    )
    storage_bucket: str = Field(
        default="",
        description="Object storage bucket name; required when storage_provider=s3",
    )
    storage_endpoint_url: str = Field(
        default="",
        description="S3-compatible endpoint (e.g. MinIO); required in production",
    )
    storage_public_endpoint_url: str = Field(
        default="",
        description=(
            "Storage endpoint used when presigning URLs, for deployments where the "
            "browser cannot reach the API's storage host (e.g. dev-docker). "
            "Defaults to storage_endpoint_url."
        ),
    )
    storage_region: str = Field(
        default="",
        description="S3 region; the adapter falls back to us-east-1 when empty",
    )
    storage_access_key_id: str = Field(
        default="",
        description="S3 access key id (secret, backend-only)",
    )
    storage_secret_access_key: str = Field(
        default="",
        description="S3 secret access key (secret, backend-only)",
    )
    storage_max_upload_size: int = Field(
        default=25 * 1024 * 1024,
        description="Maximum accepted upload size in bytes (blueprint §30)",
    )
    storage_allowed_content_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "application/json",
            "text/plain",
            "text/csv",
            "image/png",
            "image/jpeg",
        ],
        description="Content types accepted for uploads (blueprint §30)",
    )
    worker_concurrency: int = Field(
        default=8,
        description=(
            "Dramatiq worker threads per process (blueprint §18: worker "
            "concurrency must be configurable); passed to the worker CLI by "
            "`make worker` and the dev-docker worker service"
        ),
    )
    sentry_dsn: str = Field(
        default="",
        description=(
            "Sentry DSN (blueprint §28). Empty disables Sentry entirely: the app "
            "boots without initialising the SDK and nothing is captured."
        ),
    )
    sentry_environment: str = Field(
        default="",
        description="Sentry environment label; defaults to APP_ENV when empty.",
    )
    sentry_traces_sample_rate: float = Field(
        default=0.1,
        description="Sentry performance-trace sample rate between 0.0 and 1.0.",
    )
    email_provider: str = Field(
        default="smtp",
        description=(
            "Email adapter (ADR-0015): 'smtp' (standard-library smtplib, the "
            "default) or 'fake' (in-memory, test-only). The 'fake' provider "
            "is rejected in production."
        ),
    )
    email_from: str = Field(
        default="",
        description="Sender address for application email; required when email_provider=smtp",
    )
    smtp_host: str = Field(
        default="",
        description="SMTP relay host; required when email_provider=smtp (Mailhog locally)",
    )
    smtp_port: int = Field(
        default=0,
        description="SMTP relay port; 0 means unset (Mailhog publishes 1025 locally)",
    )
    smtp_username: str = Field(
        default="",
        description="SMTP username when the relay requires authentication",
    )
    smtp_password: str = Field(
        default="",
        description="SMTP password (secret, backend-only, never logged)",
    )
    smtp_use_tls: bool = Field(
        default=False,
        description="Enable STARTTLS when connecting to the SMTP relay",
    )
    # AI layer (v0.7 Scope §6.3, ADR-0017): provider enablement plus
    # endpoint/project/deployment identifiers only. Keys, Azure credentials and
    # Google credential material are server-side secrets — they live in these
    # settings (read from environment/.env) but never in the API, frontend,
    # logs, Sentry or audit metadata. The fake provider is the default test
    # adapter and is rejected in production (Scope §6.7).
    ai_enabled_providers: list[str] = Field(
        default_factory=lambda: ["fake"],
        description=(
            "Enabled AI provider adapters, from: fake, openai, anthropic, deepseek, "
            "azure_openai, vertex, local. 'fake' is the deterministic test adapter "
            "and is rejected in the production environment."
        ),
    )
    ai_http_timeout_seconds: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description="Per-request timeout for every AI provider adapter (seconds)",
    )
    ai_openai_api_key: str = Field(
        default="",
        description="OpenAI API key (server-side secret, backend-only)",
    )
    ai_openai_base_url: str = Field(
        default="",
        description="Optional OpenAI-compatible base URL override; empty uses https://api.openai.com/v1",
    )
    ai_openai_region: str = Field(
        default="",
        description=(
            "Validated OpenAI data-processing region: 'us' or 'eu' (approved-account "
            "data-residency opt-in), or empty for the provider default. A set region "
            "derives the regional endpoint (us.api.openai.com / eu.api.openai.com); an "
            "explicit AI_OPENAI_BASE_URL must name the same domain. Recorded in AI "
            "routing metadata; never implicitly changed by fallback (v0.7 Scope §6.3 "
            "regional amendment, ADR-0017)"
        ),
    )
    ai_anthropic_api_key: str = Field(
        default="",
        description="Anthropic API key (server-side secret, backend-only)",
    )
    ai_anthropic_base_url: str = Field(
        default="",
        description="Optional Anthropic base URL override; empty uses https://api.anthropic.com",
    )
    ai_anthropic_inference_geography: str = Field(
        default="",
        description=(
            "Validated Anthropic inference geography: 'global' (provider default) or "
            "'us' (US-only inference); sent as the top-level inference_geo field on "
            "POST /v1/messages, which only Claude 4.6+ models accept. Recorded in "
            "AI routing metadata; never implicitly changed by fallback (v0.7 Scope §6.3 "
            "regional amendment, ADR-0017)"
        ),
    )
    ai_deepseek_api_key: str = Field(
        default="",
        description="DeepSeek API key (server-side secret, backend-only)",
    )
    ai_deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        description="DeepSeek OpenAI-compatible base URL",
    )
    ai_azure_openai_api_key: str = Field(
        default="",
        description="Azure OpenAI resource key (server-side secret, backend-only)",
    )
    ai_azure_openai_endpoint: str = Field(
        default="",
        description=(
            "Azure OpenAI resource endpoint, e.g. https://my-resource.openai.azure.com; "
            "the registry's model field names the deployment"
        ),
    )
    ai_azure_openai_api_version: str = Field(
        default="2024-08-01-preview",
        description="Pinned Azure OpenAI api-version query parameter (reviewed configuration)",
    )
    ai_vertex_project: str = Field(
        default="",
        description="Google Cloud project id for Vertex AI Gemini (data-residency scoped)",
    )
    ai_vertex_location: str = Field(
        default="",
        description=(
            "Vertex AI location, e.g. europe-west1; explicit so deployments pin a "
            "data-residency region (ADR-0018)"
        ),
    )
    ai_vertex_credentials_path: str = Field(
        default="",
        description=(
            "Optional path to a service-account key JSON injected via the deployment "
            "secret mechanism; empty uses Application Default Credentials (ADR-0018)"
        ),
    )
    ai_local_base_url: str = Field(
        default="",
        description=(
            "Private OpenAI-compatible endpoint (Ollama/vLLM/SGLang), e.g. "
            "http://127.0.0.1:11434/v1; http is allowed only for loopback/private "
            "hosts, never exposed to browsers"
        ),
    )
    ai_local_api_key: str = Field(
        default="",
        description="Optional API key for the private local provider (vLLM deployments)",
    )
    # v0.8 Scope §2.2/§6.2: deployment-level transfer-mode configuration.
    # Non-inline modes are default-deny — an empty list means only the inline
    # mode is deployed — and enabling one requires the supporting provider and
    # configuration to be present (fail fast at startup, never at request
    # time, BP §27). The aggregate inline threshold can never be configured
    # above 5,000,000 bytes and the large-file ceiling can never exceed
    # 50,000,000 bytes; the managed signed-URL TTL defaults to 900 seconds and
    # is capped at 1,800; the Vertex staging bucket is user-provisioned and
    # never created or configured by the application (Scope §2.2/§2.4).
    ai_enabled_transfer_modes: list[str] = Field(
        default_factory=list,
        description=(
            "Non-inline AI transfer modes enabled at deployment level; empty "
            "(the default) deploys inline only. Enabling a mode requires an "
            "enabled provider whose reviewed contract supports it plus the "
            "mode's configuration (v0.8 Scope §2.2)."
        ),
    )
    ai_inline_aggregate_threshold_bytes: int = Field(
        default=5_000_000,
        ge=1,
        le=5_000_000,
        description=(
            "Aggregate raw attachment byte threshold below which inline is the "
            "only eligible mode; cannot be configured above 5,000,000 bytes "
            "(v0.8 Scope §2.2)"
        ),
    )
    ai_max_large_attachment_bytes: int = Field(
        default=50_000_000,
        ge=1,
        le=50_000_000,
        description=(
            "Template ceiling for exactly one non-inline PDF attachment; cannot "
            "be configured above 50,000,000 bytes (v0.8 Scope §2.2)"
        ),
    )
    ai_upload_expiry_seconds: int = Field(
        default=3_600,
        ge=1,
        le=2_592_000,
        description=(
            "Expiry for transient provider-hosted uploaded files (seconds); "
            "bounded by the reviewed provider contracts (v0.8 Scope §2.2)"
        ),
    )
    ai_managed_url_ttl_seconds: int = Field(
        default=900,
        ge=1,
        le=1_800,
        description=(
            "Managed signed-URL TTL for retained private sources (seconds); "
            "default 900, maximum 1,800 (v0.8 Scope §2.2)"
        ),
    )
    ai_vertex_temp_gcs_bucket: str = Field(
        default="",
        description=(
            "User-provisioned private same-region GCS staging bucket used for "
            "the Vertex storage-reference mode; never created or configured by "
            "the application, and required only when that mode is enabled "
            "(v0.8 Scope §2.4)"
        ),
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

    @field_validator("storage_provider")
    @classmethod
    def _validate_storage_provider(cls, provider: str) -> str:
        if provider not in {"s3", "fake"}:
            raise ValueError("storage_provider must be 's3' or 'fake'")
        return provider

    @field_validator("storage_max_upload_size")
    @classmethod
    def _validate_storage_max_upload_size(cls, size: int) -> int:
        if size <= 0:
            raise ValueError("storage_max_upload_size must be positive")
        return size

    @field_validator("storage_allowed_content_types")
    @classmethod
    def _validate_storage_allowed_content_types(cls, content_types: list[str]) -> list[str]:
        if not content_types:
            raise ValueError("storage_allowed_content_types must not be empty")
        if any("/" not in value or " " in value for value in content_types):
            raise ValueError("storage_allowed_content_types must contain valid MIME types")
        return content_types

    @field_validator("worker_concurrency")
    @classmethod
    def _validate_worker_concurrency(cls, concurrency: int) -> int:
        if concurrency < 1:
            raise ValueError("worker_concurrency must be at least 1")
        return concurrency

    @field_validator("sentry_traces_sample_rate")
    @classmethod
    def _validate_sentry_traces_sample_rate(cls, sample_rate: float) -> float:
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError("sentry_traces_sample_rate must be between 0.0 and 1.0")
        return sample_rate

    @field_validator("email_provider")
    @classmethod
    def _validate_email_provider(cls, provider: str) -> str:
        if provider not in {"smtp", "fake"}:
            raise ValueError("email_provider must be 'smtp' or 'fake'")
        return provider

    @field_validator("smtp_port")
    @classmethod
    def _validate_smtp_port(cls, port: int) -> int:
        if not 0 <= port <= 65535:
            raise ValueError("smtp_port must be between 0 and 65535")
        return port

    @field_validator("ai_enabled_providers")
    @classmethod
    def _validate_ai_enabled_providers(cls, providers: list[str]) -> list[str]:
        cleaned = [provider.strip().lower() for provider in providers]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("ai_enabled_providers must not contain duplicates")
        unknown = set(cleaned) - AI_KNOWN_PROVIDER_IDS
        if unknown:
            raise ValueError(
                f"unknown AI providers in ai_enabled_providers: {sorted(unknown)}; "
                f"known: {sorted(AI_KNOWN_PROVIDER_IDS)}"
            )
        return cleaned

    @field_validator("ai_azure_openai_endpoint")
    @classmethod
    def _validate_ai_azure_openai_endpoint(cls, endpoint: str) -> str:
        if not endpoint:
            return ""
        if not endpoint.startswith("https://") or not urlsplit(endpoint).hostname:
            raise ValueError("ai_azure_openai_endpoint must be an https URL")
        return endpoint.rstrip("/")

    @field_validator("ai_azure_openai_api_version")
    @classmethod
    def _validate_ai_azure_openai_api_version(cls, version: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(-preview)?", version):
            raise ValueError("ai_azure_openai_api_version must look like YYYY-MM-DD(-preview)")
        return version

    @field_validator("ai_openai_base_url", "ai_anthropic_base_url", "ai_deepseek_base_url")
    @classmethod
    def _validate_ai_provider_base_url(cls, base_url: str) -> str:
        """Provider base-url overrides must be https or a private http endpoint.

        The same safety rule as the local provider: a public plain-HTTP
        endpoint would ship API keys over an unauthenticated link (Scope §6.3,
        §6.7 fail-fast list).
        """
        if not base_url:
            return ""
        return validate_local_endpoint(base_url)

    @field_validator("ai_openai_region")
    @classmethod
    def _validate_ai_openai_region(cls, region: str) -> str:
        """OpenAI regions are explicit, validated deployment configuration.

        Empty is the honest ordinary-account default (US); any other value must
        be a reviewed supported region so a typo can never silently route data
        to an unintended processing location (v0.7 Scope §6.3 regional
        amendment, ADR-0017).
        """
        value = region.lower()
        if value and value not in AI_OPENAI_SUPPORTED_REGIONS:
            raise ValueError(
                f"ai_openai_region must be empty or one of "
                f"{sorted(AI_OPENAI_SUPPORTED_REGIONS)}, got {region!r}"
            )
        return value

    @field_validator("ai_anthropic_inference_geography")
    @classmethod
    def _validate_ai_anthropic_inference_geography(cls, geography: str) -> str:
        """Anthropic inference geographies are explicit, validated configuration.

        Empty means the provider default (global); supported values map to the
        top-level ``inference_geo`` field. An unsupported value fails fast so
        data residency is never silently misconfigured (v0.7 Scope §6.3
        regional amendment, ADR-0017).
        """
        value = geography.lower()
        if value and value not in AI_ANTHROPIC_SUPPORTED_INFERENCE_GEOGRAPHIES:
            raise ValueError(
                "ai_anthropic_inference_geography must be empty or one of "
                f"{sorted(AI_ANTHROPIC_SUPPORTED_INFERENCE_GEOGRAPHIES)}, got {geography!r}"
            )
        return value

    @field_validator("ai_local_base_url")
    @classmethod
    def _validate_ai_local_base_url(cls, base_url: str) -> str:
        if not base_url:
            return ""
        return validate_local_endpoint(base_url)

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
        if self.app_env == "production" and not self._redis_url_is_production_safe():
            raise ValueError(
                "redis_url must use rediss in the production environment unless "
                "Redis is on loopback or a private compose-network host"
            )
        if self.app_env == "production" and any(
            not origin.startswith("https://") for origin in self.cors_allowed_origins
        ):
            raise ValueError("cors_allowed_origins must use https in the production environment")
        if self.workos_api_base_url and not self.workos_api_base_url.startswith("https://"):
            raise ValueError("workos_api_base_url must use https")
        if not self.workos_jwt_issuer:
            raise ValueError("workos_jwt_issuer is required")
        if not self.storage_public_endpoint_url:
            self.storage_public_endpoint_url = self.storage_endpoint_url
        if not self.sentry_environment:
            self.sentry_environment = self.app_env
        if self.app_env == "production":
            if self.storage_provider == "fake":
                raise ValueError(
                    "storage_provider must not be 'fake' in the production environment"
                )
            if self.storage_provider == "s3":
                missing = [
                    name
                    for name, value in (
                        ("storage_access_key_id", self.storage_access_key_id),
                        ("storage_secret_access_key", self.storage_secret_access_key),
                        ("storage_bucket", self.storage_bucket),
                        ("storage_endpoint_url", self.storage_endpoint_url),
                    )
                    if not value
                ]
                if missing:
                    raise ValueError(
                        "storage_provider=s3 requires explicit storage configuration "
                        f"in the production environment: {', '.join(missing)}"
                    )
        if self.app_env == "production" and self.email_provider == "fake":
            raise ValueError("email_provider must not be 'fake' in the production environment")
        if self.app_env == "production" and self.email_provider == "smtp":
            missing_email = [
                name
                for name, value in (
                    ("email_from", self.email_from),
                    ("smtp_host", self.smtp_host),
                    ("smtp_port", str(self.smtp_port) if self.smtp_port else ""),
                )
                if not value
            ]
            if missing_email:
                raise ValueError(
                    "email_provider=smtp requires explicit email configuration "
                    f"in the production environment: {', '.join(missing_email)}"
                )
        # AI providers (v0.7 Scope §6.3/§6.7): an enabled provider must be
        # fully configured in every environment (fail fast at startup, never
        # at request time), and the fake test adapter is rejected in
        # production alongside test/fake provider configurations.
        for provider_id in self.ai_enabled_providers:
            missing_ai = [
                env_name
                for env_name, field_name in _AI_PROVIDER_REQUIRED_SETTINGS[provider_id]
                if not getattr(self, field_name)
            ]
            if missing_ai:
                raise ValueError(
                    f"AI provider {provider_id!r} is enabled but requires configuration: "
                    f"{', '.join(missing_ai)}"
                )
        if self.app_env == "production" and "fake" in self.ai_enabled_providers:
            raise ValueError(
                "ai_enabled_providers must not include 'fake' in the production environment"
            )
        # A set OpenAI region must never be mislabelled: requests are routed
        # through the matching regional domain, so an explicit base URL
        # override that points elsewhere is a configuration conflict, not a
        # silent mis-routing (v0.7 Scope §6.3 regional amendment, ADR-0017).
        if self.ai_openai_region and self.ai_openai_base_url:
            base_host = urlsplit(self.ai_openai_base_url).hostname or ""
            expected_host = f"{self.ai_openai_region}.api.openai.com"
            if base_host != expected_host:
                raise ValueError(
                    "ai_openai_base_url host "
                    f"{base_host!r} conflicts with ai_openai_region {self.ai_openai_region!r}; "
                    f"regional requests must use https://{expected_host}/v1"
                )
        # v0.8 Scope §2.2/§6.2: deployment-level transfer-mode configuration
        # must be complete and compatible, failing fast at startup (BP §27),
        # never at request time. The cross-field checks live in the AI layer
        # (``app.ai.deployment``) because they name the reviewed transfer
        # modes and consult the provider contract fixture; the import is
        # deferred to keep the ``app.core.config`` boundary intact and the
        # mode names out of this module (v0.8 Scope §6.1 import-boundary
        # rule). Default-deny: an empty ``ai_enabled_transfer_modes`` is
        # always valid and requires no cloud configuration.
        if self.ai_enabled_transfer_modes:
            from app.ai.deployment import validate_transfer_deployment

            validate_transfer_deployment(
                enabled_transfer_modes=self.ai_enabled_transfer_modes,
                enabled_providers=self.ai_enabled_providers,
                inline_aggregate_threshold_bytes=self.ai_inline_aggregate_threshold_bytes,
                max_large_attachment_bytes=self.ai_max_large_attachment_bytes,
                upload_expiry_seconds=self.ai_upload_expiry_seconds,
                managed_url_ttl_seconds=self.ai_managed_url_ttl_seconds,
                vertex_temp_gcs_bucket=self.ai_vertex_temp_gcs_bucket,
                vertex_project=self.ai_vertex_project,
                vertex_location=self.ai_vertex_location,
                storage_presign_endpoint=self.storage_public_endpoint_url,
            )
        return self

    def _redis_url_is_production_safe(self) -> bool:
        """TLS is required for production Redis unless it is unreachable from
        the public network.

        The hybrid VPS profile (Scope §6.6) runs a private, password-protected
        Redis on the compose network, reachable only by the single-label
        service name ``redis`` and never published; loopback covers local
        containers. Any other Redis host (a managed/external instance, a
        dotted hostname, or any non-loopback IP address, IPv4 or IPv6) is
        reachable over a network an attacker may observe, so plaintext
        ``redis://`` is rejected there.
        """
        if self.redis_url.startswith("rediss://"):
            return True
        host = (urlsplit(self.redis_url).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not ip.is_loopback:
            return False
        # Single-label hostnames resolve only on the private compose network.
        return "." not in host


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
