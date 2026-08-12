"""Tests for typed configuration and fail-fast validation (§27)."""

from pathlib import Path
from typing import Any

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
        storage_provider="s3",
        storage_access_key_id="ak_test",
        storage_secret_access_key="sk_storage_test",
        storage_bucket="files",
        storage_endpoint_url="https://s3.example.test",
        email_provider="smtp",
        email_from="no-reply@example.com",
        smtp_host="smtp.example.test",
        smtp_port=587,
        ai_enabled_providers=[],
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


def test_bootstrap_email_defaults_to_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(app_env="development", database_url="postgresql+asyncpg://x")
    assert settings.bootstrap_platform_admin_email == ""


def test_bootstrap_org_defaults_to_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset BOOTSTRAP_PLATFORM_ADMIN_ORG means the grant skips org creation."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(app_env="development", database_url="postgresql+asyncpg://x")
    assert settings.bootstrap_platform_admin_org == ""


def test_bootstrap_email_is_normalised() -> None:
    """Scope §6.4: whitespace and case are folded so the login match is stable."""
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        bootstrap_platform_admin_email="  Admin@Example.COM ",
    )
    assert settings.bootstrap_platform_admin_email == "admin@example.com"


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "with space@example.com", "@example.com", "admin@", "admin@@example.com"],
)
def test_bootstrap_email_rejects_malformed_values(email: str) -> None:
    """A malformed bootstrap email fails fast in every environment."""
    with pytest.raises(ValidationError, match="valid email"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            bootstrap_platform_admin_email=email,
        )


def test_production_accepts_a_valid_bootstrap_email() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://x",
        workos_api_key="sk_test",
        workos_client_id="client_1",
        cors_allowed_origins=["https://app.example.test"],
        trusted_hosts=["api.example.test"],
        redis_url="rediss://redis.example.test:6380/0",
        storage_provider="s3",
        storage_access_key_id="ak_test",
        storage_secret_access_key="sk_storage_test",
        storage_bucket="files",
        storage_endpoint_url="https://s3.example.test",
        email_provider="smtp",
        email_from="no-reply@example.com",
        smtp_host="smtp.example.test",
        smtp_port=587,
        bootstrap_platform_admin_email="admin@example.com",
        ai_enabled_providers=[],
    )
    assert settings.bootstrap_platform_admin_email == "admin@example.com"


def test_webhook_secret_defaults_to_disabled() -> None:
    """Scope §6.8: unset means webhook processing is off (fail-closed)."""
    settings = Settings(app_env="development", database_url="postgresql+asyncpg://x")
    assert settings.workos_webhook_secret == ""


def test_webhook_secret_loads_from_environment() -> None:
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        workos_webhook_secret="whsec_test",
    )
    assert settings.workos_webhook_secret == "whsec_test"


def test_production_accepts_a_webhook_secret() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://x",
        workos_api_key="sk_test",
        workos_client_id="client_1",
        cors_allowed_origins=["https://app.example.test"],
        trusted_hosts=["api.example.test"],
        redis_url="rediss://redis.example.test:6380/0",
        storage_provider="s3",
        storage_access_key_id="ak_test",
        storage_secret_access_key="sk_storage_test",
        storage_bucket="files",
        storage_endpoint_url="https://s3.example.test",
        email_provider="smtp",
        email_from="no-reply@example.com",
        smtp_host="smtp.example.test",
        smtp_port=587,
        workos_webhook_secret="whsec_prod",
        ai_enabled_providers=[],
    )
    assert settings.workos_webhook_secret == "whsec_prod"


# --- Object storage settings (Scope §6.1, blueprint §17) ---


