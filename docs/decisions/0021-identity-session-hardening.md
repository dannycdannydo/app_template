# ADR 0021: Identity and Session Hardening

Status: Proposed (awaiting the plan P1 human-review gate)

## Context

The identity and tenancy core validated WorkOS session tokens (signature,
issuer, audience/client, expiry and required claims) but left four gaps
(plan P1; predecessor `plans/IDENTITY_AND_TENANT_SECURITY_HARDENING_PLAN.md`
§4.1–§4.4):

- the validator retained arbitrary claims and did not interpret WorkOS's `act`
  impersonator claim, so an impersonated session would have been attributed to
  the target user;
- there was no maximum accepted access-token lifetime, so a long-lived token
  remained valid to its expiry;
- the accepted session-revocation window was not pinned as a decision;
- login-time invitation linking treated the local `sent` row as authoritative
  and the webhook consumer did not persist processed event ids, so a WorkOS
  revocation whose webhook was missed or duplicated could still grant.

This ADR records the decisions the repository owner approved in-session before
implementation. Authentication and tenant-isolation changes require human
review under `AGENTS.md`; the plan P1 gate records the approval.

## Options considered

1. **Retain arbitrary claims in request processing** (rejected). A bounded
   context exposes only the identity fields the application acts on and stops a
   later code path trusting a claim that was never reviewed.
2. **Support a restricted, audited impersonation mode** (rejected for the
   starter). The plan's default is to reject impersonation until a separate,
   visible and fully audited support workflow is designed and approved.
3. **Online `sid` denylist in Redis** (rejected). It adds an authentication
   dependency and a fail-open/fail-closed availability decision; with a capped
   access-token lifetime the offline JWT approach bounds the window instead.
4. **Require recent authentication for high-risk platform mutations**
   (deferred). The default WorkOS access token carries no `auth_time` and the
   starter has no re-authentication flow, so enforcing it would break platform
   administration. The context still represents `auth_time` for a future design.
5. **Trust the local invitation row** (rejected). WorkOS is authoritative for
   the invitation lifecycle; a local row may be stale when a webhook is lost.

## Decision

1. **Impersonation is always rejected.** The validator captures the `act`
   claim's `sub` as the session's `impersonator`; `get_current_user` rejects an
   impersonated session with the same generic `401 invalid_session` as any
   other invalid token and logs a safe `impersonated_session_rejected` event
   (identifiers only, never the token). No impersonation mode exists.
2. **Maximum access-token lifetime.** `WORKOS_JWT_MAX_LIFETIME_SECONDS`
   (default `3600`, must be positive) caps `exp - iat` in addition to normal
   expiry validation. A token whose total lifetime exceeds the cap, or whose
   `exp` is not after `iat`, is rejected as `excessive_lifetime`.
3. **No online revocation denylist.** The accepted session-revocation window is
   a revoked token's remaining lifetime, bounded by the maximum lifetime above.
   No Redis denylist or session-revoked consumer is introduced; this is the
   reviewed tradeoff for keeping offline JWT validation and no availability
   dependency on the auth path.
4. **No recent-authentication operation yet.** The approved high-risk set is
   empty because the default WorkOS access token has no `auth_time` and no
   re-authentication flow exists. The validated context exposes
   `authentication_time` where a token carries `auth_time`; a future re-auth
   design reopens this decision rather than adding friction now.
5. **WorkOS is authoritative for invitation acceptance.** Before granting,
   login-time linking revalidates each candidate against the live WorkOS
   invitation: the invitee's verified email, a `pending` provider state, the
   provider organisation matching the mapping captured at invite time, and a
   future expiry. The local `sent` row alone never grants; a missing provider
   id, a provider outage or any mismatch is fail-closed (no membership, retried
   on the next login) and the login itself still succeeds. The unlocked
   candidate read performs the provider call before the row lock is taken, so
   no external I/O happens while a database lock is held.
6. **Webhook delivery is deduplicated at the database.** Every verified WorkOS
   event id is inserted into `webhook_events` under a uniqueness constraint
   before any handler runs; a duplicate delivery fails the insert and is a
   deterministic no-op. The dedup row commits in the same transaction as the
   handler's single commit, so a handler failure leaves the delivery retryable.
   Only the event id, type and receipt time are stored — never a payload,
   token or signature.

## Consequences

- `invitations` gains a nullable `workos_organisation_id`, captured at send
  time; pre-existing rows without it fail closed at acceptance until
  re-invited. `webhook_events` is a new internal ledger. One additive,
  non-destructive migration (`b1c2d3e4f5a6`) carries both and `alembic check`
  stays clean.
- `.env.example` / `.env.production.example` document
  `WORKOS_JWT_MAX_LIFETIME_SECONDS`.
- The `/me` response and generated frontend types are unchanged; no route or
  response schema changes, so `PROTECTED_ROUTES` is unaffected.
- The full identity/invitation test set and the mandatory security suite cover
  normal, expired, excessive-lifetime, impersonated, stale-authentication and
  malformed-timestamp sessions, and invitation missed-revocation,
  duplicate-delivery, expiry, provider-outage, mismatched-email and
  cross-organisation-provider-id cases.

This decision implements plan P1 and follows blueprint §8 (authentication),
§9 (organisations and permissions), §29 (audit), §30 (security baseline) and
§31 (testing).
