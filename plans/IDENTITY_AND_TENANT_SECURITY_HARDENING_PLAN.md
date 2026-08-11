# Identity, Session and Tenant-Isolation Security Hardening — Plan

Status: Proposed; bounded simple hardening reviewed and approved, broader work not started

Relates to: `Internal_Custom_Application_Starter_Architecture_v2.md` (BP
§6–§10, §28–§31, §33), `TEMPLATE_V0_5_SCOPE.md` (§7 reference map),
`TEMPLATE_V0_2_SCOPE.md` (identity, organisation and permission foundations),
`SECURITY.md`, ADR-0001 and ADR-0013

Implementation progress (August 2026 isolated review branch):

| Work unit | Status |
| --- | --- |
| §5.6 Logging, request-ID and Sentry hardening | Complete and human-reviewed; focused lint, strict type checks and tests pass |
| §5.7 Worker and persistence tenant invariants | Reviewed partial delivery: notification/file job-type checks and notification organisation consistency are enforced; database recipient constraints remain a design decision |
| §5.8 Central frontend session cleanup | Complete and human-reviewed; focused lint, type checks and 13 tests pass |
| §5.10 Remove unsafe reuse seams | Complete and human-reviewed: organisation context is mandatory in the legacy role helpers; focused lint, strict type checks and service tests pass |

The bounded changes above were approved for incorporation after review. The
real-database/ASGI test harness stalled without output in the isolated sandbox;
those suites remain mandatory in the merge/CI environment. §5.7 remains
partial and is not represented as completing its deferred persistence-design
work.

## 1. Purpose

Harden the boundary between WorkOS authentication and the application's own
identity, organisation, membership and permission model. The desired outcome
is that:

- a WorkOS access token proves only the caller's external identity and session;
- the application remains authoritative for users, organisations,
  memberships, roles, permissions and tenant data;
- an organisation-scoped request can read or mutate only data belonging to the
  validated active membership in that request's organisation context;
- platform administration remains an explicit, separately authorised
  cross-tenant plane;
- impersonation, invitation reconciliation, token revocation and high-risk
  administration have deliberate, auditable policies;
- logs, error reporting, frontend state and background work do not create a
  second path around the tenant boundary; and
- tests prove resource-level isolation, including the difficult case where one
  user belongs to multiple organisations with different roles.

This plan records the findings of the August 2026 read-only security review.
That review found no direct cross-organisation IDOR in the current
tenant-facing API. It did identify assurance gaps and defence-in-depth work
that must be completed before the project should claim strong tenant-isolation
assurance.

## 2. Current security baseline

The following controls are already present and must not be weakened:

- WorkOS JWTs are validated for RS256 signature, expiry, issuer, client binding
  and required identity/session claims.
- WorkOS `org_id`, role and permission claims are not used as application
  authorisation.
- `X-Org-Id` is parsed by the backend and must resolve to the authenticated
  user's active organisation membership.
- Organisation permissions are default-deny and are calculated for that
  membership, not globally for the user.
- Platform permissions use a separate role graph, take no `X-Org-Id`, and do
  not fall back to organisation ownership.
- Tenant-facing records, files, jobs and notifications use organisation-scoped
  query helpers; notifications additionally scope by recipient user.
- Foreign-organisation resources are represented as not found rather than
  disclosing their existence.
- Invitation linking requires a current server-fetched WorkOS profile and a
  verified matching email; membership uniqueness makes concurrent first login
  race-safe.
- Access tokens are held in frontend memory rather than application-managed
  persistent browser storage. The selected organisation ID is non-secret
  client state and is always revalidated by the backend.
- The authentication callback uses PKCE and restricts `returnTo` to a safe
  same-origin local path.
- Platform routes and tenant routes participate in the mandatory protected
  route suite, including cross-plane denial checks.

## 3. Threat model and trust boundaries

### 3.1 Trusted authorities

- WorkOS owns identity proof, authentication factors, session creation and the
  signing of access tokens.
- The application database owns internal users, organisations, memberships,
  roles, permissions, platform roles, invitations and business resources.
- A validated WorkOS subject maps to exactly one internal user through
  `users.workos_user_id`.
