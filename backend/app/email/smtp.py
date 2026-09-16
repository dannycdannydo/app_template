"""SMTP email adapter over the standard library (blueprint §20, Scope §6.2).

``SmtpEmailProvider`` is the first real implementation of the
:class:`EmailProvider` interface: it sends through any SMTP relay using only
the standard library's ``smtplib`` and ``email`` — no new runtime dependency
(blueprint §32). It works against Mailhog locally (SMTP 1025, no auth) and
against any transactional provider's SMTP relay in production (Postmark, SES,
SendGrid and Resend all expose SMTP).

Every blocking smtplib call runs in a worker thread via ``asyncio.to_thread``
so the adapter satisfies the async interface without tying up the event loop
(the same pattern as the S3 storage adapter). The provider message id is the
message's ``Message-ID`` header, derived from the caller-persisted delivery
identity and reused on safe retries. Relays preserve it and Mailhog's API
surfaces it, so a delivery can be traced end to end without treating SMTP
correlation as an exactly-once guarantee.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import NoReturn

from app.email.base import (
    AcceptanceUnknownEmailSendError,
    DefinitelyUnsentEmailSendError,
    EmailProvider,
    PermanentlyRejectedEmailSendError,
)
from app.email.types import EMAIL_DELIVERY_STATUS_SENT, EmailDeliveryResult


def _raise_transport_error(*, submission_started: bool, cause: BaseException) -> NoReturn:
    """Classify a transport failure using the durable SMTP submission boundary."""
    error_type = (
        AcceptanceUnknownEmailSendError if submission_started else DefinitelyUnsentEmailSendError
    )
    message = (
        "SMTP acceptance could not be determined."
        if submission_started
        else "SMTP transport is temporarily unavailable."
    )
    raise error_type(message) from cause


class SmtpEmailProvider(EmailProvider):
    """SMTP :class:`EmailProvider` implementation over ``smtplib``."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        use_tls: bool = False,
        timeout: float = 10.0,
    ) -> None:
        if not host:
            raise ValueError("SmtpEmailProvider requires a host")
        if not 1 <= port <= 65535:
            raise ValueError("SmtpEmailProvider port must be between 1 and 65535")
        if timeout <= 0:
            raise ValueError("SmtpEmailProvider timeout must be positive")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._timeout = timeout

    def _send_sync(
        self,
        *,
        delivery_identity: str,
        from_address: str,
        to_address: str,
        subject: str,
        text_body: str,
        html_body: str | None,
    ) -> EmailDeliveryResult:
        message = EmailMessage()
        message["From"] = from_address
        message["To"] = to_address
        message["Subject"] = subject
        message["Message-ID"] = f"<{delivery_identity}@{self._host}>"
        message.set_content(text_body)
        if html_body:
            message.add_alternative(html_body, subtype="html")
        submission_started = False
        accepted_result: EmailDeliveryResult | None = None
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as client:
                if self._use_tls:
                    client.starttls(context=ssl.create_default_context())
                if self._username:
                    client.login(self._username, self._password)
                submission_started = True
                client.send_message(message)
                accepted_result = EmailDeliveryResult(
                    provider_message_id=message["Message-ID"],
                    status=EMAIL_DELIVERY_STATUS_SENT,
                )
        except smtplib.SMTPAuthenticationError as exc:
            if accepted_result is not None:
                return accepted_result
            raise PermanentlyRejectedEmailSendError("SMTP authentication was rejected.") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            if accepted_result is not None:
                return accepted_result
            codes = [code for code, _message in exc.recipients.values()]
            error_type = (
                PermanentlyRejectedEmailSendError
                if codes and all(500 <= code < 600 for code in codes)
                else DefinitelyUnsentEmailSendError
            )
            raise error_type("SMTP recipients were rejected.") from exc
        except smtplib.SMTPNotSupportedError as exc:
            if accepted_result is not None:
                return accepted_result
            raise PermanentlyRejectedEmailSendError(
                "SMTP does not support a required message feature."
            ) from exc
        except smtplib.SMTPResponseException as exc:
            if accepted_result is not None:
                return accepted_result
            error_type = (
                DefinitelyUnsentEmailSendError
                if 400 <= exc.smtp_code < 500
                else PermanentlyRejectedEmailSendError
            )
            raise error_type("SMTP rejected the message.") from exc
        except smtplib.SMTPServerDisconnected as exc:
            if accepted_result is not None:
                return accepted_result
            _raise_transport_error(submission_started=submission_started, cause=exc)
        except smtplib.SMTPException as exc:
            if accepted_result is not None:
                return accepted_result
            _raise_transport_error(submission_started=submission_started, cause=exc)
        except (OSError, TimeoutError) as exc:
            if accepted_result is not None:
                return accepted_result
            _raise_transport_error(submission_started=submission_started, cause=exc)
        assert accepted_result is not None
        return accepted_result

    async def send_email(
        self,
        *,
        delivery_identity: str,
        from_address: str,
        to_address: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
    ) -> EmailDeliveryResult:
        return await asyncio.to_thread(
            self._send_sync,
            delivery_identity=delivery_identity,
            from_address=from_address,
            to_address=to_address,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