def test_storage_defaults_are_s3_with_dev_sensible_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope §6.1: the provider defaults to s3; limits are dev-friendly."""
    # A developer's shell may export STORAGE_* values; unset them so the field
    # defaults (not the shell) are what this test asserts.
    for var in (
        "STORAGE_ENDPOINT_URL",
        "STORAGE_PUBLIC_ENDPOINT_URL",
        "STORAGE_REGION",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        storage_provider="s3",  # explicit: conftest pins STORAGE_PROVIDER=fake for the suite
        storage_bucket="",  # explicit: conftest pins STORAGE_BUCKET=test-bucket for the suite
    )
    assert settings.storage_provider == "s3"
    assert settings.storage_bucket == ""
    assert settings.storage_endpoint_url == ""
    assert settings.storage_public_endpoint_url == ""
    assert settings.storage_region == ""
    assert settings.storage_max_upload_size == 25 * 1024 * 1024
    assert "application/pdf" in settings.storage_allowed_content_types
    assert settings.worker_concurrency == 8


def test_worker_concurrency_must_be_at_least_one() -> None:
    """Scope §6.2: blueprint §18 worker concurrency is configurable and sane."""
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        worker_concurrency=4,
    )
    assert settings.worker_concurrency == 4
    with pytest.raises(ValidationError, match="worker_concurrency"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            worker_concurrency=0,
        )


def test_storage_public_endpoint_defaults_to_endpoint() -> None:
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        storage_endpoint_url="http://localhost:9000",
    )
    assert settings.storage_public_endpoint_url == "http://localhost:9000"


def test_sentry_settings_defaults_and_validation() -> None:
    """Sentry is optional and off by default; the sample rate is bounded.

    The environment label falls back to APP_ENV when empty (Scope §6.1,
    blueprint §28): the application boots with no Sentry unless a DSN is
    configured, and production is not forced to set one.
    """
    settings = Settings(app_env="staging", database_url="postgresql+asyncpg://x")
    assert settings.sentry_dsn == ""
    assert settings.sentry_environment == "staging"
    assert settings.sentry_traces_sample_rate == 0.1

    with pytest.raises(ValidationError, match="sentry_traces_sample_rate"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            sentry_traces_sample_rate=1.5,
        )
    with pytest.raises(ValidationError, match="sentry_traces_sample_rate"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            sentry_traces_sample_rate=-0.1,
        )
    assert (
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            sentry_traces_sample_rate=0.0,
        ).sentry_traces_sample_rate
        == 0.0
    )


def test_storage_public_endpoint_can_be_set_explicitly() -> None:
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        storage_endpoint_url="http://localhost:9000",
        storage_public_endpoint_url="http://minio:9000",
    )
    assert settings.storage_public_endpoint_url == "http://minio:9000"


def test_storage_provider_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError, match="storage_provider"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            storage_provider="gcs",
        )


def test_storage_max_upload_size_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="storage_max_upload_size"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            storage_max_upload_size=0,
        )


def test_storage_allowed_content_types_must_be_non_empty_and_valid() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            storage_allowed_content_types=[],
        )
    with pytest.raises(ValidationError, match="valid MIME types"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            storage_allowed_content_types=["application/pdf", "not-a-mime"],
        )


def test_production_rejects_fake_provider() -> None:
    with pytest.raises(ValidationError, match="'fake'"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            trusted_hosts=["api.example.test"],
            redis_url="rediss://redis.example.test:6380/0",
            storage_provider="fake",
        )


def test_production_requires_explicit_s3_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope §4: production with storage_provider=s3 must set credentials/bucket/endpoint."""
    # Unset leaked STORAGE_* shell exports so the "missing configuration"
    # scenarios below are what actually runs.
    for var in (
        "STORAGE_ACCESS_KEY_ID",
        "STORAGE_SECRET_ACCESS_KEY",
        "STORAGE_ENDPOINT_URL",
        "STORAGE_REGION",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError, match="storage_access_key_id"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            trusted_hosts=["api.example.test"],
            redis_url="rediss://redis.example.test:6380/0",
            storage_provider="s3",
            storage_secret_access_key="sk_storage_test",
            storage_bucket="files",
            storage_endpoint_url="https://s3.example.test",
            ai_enabled_providers=[],
        )

    with pytest.raises(ValidationError, match="storage_endpoint_url"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            trusted_hosts=["api.example.test"],
            redis_url="rediss://redis.example.test:6380/0",
            storage_provider="s3",
            storage_access_key_id="ak_test",
            storage_secret_access_key="sk_storage_test",
            storage_bucket="files",
            ai_enabled_providers=[],
        )


