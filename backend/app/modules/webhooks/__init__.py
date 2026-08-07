"""WorkOS webhook consumption (Scope §6.8, blueprint §8, §30).

The webhook endpoint is the only route in the API gated by a signature rather
than a session token: ``POST /api/v1/webhooks/workos`` verifies the WorkOS
HMAC-SHA256 ``workos-signature`` header (300s tolerance) before any payload is
parsed. The consumer refreshes best-effort local state only — invitation
revocations mirror locally, deleted WorkOS users are deactivated defensively —
and never grants anything: login-time reconciliation (Scope §6.5) stays the
single authoritative grant path.
"""
