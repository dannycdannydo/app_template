"""Mailhog-backed SMTP adapter integration tests (Scope §6.2, blueprint §20).

These prove the acceptance-criteria items a mock cannot: a real SMTP round
trip through Mailhog — the message lands in its mailbox with the provider
message id preserved — and the failure path against an unreachable relay.
They carry the ``email_integration`` marker and are excluded from the default
suite by the pytest addopts in ``pyproject.toml``; run them against the
Mailhog started by ``make dev`` (or a CI service) with:

    uv run pytest -m email_integration

Every test skips when Mailhog is unreachable, so a developer without it
running sees skips, not failures (the same contract as
``test_storage_integration.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import httpx
import pytest

from app.email import SmtpEmailProvider
from app.email.base import EmailSendError
from app.email.types import EMAIL_DELIVERY_STATUS_SENT, EmailDeliveryResult

pytestmark = pytest.mark.email_integration

_SMTP_HOST = os.environ.get("SMTP_HOST", "localhost")
_SMTP_PORT = int(os.environ.get("SMTP_PORT", "1025"))
_UI_PORT = int(os.environ.get("MAILHOG_UI_PORT", "8025"))
_UI_BASE = f"http://localhost:{_UI_PORT}"


def _provider() -> SmtpEmailProvider:
    return SmtpEmailProvider(host=_SMTP_HOST, port=_SMTP_PORT)


async def _smtp_reachable() -> bool:
    try:
        _, writer = await asyncio.open_connection(_SMTP_HOST, _SMTP_PORT)
        writer.close()
        await writer.wait_closed()
        return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def provider() -> SmtpEmailProvider:
    """Probe SMTP connectivity once; skip the whole module when Mailhog is down."""
    if not asyncio.run(_smtp_reachable()):
        pytest.skip(f"SMTP relay not reachable at {_SMTP_HOST}:{_SMTP_PORT}")
    return _provider()


async def _mailhog_messages(*, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Poll the Mailhog v2 API for every captured message."""
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            try:
                response = await client.get(f"{_UI_BASE}/api/v2/messages")
                if response.status_code == 200:
                    return list(response.json().get("items", []))
            except httpx.HTTPError:
                pass
            if asyncio.get_running_loop().time() >= deadline:
                return []
            await asyncio.sleep(0.2)


async def test_smtp_round_trip_delivers_to_mailhog(provider: SmtpEmailProvider) -> None:
    """A real send lands in Mailhog with the provider message id preserved."""
    to_address = f"recipient-{uuid.uuid4().hex[:8]}@example.com"
    subject = f"integration test {uuid.uuid4().hex[:8]}"
    text_body = "hello from the smtp integration test"
    html_body = "<p>hello from the smtp integration test</p>"

    result = await provider.send_email(
        from_address="sender@example.com",
        to_address=to_address,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
    assert isinstance(result, EmailDeliveryResult)
    assert result.status == EMAIL_DELIVERY_STATUS_SENT
    assert result.provider_message_id.startswith("<")  # a Message-ID header

    messages = await _mailhog_messages()
    matching = [
        message
        for message in messages
        if any(to_address in str(to) for to in message["Content"]["Headers"].get("To", []))
    ]
    assert matching, "the sent message never reached Mailhog"
    captured = matching[-1]
    headers = captured["Content"]["Headers"]
    assert headers.get("Subject") == [subject]
    assert result.provider_message_id in json.dumps(captured)
    assert text_body in captured["Content"]["Body"]


async def test_smtp_unreachable_relay_raises_email_send_error() -> None:
    """Failure path: an unreachable relay surfaces as EmailSendError."""
    unreachable = SmtpEmailProvider(host="127.0.0.1", port=1, timeout=2.0)
    with pytest.raises(EmailSendError, match="SMTP delivery failed"):
        await unreachable.send_email(
            from_address="sender@example.com",
            to_address="recipient@example.com",
            subject="Subject",
            text_body="Body",
        )