def test_production_accepts_complete_s3_configuration() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://x",
        workos_api_key="sk_test",
        workos_client_id="client_1",
        cors_allowed_origins=["https://app.example.test"],
        trusted_hosts=["api.example.test"],
        redis_url="rediss://redis.example.test:6380/0",
        storage_provider="s3",
        storage_access_key_id="ak_test",
        storage_secret_access_key="sk_storage_test",
        storage_bucket="files",
        storage_endpoint_url="https://s3.example.test",
        email_provider="smtp",
        email_from="no-reply@example.com",
        smtp_host="smtp.example.test",
        smtp_port=587,
        ai_enabled_providers=[],
    )
    assert settings.storage_provider == "s3"
    assert settings.storage_public_endpoint_url == "https://s3.example.test"


def test_production_accepts_private_compose_network_redis_without_tls() -> None:
    """Scope §6.6 / backup-and-recovery run B (defect D1): the hybrid VPS
    profile runs a private, password-protected Redis on the compose network,
    reachable only by the single-label service name ``redis`` and never
    published, so plain ``redis://`` over that private network is acceptable
    in production."""
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://x",
        workos_api_key="sk_test",
        workos_client_id="client_1",
        cors_allowed_origins=["https://app.example.test"],
        trusted_hosts=["api.example.test"],
        redis_url="redis://:secret@redis:6379/0",
        storage_provider="s3",
        storage_access_key_id="ak_test",
        storage_secret_access_key="sk_storage_test",
        storage_bucket="files",
        storage_endpoint_url="https://s3.example.test",
        email_provider="smtp",
        email_from="no-reply@example.com",
        smtp_host="smtp.example.test",
        smtp_port=587,
        ai_enabled_providers=[],
    )
    assert settings.redis_url.startswith("redis://")


def test_production_accepts_loopback_redis_without_tls() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://x",
        workos_api_key="sk_test",
        workos_client_id="client_1",
        cors_allowed_origins=["https://app.example.test"],
        trusted_hosts=["api.example.test"],
        redis_url="redis://localhost:6379/0",
        storage_provider="s3",
        storage_access_key_id="ak_test",
        storage_secret_access_key="sk_storage_test",
        storage_bucket="files",
        storage_endpoint_url="https://s3.example.test",
        email_provider="smtp",
        email_from="no-reply@example.com",
        smtp_host="smtp.example.test",
        smtp_port=587,
        ai_enabled_providers=[],
    )
    assert settings.redis_url.startswith("redis://")


def test_production_requires_tls_for_external_redis() -> None:
    """Any externally reachable Redis host (a dotted hostname or an IP) still
    requires TLS in production."""
    with pytest.raises(ValidationError, match="rediss"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            trusted_hosts=["api.example.test"],
            redis_url="redis://redis.example.test:6379/0",
            storage_provider="s3",
            storage_access_key_id="ak_test",
            storage_secret_access_key="sk_storage_test",
            storage_bucket="files",
            storage_endpoint_url="https://s3.example.test",
            email_provider="smtp",
            email_from="no-reply@example.com",
            smtp_host="smtp.example.test",
            smtp_port=587,
        )


def test_production_requires_tls_for_public_ipv6_redis() -> None:
    """A non-loopback IPv6 literal contains no dot, so the single-label
    compose-network heuristic must not mistake it for a private host (review
    should-fix on the D1 rule): plain ``redis://`` to a public IPv6 address is
    rejected in production."""
    with pytest.raises(ValidationError, match="rediss"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            trusted_hosts=["api.example.test"],
            redis_url="redis://[2001:db8::1]:6379/0",
            storage_provider="s3",
            storage_access_key_id="ak_test",
            storage_secret_access_key="sk_storage_test",
            storage_bucket="files",
            storage_endpoint_url="https://s3.example.test",
            email_provider="smtp",
            email_from="no-reply@example.com",
            smtp_host="smtp.example.test",
            smtp_port=587,
        )


# --- Email settings (Scope §6.2, blueprint §20, ADR-0015) ---


