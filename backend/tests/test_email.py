"""Interface contract tests for the email package (Scope §6.2, blueprint §20).

The suite runs against :class:`FakeEmailProvider` — the default adapter under
``EMAIL_PROVIDER=fake`` (pinned in ``tests/conftest.py``) — and proves the
whole provider contract: round-trip delivery, deterministic provider message
ids, the failure path, and the settings-driven factory wiring. A structural
guard proves the BP §20 rule that application email is only ever sent from
worker tasks, never inside an HTTP handler. No provider SDK is imported
anywhere, which is the point of ADR-0015.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from app.core.config import Settings
from app.email import (
    EmailDeliveryResult,
    EmailProvider,
    FakeEmailProvider,
    get_email_provider,
)
from app.email.base import EmailSendError
from app.email.smtp import SmtpEmailProvider
from app.email.types import EMAIL_DELIVERY_STATUS_SENT

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def provider() -> FakeEmailProvider:
    return FakeEmailProvider()


async def test_fake_is_an_email_provider_implementation(provider: FakeEmailProvider) -> None:
    assert isinstance(provider, EmailProvider)


async def test_send_email_round_trip_records_the_message(provider: FakeEmailProvider) -> None:
    """The full provider contract: send -> result + recorded message."""
    result = await provider.send_email(
        from_address="sender@example.com",
        to_address="recipient@example.com",
        subject="Hello from the template",
        text_body="Plain text body",
        html_body="<p>HTML body</p>",
    )
    assert isinstance(result, EmailDeliveryResult)
    assert result.provider_message_id == "fake-1"
    assert result.status == EMAIL_DELIVERY_STATUS_SENT

    assert len(provider.messages) == 1
    message = provider.messages[0]
    assert message.from_address == "sender@example.com"
    assert message.to_address == "recipient@example.com"
    assert message.subject == "Hello from the template"
    assert message.text_body == "Plain text body"
    assert message.html_body == "<p>HTML body</p>"


async def test_send_email_without_html_records_text_only(provider: FakeEmailProvider) -> None:
    result = await provider.send_email(
        from_address="sender@example.com",
        to_address="recipient@example.com",
        subject="Text only",
        text_body="Plain text body",
    )
    assert result.provider_message_id == "fake-1"
    assert provider.messages[0].html_body is None


async def test_provider_message_ids_are_deterministic(provider: FakeEmailProvider) -> None:
    for index in range(1, 4):
        result = await provider.send_email(
            from_address="sender@example.com",
            to_address=f"recipient-{index}@example.com",
            subject="Subject",
            text_body="Body",
        )
        assert result.provider_message_id == f"fake-{index}"


async def test_failure_path_raises_and_records_nothing(provider: FakeEmailProvider) -> None:
    """Armed failure: EmailSendError, nothing recorded, next send works."""
    provider.fail_next_send()
    with pytest.raises(EmailSendError, match="simulated provider failure"):
        await provider.send_email(
            from_address="sender@example.com",
            to_address="recipient@example.com",
            subject="Subject",
            text_body="Body",
        )
    assert provider.messages == []

    result = await provider.send_email(
        from_address="sender@example.com",
        to_address="recipient@example.com",
        subject="Subject",
        text_body="Body",
    )
    assert result.provider_message_id == "fake-1"


def test_fail_next_send_rejects_zero_or_negative_counts(provider: FakeEmailProvider) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        provider.fail_next_send(0)
    with pytest.raises(ValueError, match="at least 1"):
        provider.fail_next_send(-1)


async def test_smtp_constructor_validates_configuration() -> None:
    with pytest.raises(ValueError, match="host"):
        SmtpEmailProvider(host="", port=1025)
    with pytest.raises(ValueError, match="port"):
        SmtpEmailProvider(host="localhost", port=0)
    with pytest.raises(ValueError, match="port"):
        SmtpEmailProvider(host="localhost", port=65536)
    with pytest.raises(ValueError, match="timeout"):
        SmtpEmailProvider(host="localhost", port=1025, timeout=0)


# --- Factory (get_email_provider, wired from settings) ---


def _settings_with_email_provider(provider_name: str) -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+asyncpg://x",
        email_provider=provider_name,
    )


def test_get_email_provider_returns_a_cached_fake_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_email_provider.cache_clear()
    monkeypatch.setattr(
        "app.email.factory.get_settings", lambda: _settings_with_email_provider("fake")
    )
    provider = get_email_provider()
    assert isinstance(provider, FakeEmailProvider)
    assert get_email_provider() is provider  # lru_cache singleton, like get_storage
    get_email_provider.cache_clear()


def test_get_email_provider_returns_a_cached_smtp_provider_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_email_provider.cache_clear()
    monkeypatch.setattr(
        "app.email.factory.get_settings",
        lambda: Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            email_provider="smtp",
            email_from="no-reply@example.com",
            smtp_host="smtp.example.test",
            smtp_port=587,
            smtp_username="user",
            smtp_password="secret",
            smtp_use_tls=True,
        ),
    )
    provider = get_email_provider()
    assert isinstance(provider, SmtpEmailProvider)
    assert get_email_provider() is provider
    get_email_provider.cache_clear()


def test_get_email_provider_rejects_unknown_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory stays defensive even though Settings rejects unknown providers."""
    get_email_provider.cache_clear()
    monkeypatch.setattr(
        "app.email.factory.get_settings",
        lambda: types.SimpleNamespace(email_provider="resend"),
    )
    with pytest.raises(ValueError, match="unknown email_provider"):
        get_email_provider()
    get_email_provider.cache_clear()


# --- BP §20: email is only ever sent from worker tasks ---


def test_email_is_only_used_from_worker_tasks() -> None:
    """BP §20: no non-task module may use the email provider.

    Application email is always sent through the Dramatiq worker, never in an
    HTTP handler or a service called by one. The provider package itself
    (interface, adapters, factory) is the only other place allowed to import
    it; any future sender must live in a ``*_tasks.py`` module. This guard
    keeps the rule structural — a sender added to an HTTP-path module fails
    the suite.
    """
    offenders: list[str] = []
    app_dir = BACKEND_ROOT / "app"
    for source in sorted(app_dir.rglob("*.py")):
        if source.is_relative_to(app_dir / "email"):
            continue
        if "app.email" in source.read_text(encoding="utf-8") and not source.name.endswith(
            "tasks.py"
        ):
            offenders.append(str(source.relative_to(BACKEND_ROOT)))
    assert not offenders, f"email provider used outside worker tasks: {offenders}"
