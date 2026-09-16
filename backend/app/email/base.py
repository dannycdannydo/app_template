"""Provider-neutral email interface (blueprint §20, ADR-0015, Scope §6.2).

``EmailProvider`` is the only seam between the application and the mail
service. Adapters implement it (SMTP, and later Resend/SES/SendGrid/Graph per
the rule of three); the in-memory fake lives in ``app.email.fake``; the
process-wide instance is selected from settings by
``app.email.factory.get_email_provider``. No module outside ``app/email/``
may import a provider SDK — application code depends on this interface only
(the same contract as ADR-0006's storage interface).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.email.types import EmailDeliveryResult


class EmailSendError(RuntimeError):
    """Raised by an adapter when the provider cannot accept or deliver mail.

    The caller (always a Dramatiq task, BP §20) translates the failure into
    the durable delivery-row state (Scope §6.4) and lets the job retry policy
    decide whether to try again; nothing sends synchronously.
    """


class DefinitelyUnsentEmailSendError(EmailSendError):
    """The provider definitely did not accept the message; retrying is safe."""


class PermanentlyRejectedEmailSendError(EmailSendError):
    """The provider explicitly rejected the message; retrying cannot help."""


class AcceptanceUnknownEmailSendError(EmailSendError):
    """Submission began but provider acceptance could not be determined.

    Retrying this category automatically could duplicate an externally visible
    email. Callers must persist an attention-required outcome instead.
    """


# Compatibility names retain the old import surface while making its certainty
# precise: transient is definitely unsent; permanent is explicitly rejected.
TransientEmailSendError = DefinitelyUnsentEmailSendError
PermanentEmailSendError = PermanentlyRejectedEmailSendError


class EmailProvider(ABC):
    """Minimal contract every email provider adapter implements.

    Adapters wrap blocking SDK calls (``smtplib`` for the SMTP adapter); the
    interface is async and adapters run the blocking work in a worker thread
    via ``asyncio.to_thread``, so the interface never blocks the event loop
    (the same pattern as the object-storage adapters, ADR-0006).
    """

    @abstractmethod
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
        """Send one email and return the provider's delivery result.

        ``delivery_identity`` is a stable caller-persisted opaque identifier
        reused on every safe retry. ``html_body`` is optional; when given, the
        message is a multipart text/html message. Raises
        :class:`EmailSendError` on any provider failure (network,
        authentication, rejected recipient).
        """