def test_email_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scope §6.2: smtp is the default provider; SMTP is unconfigured by default."""
    # Unset leaked EMAIL_FROM/SMTP_* shell exports so the field defaults are
    # what this test asserts.
    for var in (
        "EMAIL_FROM",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_USE_TLS",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        email_provider="smtp",  # explicit: conftest pins EMAIL_PROVIDER=fake for the suite
    )
    assert settings.email_provider == "smtp"
    assert settings.email_from == ""
    assert settings.smtp_host == ""
    assert settings.smtp_port == 0
    assert settings.smtp_username == ""
    assert settings.smtp_password == ""
    assert settings.smtp_use_tls is False


def test_email_provider_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError, match="email_provider"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            email_provider="resend",
        )


def test_smtp_port_rejects_values_outside_the_valid_range() -> None:
    with pytest.raises(ValidationError, match="smtp_port"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            smtp_port=70000,
        )
    with pytest.raises(ValidationError, match="smtp_port"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            smtp_port=-1,
        )


def test_production_rejects_fake_email_provider() -> None:
    """Scope §5.3: production never boots with the in-memory provider."""
    with pytest.raises(ValidationError, match="email_provider must not be 'fake'"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            trusted_hosts=["api.example.test"],
            redis_url="rediss://redis.example.test:6380/0",
            storage_provider="s3",
            storage_access_key_id="ak_test",
            storage_secret_access_key="sk_storage_test",
            storage_bucket="files",
            storage_endpoint_url="https://s3.example.test",
            email_provider="fake",
        )


def test_production_requires_explicit_smtp_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope §4: production with email_provider=smtp requires host/port/from."""
    # Unset leaked EMAIL_FROM/SMTP_* shell exports so the "missing
    # configuration" scenarios below are what actually runs.
    for var in (
        "EMAIL_FROM",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_USE_TLS",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError, match="smtp_host"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            trusted_hosts=["api.example.test"],
            redis_url="rediss://redis.example.test:6380/0",
            storage_provider="s3",
            storage_access_key_id="ak_test",
            storage_secret_access_key="sk_storage_test",
            storage_bucket="files",
            storage_endpoint_url="https://s3.example.test",
            email_provider="smtp",
            email_from="no-reply@example.com",
            smtp_port=587,
            ai_enabled_providers=[],
        )

    with pytest.raises(ValidationError, match="smtp_port"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            trusted_hosts=["api.example.test"],
            redis_url="rediss://redis.example.test:6380/0",
            storage_provider="s3",
            storage_access_key_id="ak_test",
            storage_secret_access_key="sk_storage_test",
            storage_bucket="files",
            storage_endpoint_url="https://s3.example.test",
            email_provider="smtp",
            email_from="no-reply@example.com",
            smtp_host="smtp.example.test",
            ai_enabled_providers=[],
        )

    with pytest.raises(ValidationError, match="email_from"):
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://x",
            workos_api_key="sk_test",
            workos_client_id="client_1",
            cors_allowed_origins=["https://app.example.test"],
            trusted_hosts=["api.example.test"],
            redis_url="rediss://redis.example.test:6380/0",
            storage_provider="s3",
            storage_access_key_id="ak_test",
            storage_secret_access_key="sk_storage_test",
            storage_bucket="files",
            storage_endpoint_url="https://s3.example.test",
            email_provider="smtp",
            smtp_host="smtp.example.test",
            smtp_port=587,
            ai_enabled_providers=[],
        )


def test_production_accepts_complete_smtp_configuration() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://x",
        workos_api_key="sk_test",
        workos_client_id="client_1",
        cors_allowed_origins=["https://app.example.test"],
        trusted_hosts=["api.example.test"],
        redis_url="rediss://redis.example.test:6380/0",
        storage_provider="s3",
        storage_access_key_id="ak_test",
        storage_secret_access_key="sk_storage_test",
        storage_bucket="files",
        storage_endpoint_url="https://s3.example.test",
        email_provider="smtp",
        email_from="no-reply@example.com",
        smtp_host="smtp.example.test",
        smtp_port=587,
        smtp_username="smtp-user",
        smtp_password="smtp-secret",
        smtp_use_tls=True,
        ai_enabled_providers=[],
    )
    assert settings.email_provider == "smtp"
    assert settings.email_from == "no-reply@example.com"
    assert settings.smtp_host == "smtp.example.test"
    assert settings.smtp_port == 587
    assert settings.smtp_username == "smtp-user"
    assert settings.smtp_password == "smtp-secret"
    assert settings.smtp_use_tls is True


# --- AI provider settings (Scope §6.3, blueprint §27, ADR-0017/0018) ---