- Platform administrators are trusted for intentional cross-tenant
  administration, but their actions must remain least-privileged, attributable
  and protected as high-risk operations.

### 3.2 Untrusted inputs

- Every browser-controlled header, including `Authorization`, `X-Org-Id` and
  `X-Request-ID`.
- Resource IDs, organisation IDs and role codes supplied through paths,
  queries or bodies.
- WorkOS webhook delivery order, duplication and availability.
- Broker messages and durable job references, even when only internal code is
  expected to produce them.
- Exception messages and third-party SDK errors before they reach logs or
  Sentry.
- Frontend visibility and disabled-state decisions, which are cosmetic and
  never an authorisation control.

### 3.3 Required invariant

For an organisation-plane operation, possession of a valid token and knowledge
of another tenant's resource ID must never be sufficient. The operation must
bind all of the following before data is returned or changed:

1. validated WorkOS subject;
2. enabled internal user;
3. active membership for the selected organisation;
4. required permission granted by that membership; and
5. resource ownership by the same organisation, plus caller ownership where
   the resource is user-private.

## 4. Findings to address

### 4.1 WorkOS impersonation is not represented in application context

The JWT validator retains arbitrary claims, but the authentication and audit
paths do not interpret WorkOS's `act` impersonator claim. If WorkOS
impersonation were enabled, actions would be attributed only to the target
user, including actions performed as a platform administrator.

Default policy: impersonated sessions are rejected unless a separate,
human-reviewed impersonation design is approved. Any later opt-in design must
preserve both actor and subject, provide an unmistakable UI indication, audit
the original actor, expire quickly and deny sensitive platform operations.

### 4.2 Revoked sessions remain valid until access-token expiry

The application validates token expiry but does not consume
`session.revoked`, maintain a `sid` denylist, enforce a maximum token lifetime,
or require recent authentication for high-risk platform mutations. This is the
normal limitation of offline JWT validation, but the accepted exposure window
is not currently pinned as an operational security decision.

### 4.3 Frontend roles are aggregated across organisations

`GET /api/v1/me` exposes distinct role codes across all memberships. The
frontend unions their permissions, so a user who is an owner in organisation A
and a viewer in organisation B can see owner-only controls while operating in
B. The backend still denies the request, so this is not a current data-access
bypass, but the projection is misleading and prevents precise organisation UI
authorisation.

### 4.4 Invitation acceptance can miss out-of-band WorkOS revocation

Login-time linking refreshes the WorkOS user profile but treats the local
invitation row as authoritative. If an invitation is revoked directly in
WorkOS and the corresponding webhook is lost or delayed, a local `sent` row
can still grant membership.

Webhook consumers also do not persist processed event IDs or reject stale
updates explicitly. Mostly idempotent handlers reduce the present impact, but
they do not establish delivery correctness.

### 4.5 Tenant isolation is enforced only by application queries

The reviewed resource modules consistently filter by organisation, but the
database does not use PostgreSQL Row-Level Security. A future missed filter can
therefore expose data. RLS could add a second enforcement boundary, but it is a
major design change: platform access, workers, migrations, connection pooling,
transaction-local context and non-bypass database roles must all be designed
together.

### 4.6 Logging and error reporting lack explicit secret redaction

Unexpected exceptions log `str(exc)` and formatted tracebacks, and Sentry has
no application-specific `before_send` scrubber. Client-provided request IDs are
accepted without a length or character policy. The current never-log test
declares secret candidates but does not inject them into the exercised request,
so it does not prove its stated guarantee.

### 4.7 Worker references do not always re-establish tenant consistency

The notification email worker loads its job, delivery and notification by
independent IDs without proving that their organisation context agrees. There
is no reviewed public route that can currently manufacture a mismatched job,
but corrupted durable state or an incorrectly produced broker message should
fail closed rather than operate across tenant references.

The notification schema also allows a notification's `user_id` to name a user
without a database-enforced membership in `organisation_id`; current producers
are responsible for maintaining that relationship.

### 4.8 Logout fallback does not clear all cached tenant data

A successful WorkOS logout performs a top-level navigation and naturally
clears memory. If logout navigation cannot start, the local fallback clears the
session store but does not centrally clear the selected organisation and
TanStack Query cache.

