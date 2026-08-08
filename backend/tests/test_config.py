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
        storage_provider="s3",
        storage_access_key_id="ak_test",
        storage_secret_access_key="sk_storage_test",
        storage_bucket="files",
        storage_endpoint_url="https://s3.example.test",
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


def test_bootstrap_email_defaults_to_disabled() -> None:
    settings = Settings(app_env="development", database_url="postgresql+asyncpg://x")
    assert settings.bootstrap_platform_admin_email == ""


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
        bootstrap_platform_admin_email="admin@example.com",
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
        workos_webhook_secret="whsec_prod",
    )
    assert settings.workos_webhook_secret == "whsec_prod"


# --- Object storage settings (Scope §6.1, blueprint §17) ---


def test_storage_defaults_are_s3_with_dev_sensible_limits() -> None:
    """Scope §6.1: the provider defaults to s3; limits are dev-friendly."""
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


def test_production_requires_explicit_s3_configuration() -> None:
    """Scope §4: production with storage_provider=s3 must set credentials/bucket/endpoint."""
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
    )
    assert settings.storage_provider == "s3"
    assert settings.storage_public_endpoint_url == "https://s3.example.test"
