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
message's ``Message-ID`` header: it is generated before sending, preserved by
relays, and is what Mailhog's API surfaces, so a delivery can be traced end
to end.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from app.email.base import EmailProvider, EmailSendError
from app.email.types import EMAIL_DELIVERY_STATUS_SENT, EmailDeliveryResult


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
        message["Message-ID"] = make_msgid(domain=self._host)
        message.set_content(text_body)
        if html_body:
            message.add_alternative(html_body, subtype="html")
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as client:
                if self._use_tls:
                    client.starttls(context=ssl.create_default_context())
                if self._username:
                    client.login(self._username, self._password)
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailSendError(f"SMTP delivery failed: {exc}") from exc
        return EmailDeliveryResult(
            provider_message_id=message["Message-ID"],
            status=EMAIL_DELIVERY_STATUS_SENT,
        )

    async def send_email(
        self,
        *,
        from_address: str,
        to_address: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
    ) -> EmailDeliveryResult:
        return await asyncio.to_thread(
            self._send_sync,
            from_address=from_address,
            to_address=to_address,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
