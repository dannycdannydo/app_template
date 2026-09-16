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

import smtplib
import types
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.email import (
    EmailDeliveryResult,
    EmailProvider,
    FakeEmailProvider,
    get_email_provider,
)
from app.email.base import (
    AcceptanceUnknownEmailSendError,
    EmailSendError,
    PermanentEmailSendError,
    TransientEmailSendError,
)
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
        delivery_identity="delivery-1",
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
    assert message.delivery_identity == "delivery-1"
    assert message.to_address == "recipient@example.com"
    assert message.subject == "Hello from the template"
    assert message.text_body == "Plain text body"
    assert message.html_body == "<p>HTML body</p>"


async def test_send_email_without_html_records_text_only(provider: FakeEmailProvider) -> None:
    result = await provider.send_email(
        delivery_identity="delivery-1",
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
            delivery_identity=f"delivery-{index}",
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
            delivery_identity="delivery-1",
            from_address="sender@example.com",
            to_address="recipient@example.com",
            subject="Subject",
            text_body="Body",
        )
    assert provider.messages == []

    result = await provider.send_email(
        delivery_identity="delivery-1",
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


@pytest.mark.parametrize(
    ("smtp_error", "expected"),
    [
        (OSError("network unavailable"), TransientEmailSendError),
        (smtplib.SMTPServerDisconnected("connection lost"), TransientEmailSendError),
        (smtplib.SMTPDataError(450, b"try later"), TransientEmailSendError),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), PermanentEmailSendError),
        (smtplib.SMTPDataError(550, b"rejected"), PermanentEmailSendError),
    ],
)
async def test_smtp_errors_are_classified_without_exposing_provider_detail(
    monkeypatch: pytest.MonkeyPatch,
    smtp_error: Exception,
    expected: type[EmailSendError],
) -> None:
    """P4 retry classification keeps raw SMTP responses out of task errors."""

    class _RaisingSmtp:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _RaisingSmtp:
            raise smtp_error

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("app.email.smtp.smtplib.SMTP", _RaisingSmtp)
    provider = SmtpEmailProvider(host="smtp.example.test", port=25)
    with pytest.raises(expected) as raised:
        await provider.send_email(
            delivery_identity="delivery-1",
            from_address="sender@example.com",
            to_address="recipient@example.com",
            subject="Subject",
            text_body="Body",
            html_body=None,
        )
    assert "credentials" not in str(raised.value)
    assert "try later" not in str(raised.value)
    assert "bad credentials" not in str(raised.value)


async def test_smtp_disconnect_after_submission_begins_is_acceptance_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DisconnectDuringSubmission:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _DisconnectDuringSubmission:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def send_message(self, message: object) -> None:
            raise smtplib.SMTPServerDisconnected("lost after DATA")

    monkeypatch.setattr("app.email.smtp.smtplib.SMTP", _DisconnectDuringSubmission)
    provider = SmtpEmailProvider(host="smtp.example.test", port=25)
    with pytest.raises(AcceptanceUnknownEmailSendError):
        await provider.send_email(
            delivery_identity="stable-delivery-id",
            from_address="sender@example.com",
            to_address="recipient@example.com",
            subject="Subject",
            text_body="Body",
        )


async def test_smtp_teardown_failure_after_acceptance_returns_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message_ids: list[str] = []

    class _AcceptedThenTeardownFailed:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _AcceptedThenTeardownFailed:
            return self

        def __exit__(self, *args: object) -> None:
            raise smtplib.SMTPResponseException(421, b"try again later")

        def send_message(self, message: Any) -> None:
            message_ids.append(str(message["Message-ID"]))

    monkeypatch.setattr("app.email.smtp.smtplib.SMTP", _AcceptedThenTeardownFailed)
    provider = SmtpEmailProvider(host="smtp.example.test", port=25)

    result = await provider.send_email(
        delivery_identity="stable-delivery-id",
        from_address="sender@example.com",
        to_address="recipient@example.com",
        subject="Subject",
        text_body="Body",
    )

    assert result.provider_message_id == "<stable-delivery-id@smtp.example.test>"
    assert message_ids == ["<stable-delivery-id@smtp.example.test>"]


async def test_smtp_unsupported_message_feature_is_permanently_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnsupportedMessageFeature:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _UnsupportedMessageFeature:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def send_message(self, message: Any) -> None:
            raise smtplib.SMTPNotSupportedError("SMTPUTF8 is required")

    monkeypatch.setattr("app.email.smtp.smtplib.SMTP", _UnsupportedMessageFeature)
    provider = SmtpEmailProvider(host="smtp.example.test", port=25)

    with pytest.raises(PermanentEmailSendError, match="required message feature"):
        await provider.send_email(
            delivery_identity="stable-delivery-id",
            from_address="sender@example.com",
            to_address="recipient@example.com",
            subject="Subject",
            text_body="Body",
        )


async def test_smtp_reuses_delivery_identity_as_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message_ids: list[str] = []

    class _RecordingSmtp:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _RecordingSmtp:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def send_message(self, message: Any) -> None:
            message_ids.append(str(message["Message-ID"]))

    monkeypatch.setattr("app.email.smtp.smtplib.SMTP", _RecordingSmtp)
    provider = SmtpEmailProvider(host="smtp.example.test", port=25)
    for _ in range(2):
        await provider.send_email(
            delivery_identity="stable-delivery-id",
            from_address="sender@example.com",
            to_address="recipient@example.com",
            subject="Subject",
            text_body="Body",
        )
    assert message_ids == [
        "<stable-delivery-id@smtp.example.test>",
        "<stable-delivery-id@smtp.example.test>",
    ]


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
