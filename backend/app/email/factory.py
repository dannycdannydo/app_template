"""Email provider factory wired from settings (blueprint §20, Scope §6.2).

``get_email_provider`` is the process-wide singleton for the email adapter,
mirroring ``get_storage``: it reads the selected provider from settings once
and returns the same instance for the lifetime of the process. The pytest
suite pins ``EMAIL_PROVIDER=fake`` in ``tests/conftest.py``, so the default
suite never constructs a real provider (Scope §6.2).
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.email.base import EmailProvider
from app.email.fake import FakeEmailProvider
from app.email.smtp import SmtpEmailProvider


@lru_cache
def get_email_provider() -> EmailProvider:
    """Return the process-wide :class:`EmailProvider` selected by settings."""
    settings = get_settings()
    if settings.email_provider == "fake":
        return FakeEmailProvider()
    if settings.email_provider == "smtp":
        return SmtpEmailProvider(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
        )
    raise ValueError(f"unknown email_provider: {settings.email_provider!r}")