### 4.9 Organisation creation policy is implicit

Any authenticated WorkOS user can call `POST /api/v1/organisations` and become
the new organisation's owner. This does not expose an existing tenant, but it
may be inappropriate for an internal deployment if WorkOS sign-up is enabled.
The production policy must be explicit rather than relying on dashboard
configuration by convention.

### 4.10 Legacy unscoped role-mutation helpers remain reusable

The current platform administration routes use organisation-scoped membership
lookups. Older generic permission service functions accept only a membership
ID and role code. They are not used by production routes, but they are an easy
future footgun and should be removed, made private, or require organisation
context.

## 5. Proposed work units

Each work unit follows the repository's mandatory
**implement → review → apply-and-commit** loop. Authentication, permission,
tenant-isolation, secret-handling and public API work requires recorded human
review before application.

### 5.1 WorkOS security policy and deployment contract

- Document the production WorkOS settings for access-token duration, session
  lifetime, inactivity timeout, MFA and permitted sign-in methods.
- Choose and record the maximum accepted access-token lifetime (`exp - iat`).
- Confirm that WorkOS impersonation is disabled in every environment until an
  explicit application design exists.
- Decide whether organisation creation is open to every authenticated user,
  platform-admin only, invitation/bootstrap only, or controlled by a typed
  deployment setting.
- Add deployment/runbook checks for drift in these settings. Do not place API
  keys, webhook secrets or token values in generated reports.

Deliverables: security configuration documentation, deployment checklist and
recorded decisions for §8.

### 5.2 Explicit authenticated session context

- Replace the loose use of `ValidatedSession.claims` in request processing with
  a bounded authenticated-session context containing at least WorkOS subject,
  session ID, issued-at, expiry, authentication time and optional impersonator.
- Reject `act` by default with a generic `401`/`403` response and a safe
  security event that contains identifiers but no token.
- Enforce the approved maximum access-token lifetime in addition to normal
  expiry validation.
- Avoid repeated WorkOS profile calls in one request by resolving the verified
  profile once and passing it through the authentication/provisioning chain.
- Preserve exact issuer and client binding; do not begin trusting WorkOS
  organisation or role claims.

Tests: normal token, excessive lifetime, malformed timestamps, impersonator
claim, missing claims, wrong issuer/client/audience, expired token and disabled
internal user.

### 5.3 Session revocation and recent-authentication controls

- Subscribe to the WorkOS session-revocation event after confirming the exact
  event contract against current official documentation.
- If a denylist is adopted, store only `sid` plus expiry in Redis or another
  bounded TTL store; fail according to an explicitly reviewed availability
  policy.
- Apply recent-authentication checks to high-risk platform mutations: granting
  or revoking platform administrators, changing organisation roles, suspending
  or removing memberships, and other actions selected during review.
- Return a stable error that the frontend can turn into a reauthentication
  flow without exposing token details.
- Preserve the existing final-platform-administrator safeguard.

Tests: revoked session, denylist expiry, cache unavailability policy, stale
`auth_time`, recent authentication, normal tenant operations and platform
cross-plane denial.

### 5.4 Per-organisation roles and effective permissions

- Change the `/me` projection so each membership carries its role codes, or add
  a selected-organisation effective-permissions endpoint. Prefer returning
  server-derived permission codes when that avoids duplicating role bundles in
  the frontend.
- Update generated frontend API types.
- Derive record, file and other UI capabilities only for the selected active
  membership.
- Keep every backend permission dependency unchanged as the enforcement point.
- Define compatibility and rollout behaviour because changing `/me` is a
  public API contract change.

Tests must include one user who is owner in organisation A and viewer in
organisation B. The UI must hide B's write controls and direct API requests
must still receive `403`.

### 5.5 Authoritative invitation reconciliation

- Before granting a membership for a WorkOS-backed invitation, fetch the
  invitation through the existing adapter and verify its current state,
  identity, organisation mapping and expiry.
- Fail closed on WorkOS unavailability; do not grant from potentially stale
  local state.
- Define whether invitations without a WorkOS identifier are valid legacy
  rows, and provide an explicit migration/retirement path.