def _prod_ai(**overrides: Any) -> Settings:
    """A fully valid production Settings with AI providers configurable.
    Callers must pass ``ai_enabled_providers`` explicitly."""
    return Settings(
        app_env="production",
        database_url="postgresql+asyncpg://x",
        workos_api_key="sk_test",
        workos_client_id="client_1",
        cors_allowed_origins=["https://app.example.test"],
        trusted_hosts=["api.example.test"],
        redis_url="rediss://redis.example.test:6380/0",
        storage_provider="s3",
        storage_access_key_id="ak_test",
        storage_secret_access_key="sk_storage_test",
        storage_bucket="files",
        storage_endpoint_url="https://s3.example.test",
        email_provider="smtp",
        email_from="no-reply@example.com",
        smtp_host="smtp.example.test",
        smtp_port=587,
        **overrides,
    )


def test_ai_settings_defaults() -> None:
    """The fake provider is the default test adapter; bounds are dev-sane."""
    settings = Settings(
        app_env="test",
        database_url="postgresql+asyncpg://x",
        ai_enabled_providers=["fake"],
        ai_http_timeout_seconds=60.0,
    )
    assert settings.ai_enabled_providers == ["fake"]
    assert settings.ai_http_timeout_seconds == 60.0
    assert settings.ai_openai_api_key == ""
    assert settings.ai_openai_base_url == ""
    assert settings.ai_openai_region == ""
    assert settings.ai_anthropic_api_key == ""
    assert settings.ai_anthropic_inference_geography == ""
    assert settings.ai_deepseek_base_url == "https://api.deepseek.com"
    assert settings.ai_azure_openai_api_version == "2024-08-01-preview"
    assert settings.ai_vertex_project == ""
    assert settings.ai_vertex_location == ""
    assert settings.ai_local_base_url == ""
    assert settings.ai_local_api_key == ""


def test_ai_enabled_providers_rejects_unknown_and_duplicate_values() -> None:
    with pytest.raises(ValidationError, match="unknown AI providers"):
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            ai_enabled_providers=["mistral"],
        )
    with pytest.raises(ValidationError, match="duplicates"):
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            ai_enabled_providers=["fake", "fake"],
        )


def test_ai_http_timeout_is_bounded() -> None:
    with pytest.raises(ValidationError, match="ai_http_timeout_seconds"):
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            ai_http_timeout_seconds=0,
        )
    with pytest.raises(ValidationError, match="ai_http_timeout_seconds"):
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            ai_http_timeout_seconds=601,
        )


def test_ai_openai_region_is_validated() -> None:
    """OpenAI regions are explicit, validated deployment configuration; a typo
    can never silently route data to an unintended processing location (v0.7
    Scope §6.3 regional amendment, ADR-0017)."""
    for invalid in ("mars", "US-WEST", "eu-west-1", " "):
        with pytest.raises(ValidationError, match="ai_openai_region"):
            Settings(
                app_env="test",
                database_url="postgresql+asyncpg://x",
                ai_openai_region=invalid,
            )
    settings = Settings(
        app_env="test",
        database_url="postgresql+asyncpg://x",
        ai_openai_region="eu",
    )
    assert settings.ai_openai_region == "eu"
    assert (
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
        ).ai_openai_region
        == ""
    )


def test_ai_openai_region_conflicts_with_base_url() -> None:
    """A regional label with a non-regional endpoint is a configuration
    conflict: requests must be routed through the matching regional domain,
    never labelled regional while going to the global endpoint (v0.7 Scope
    §6.3 regional amendment)."""
    for region, wrong_host in (("eu", "api.openai.com"), ("us", "eu.api.openai.com")):
        with pytest.raises(ValidationError, match="conflicts with ai_openai_region"):
            Settings(
                app_env="test",
                database_url="postgresql+asyncpg://x",
                ai_openai_region=region,
                ai_openai_base_url=f"https://{wrong_host}/v1",
            )
    settings = Settings(
        app_env="test",
        database_url="postgresql+asyncpg://x",
        ai_openai_region="eu",
        ai_openai_base_url="https://eu.api.openai.com/v1",
    )
    assert settings.ai_openai_base_url == "https://eu.api.openai.com/v1"


