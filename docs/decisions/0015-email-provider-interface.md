# ADR 0015: Provider-Neutral Email Interface with SMTP as the First Adapter

Status: Accepted

## Context

The v0.6 release (operations) had to make the template able to send email —
test notifications for users, and the delivery half of the notifications
feature (Scope §6.6) — without locking derived applications into one email
vendor. The blueprint §20 names a provider-neutral email interface with an
adapter list (Postmark, Resend, SendGrid, SES, Microsoft Graph), but leaves
the first implementation and the local-development story open, exactly as
ADR-0006 had left storage open.

Three questions had to be answered:

1. What is the seam between the application and the mail service?
2. Which adapter ships first, with what dependency cost?
3. How does local development exercise real email without a vendor account?

## Options considered

- **HTTP-API provider first (e.g. Resend)**: a modern SDK, but the release
  rule of three grants no second consumer, and every HTTP-API provider is a
  vendor-specific dependency with a vendor-specific account to create.
- **SMTP adapter over the standard library (adopted)**: `smtplib` is in the
  Python standard library — zero new runtime dependency. Postmark, SES,
  SendGrid and Resend all expose SMTP relays, so one adapter is production-
  viable against any of them, and Mailhog (a zero-config SMTP catcher) gives
  local development a real round trip without an account.
- **Multiple adapters at once**: violates the rule of three — no second
  consumer yet, and each adapter is real maintenance before an application
  needs it.

## Decision

**Ship a provider-neutral `EmailProvider` interface in `app/email/`** —
`send_email(*, from_address, to_address, subject, text_body, html_body=None)
-> EmailDeliveryResult(provider_message_id, status)` — parallel to the
ADR-0006 storage contract. The interface is async; adapters run blocking
provider work in worker threads via `asyncio.to_thread` so the event loop
never blocks. No module outside `app/email/` imports a provider SDK;
application code depends on the interface only.

**Ship exactly two implementations**: `SmtpEmailProvider` (standard library
`smtplib`, selected by `EMAIL_PROVIDER=smtp`) and `FakeEmailProvider`
(in-memory, test-only, `EMAIL_PROVIDER=fake`). `get_email_provider()` is an
`lru_cache` singleton wired from settings, mirroring `get_storage`. The
pytest suite pins `EMAIL_PROVIDER=fake` in `conftest.py`, so the default
suite needs no Mailhog and never touches a real relay.

**Mailhog for local development**: a `mailhog` service joins the infra set of
`compose.local.yml` (so `make dev` starts it) with the SMTP port 1025 and the
web UI on 8025; `.env.example` documents the Mailhog-friendly SMTP defaults.
Mailhog-backed SMTP adapter tests carry the `email_integration` marker and
are excluded from the default suite (the same contract as the
`storage_integration` MinIO tests), run explicitly against the local stack or
a CI Mailhog service.

**Production fail-fast**: `EMAIL_PROVIDER=fake` is rejected when
`APP_ENV=production`, and `EMAIL_PROVIDER=smtp` in production requires
explicit `SMTP_HOST`, `SMTP_PORT` and `EMAIL_FROM`; SMTP credentials are
server-side secrets.

**Email is only ever sent from Dramatiq tasks, never from an HTTP handler**
(blueprint §20, ADR-0004) — proven by test.

## Consequences

- One adapter covers every transactional provider's SMTP relay in production
  and Mailhog locally; applications that need an HTTP-API adapter (Resend,
  SES, SendGrid, Postmark, Microsoft Graph) implement it behind the same
  interface, per the rule of three, with no application-code change.
- The template gains no new email runtime dependency; `smtplib` is standard
  library. The two v0.6 observability dependencies are documented separately:
  `sentry-sdk` (error tracking, blueprint §28, Scope §6.1) and
  `prometheus-client` (basic metrics, blueprint §28, Scope §6.1), both pinned
  by `uv.lock`.
- The event loop is never blocked by SMTP I/O (worker-thread pattern).
- `make check` stays provider-free; Mailhog is optional for developers.
- A derived application can swap vendors by changing settings and, where
  needed, adding an adapter — the application code never notices.

---
