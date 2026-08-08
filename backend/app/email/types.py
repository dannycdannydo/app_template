"""Value objects returned by the email interface (blueprint §20, Scope §6.2).

These are the provider-neutral payloads every adapter returns: the terminal
outcome of one send attempt. Application code depends on these types (never
on provider SDK types) so the interface stays the only seam between the app
and the mail service.
"""

from __future__ import annotations

from dataclasses import dataclass

# Terminal status reported by an adapter after a successful send. The durable
# notification delivery row (Scope §6.4) carries its own lifecycle status
# (queued -> running -> succeeded/failed); this value is the provider's
# outcome, recorded as ``provider_message_id`` evidence of delivery.
EMAIL_DELIVERY_STATUS_SENT = "sent"


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    """Provider-level outcome of one send attempt.

    ``provider_message_id`` is the message id the provider knows the mail by
    (the SMTP adapter's Message-ID header; the fake adapter's deterministic
    ``fake-<n>``). ``status`` is the terminal delivery status.
    """

    provider_message_id: str
    status: str
