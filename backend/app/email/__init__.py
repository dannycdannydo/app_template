"""Provider-neutral email (blueprint §20, ADR-0015, Scope §6.2).

Application code imports the :class:`EmailProvider` interface from here and
never a provider SDK; the concrete adapter is selected from settings through
:func:`get_email_provider`. Email is always sent from Dramatiq worker tasks,
never inside an HTTP handler (BP §20, ADR-0004), so a new provider means
adding one adapter class in ``app/email/`` — no other module changes.
"""

from app.email.base import EmailProvider, EmailSendError
from app.email.factory import get_email_provider
from app.email.fake import FakeEmailProvider
from app.email.smtp import SmtpEmailProvider
from app.email.types import EMAIL_DELIVERY_STATUS_SENT, EmailDeliveryResult

__all__ = [
    "EMAIL_DELIVERY_STATUS_SENT",
    "EmailDeliveryResult",
    "EmailProvider",
    "EmailSendError",
    "FakeEmailProvider",
    "SmtpEmailProvider",
    "get_email_provider",
]