- Persist WorkOS webhook event IDs with a uniqueness constraint and processed
  timestamp so duplicate delivery is a no-op.
- Compare provider object update time/version where available so an older
  webhook cannot overwrite newer local identity state.
- Keep webhook signatures, timestamp tolerance and request-size bounds.
- Add reconciliation metrics and an operator procedure for failed provider
  checks.

Tests: direct WorkOS revocation with a missed webhook, duplicate events,
out-of-order events, provider outage, expired invite, unverified/mismatched
email, cross-organisation invitation ID and concurrent first login.

### 5.6 Logging, request-ID and Sentry hardening

- Generate request IDs server-side, or validate inbound values against a
  bounded length and conservative character set while recording an external
  correlation ID separately.
- Stop adding raw `str(exc)` values to production log fields unless the
  exception type has a reviewed safe serializer.
- Add a central structlog redaction processor for authorization headers,
  tokens, passwords, cookies, connection strings, signed URL query values and
  provider credentials.
- Configure Sentry with explicit PII defaults and a `before_send` scrubber for
  request headers, cookies, query strings, exception values and breadcrumbs.
- Replace the ineffective never-log test with tests that actually inject each
  candidate through headers, query values and controlled failing adapters, and
  inspect the serialized log/Sentry event.
- Ensure API responses retain generic errors and never expose stack traces.

### 5.7 Worker and persistence tenant invariants

- Make notification task lookups verify the expected job type and bind job,
  delivery and notification to one organisation before any provider call.
- Apply the same invariant audit to file processing, retry finalizers and every
  future worker consuming multiple durable references.
- Fail a mismatched job permanently with a safe error code and auditable
  security event; do not send email or access object storage.
- Evaluate composite foreign keys or service-level constraints that ensure a
  notification recipient has an appropriate relationship with its
  organisation. Document any intentional exceptions for historic recipients.
- Keep broker messages reference-only and never place tenant data, tokens or
  signed URLs in messages.

Tests: mismatched job/delivery/notification organisations, wrong job type,
foreign file reference, terminal redelivery, provider-not-called assertions
and normal retry behaviour.

### 5.8 Central frontend session cleanup

- Introduce one local cleanup function used by normal logout, rejected-session
  handling and logout failure.
- Clear the Pinia session, selected organisation and TanStack Query cache before
  local fallback navigation.
- Ensure a subsequent user cannot render cached `/me` or tenant resource data.
- Continue using top-level WorkOS logout navigation to clear the provider
  session cookie.

Tests: successful logout navigation, SDK failure fallback, `401` handling,
cached tenant data removal and two users signing in sequentially in one tab.

### 5.9 Tenant-isolation contract suite

- Build a reusable real-database fixture with two organisations, users who
  belong to only one organisation, and a user who belongs to both with
  different roles.
- For every organisation-owned resource, exercise list, get, create, update,
  delete and action endpoints using a valid foreign resource ID.
- Prove that `X-Org-Id` selection does not override resource ownership and that
  a user with membership in both organisations cannot use permissions from one
  membership in the other.
- Test pagination counts, filters, exports/download URLs, batch endpoints and
  indirect references—not only single-row reads.
- Extend the mandatory `PROTECTED_ROUTES` table for every new protected route.
- Add a structural check that tenant-facing modules use approved organisation-
  scoped query helpers, while recognising that structural checks supplement
  rather than replace behavioural tests.

### 5.10 Remove unsafe reuse seams

- Confirm through import analysis that the generic unscoped role service is not
  used by production code.
- Remove it, make it private to fixtures, or change its contract to require and
  validate `organisation_id`.
- Search for other service methods that mutate a tenant-owned row using only a
  globally unique resource ID and bring them under the same scoped contract.
- Keep routers thin; scoping belongs in dependencies, services and reusable
  queries rather than client-supplied body fields.

### 5.11 PostgreSQL Row-Level Security decision and prototype

This is a decision work unit, not an implicit commitment to ship RLS.

- Add an ADR comparing the accepted application-layer model with PostgreSQL
  RLS for tenant-owned tables.
