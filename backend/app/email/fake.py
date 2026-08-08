"""In-memory EmailProvider implementation for tests (Scope §6.2, blueprint §20).

The fake never touches a provider: it records every sent message in
``messages`` and mints deterministic provider message ids (``fake-<n>``). It
is the adapter the pytest suite pins via ``EMAIL_PROVIDER=fake`` so
``make check`` needs no Mailhog or network. ``fail_next_send`` arms a number
of consecutive sends to raise :class:`EmailSendError`, proving the delivery
failure path (Scope §6.4) without a provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.email.base import EmailProvider, EmailSendError
from app.email.types import EMAIL_DELIVERY_STATUS_SENT, EmailDeliveryResult


@dataclass(frozen=True, slots=True)
class SentEmail:
    """One recorded message — everything the fake "delivered"."""

    from_address: str
    to_address: str
    subject: str
    text_body: str
    html_body: str | None


class FakeEmailProvider(EmailProvider):
    """Deterministic, test-only :class:`EmailProvider` implementation."""

    def __init__(self) -> None:
        self.messages: list[SentEmail] = []
        self._fail_next = 0

    def fail_next_send(self, count: int = 1) -> None:
        """Arm the next ``count`` sends to raise :class:`EmailSendError`."""
        if count < 1:
            raise ValueError("fail_next_send count must be at least 1")
        self._fail_next += count

    async def send_email(
        self,
        *,
        from_address: str,
        to_address: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
    ) -> EmailDeliveryResult:
        if self._fail_next > 0:
            self._fail_next -= 1
            raise EmailSendError("simulated provider failure")
        self.messages.append(
            SentEmail(
                from_address=from_address,
                to_address=to_address,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
        )
        return EmailDeliveryResult(
            provider_message_id=f"fake-{len(self.messages)}",
            status=EMAIL_DELIVERY_STATUS_SENT,
        )