def test_ai_anthropic_inference_geography_is_validated() -> None:
    """Anthropic inference geographies are explicit, validated configuration;
    only ``global`` and ``us`` exist and an unsupported value fails fast so
    residency is never misconfigured (v0.7 Scope §6.3 regional amendment,
    ADR-0017)."""
    for invalid in ("apac", "europe", "eu", "US-EAST", " "):
        with pytest.raises(ValidationError, match="ai_anthropic_inference_geography"):
            Settings(
                app_env="test",
                database_url="postgresql+asyncpg://x",
                ai_anthropic_inference_geography=invalid,
            )
    assert (
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            ai_anthropic_inference_geography="us",
        ).ai_anthropic_inference_geography
        == "us"
    )
    assert (
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            ai_anthropic_inference_geography="global",
        ).ai_anthropic_inference_geography
        == "global"
    )
    assert (
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
        ).ai_anthropic_inference_geography
        == ""
    )


def test_enabled_ai_provider_requires_configuration() -> None:
    """An enabled provider must be fully configured in every environment
    (Scope §6.3/§6.7 fail-fast, never at request time)."""
    with pytest.raises(ValidationError, match="AI_OPENAI_API_KEY"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            ai_enabled_providers=["openai"],
        )
    with pytest.raises(ValidationError, match="AI_ANTHROPIC_API_KEY"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            ai_enabled_providers=["anthropic"],
        )
    with pytest.raises(ValidationError, match="AI_DEEPSEEK_API_KEY"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            ai_enabled_providers=["deepseek"],
        )
    with pytest.raises(ValidationError, match="AI_AZURE_OPENAI_ENDPOINT"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            ai_enabled_providers=["azure_openai"],
            ai_azure_openai_api_key="az-test",
        )
    with pytest.raises(ValidationError, match="AI_VERTEX_PROJECT"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            ai_enabled_providers=["vertex"],
            ai_vertex_location="europe-west1",
        )
    with pytest.raises(ValidationError, match="AI_LOCAL_BASE_URL"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            ai_enabled_providers=["local"],
        )
    # A fully configured provider is accepted outside production too.
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        ai_enabled_providers=["openai", "vertex"],
        ai_openai_api_key="sk-test",
        ai_vertex_project="demo-project",
        ai_vertex_location="europe-west1",
    )
    assert settings.ai_enabled_providers == ["openai", "vertex"]


def test_production_rejects_fake_ai_provider() -> None:
    with pytest.raises(ValidationError, match="must not include 'fake'"):
        _prod_ai(ai_enabled_providers=["fake"])


def test_production_accepts_enabled_real_provider() -> None:
    settings = _prod_ai(
        ai_enabled_providers=["openai"],
        ai_openai_api_key="sk-test",
    )
    assert settings.ai_enabled_providers == ["openai"]


def test_ai_azure_endpoint_must_be_https() -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            ai_azure_openai_endpoint="http://my-resource.openai.azure.com",
        )
    settings = Settings(
        app_env="test",
        database_url="postgresql+asyncpg://x",
        ai_azure_openai_endpoint="https://my-resource.openai.azure.com/",
    )
    assert settings.ai_azure_openai_endpoint == "https://my-resource.openai.azure.com"


def test_ai_azure_api_version_must_match_the_pinned_format() -> None:
    with pytest.raises(ValidationError, match="api_version"):
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            ai_azure_openai_api_version="latest",
        )


def test_ai_local_endpoint_safety_in_settings() -> None:
    """Plain HTTP to a public host is rejected in every environment."""
    with pytest.raises(ValidationError, match="ai_local_base_url"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            ai_enabled_providers=["local"],
            ai_local_base_url="http://ollama.example.com",
        )
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        ai_enabled_providers=["local"],
        ai_local_base_url="http://127.0.0.1:11434/v1",
    )
    assert settings.ai_local_base_url == "http://127.0.0.1:11434/v1"


def test_ai_provider_base_url_overrides_require_private_http_or_https() -> None:
    with pytest.raises(ValidationError, match="ai_openai_base_url"):
        Settings(
            app_env="development",
            database_url="postgresql+asyncpg://x",
            ai_openai_base_url="http://openai-proxy.example.com",
        )
    settings = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://x",
        ai_openai_base_url="https://openai-proxy.example.com/v1",
    )
    assert settings.ai_openai_base_url == "https://openai-proxy.example.com/v1"