- If RLS remains under consideration, prototype it on one representative module
  using a dedicated non-owner, non-`BYPASSRLS` application role,
  `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, default deny and a
  transaction-local organisation context.
- Design separate policies or database roles for tenant requests, platform
  administration, migrations and workers. A universal bypass role is not an
  acceptable application default.
- Prove connection-pool context cannot leak between requests and that context
  is cleared on commit, rollback, cancellation and error.
- Decide how user-private resources such as notifications add `user_id` to the
  organisation policy.
- Measure operational consequences for Alembic, local development, tests,
  backups, support tooling and incident recovery.
- Either approve a staged RLS rollout with migrations and rollback plan, or
  explicitly accept application-layer isolation and its strengthened contract
  suite as the project standard.

## 6. Required documentation and architecture changes

- Amend BP §8 with impersonation, session-revocation and recent-authentication
  rules.
- Amend BP §9 with the chosen per-membership permission projection and, if
  adopted, the database enforcement model.
- Amend BP §28 with request-ID validation and concrete log/Sentry redaction
  requirements.
- Amend BP §30 with webhook deduplication, ordering and login-time provider
  reconciliation.
- Amend BP §31 with the two-membership/different-role resource matrix and
  worker tenant-consistency cases.
- Record organisation-creation policy and WorkOS dashboard security settings in
  the deployment/runbook documentation.
- Add an ADR for RLS or for the explicit decision to retain application-only
  tenant enforcement.

All new scope citations must use the correct version prefix as required by
`AGENTS.md`.

## 7. Acceptance criteria

1. A WorkOS token containing an impersonator claim is rejected unless a
   separately approved impersonation mode is enabled; no impersonated action
   can be mistaken for an unaided user action.
2. The maximum accepted token lifetime, revocation exposure window, MFA policy
   and recent-authentication rules are documented, configured and tested.
3. A user's roles or effective permissions are resolved per organisation; a
   role in organisation A never changes UI or API capability in organisation B.
4. An invitation revoked at WorkOS cannot grant membership even when its
   webhook was missed, duplicated or delivered out of order.
5. Every organisation-owned resource rejects a valid foreign resource ID for
   non-members and for users who belong to both organisations.
6. Platform administrators retain only the documented cross-tenant plane;
   organisation owners cannot enter it, and platform membership alone cannot
   enter an organisation route.
7. Background workers fail closed when durable references disagree on
   organisation or job type, before calling an external provider.
8. Logout and rejected-session paths clear token state, selected organisation
   and cached server data even when WorkOS navigation fails.
9. Logs and serialized Sentry events contain no access/refresh token,
   authorization header, password, cookie, connection credential, provider
   secret or signed URL credential under the tested failure paths.
10. Organisation creation follows an explicit production policy and cannot be
    enabled accidentally through an undocumented WorkOS dashboard setting.
11. The RLS ADR records either an approved, tested rollout design or an explicit
    acceptance of application-layer isolation and its residual risk.
12. Database migrations, generated frontend API types, linting, typing,
    `make check`, the mandatory security suite and relevant real-database tests
    are green.
13. Authentication, permission, tenant-isolation, secret-handling and public
    API changes have recorded human review before they are applied or committed.

## 8. Decisions required before implementation

1. Must impersonated WorkOS sessions always be rejected, or will the product
   support a restricted, visible and fully audited support workflow?
2. What maximum access-token lifetime and session-revocation window are
   acceptable for normal users?
3. Which platform mutations require recent authentication, and how recent must
   it be?
4. Should `/me` return roles per membership, effective permissions per
   membership, or should a separate selected-organisation permissions endpoint
   own that projection?
5. Is WorkOS or the local database authoritative for invitation lifecycle, and
   what should happen when WorkOS is unavailable during login?
6. Is self-service organisation creation a supported product capability in
   production?
7. Does the project require database-enforced RLS, or is strengthened
   application-layer isolation with contract tests the accepted assurance
   level?
8. If RLS is adopted, which narrowly scoped roles and policies serve tenant
   requests, platform administration, workers, migrations and operations?

Do not begin implementation until the decisions relevant to that work unit are
recorded. Work units may proceed independently only where their contracts do
not prejudge the RLS, impersonation, invitation-authority or public API
decisions.
