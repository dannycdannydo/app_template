# Security

The practical baseline is **OWASP ASVS Level 2**. This file records the controls the template enforces and the expectations for applications built from it. Capabilities still deferred (malware scanning, document processing) are listed under Deferred controls; the controls below state the target for the final system.

## Baseline controls

- WorkOS session validation for all authenticated routes (v0.2: `app/core/security.py`, enforced by the `get_current_user` dependency).
- Default-deny authorisation; permissions are explicit, never implicit (v0.2 Scope §6.4: `require_permission`).
- Tenant-scoped queries; cross-tenant access is a bug (v0.2: `X-Org-Id` context, org-scoped queries in `queries.py`).
- Explicit CORS allowlist and Trusted Host allowlist; neither permits wildcards.
- CSRF protection where cookie authentication requires it.
- Input limits on request bodies, string lengths, and upload sizes.
- Redis-backed, distributed coarse rate limiting for `/api/v1` requests (300 requests per minute per source IP); production fails closed if its TLS Redis service is unavailable.
- Secure headers: API responses set MIME-sniffing, frame, referrer and permissions policies; the frontend edge sets CSP as well. HSTS remains the responsibility of the TLS-terminating production edge, not the local HTTP nginx container.
- Private object storage; no public buckets.
- Upload scanning hook on all uploaded content.
- Restricted external URL fetching (SSRF controls, see below).
- Webhook signature verification for inbound webhooks (v0.2: `verify_webhook_signature` in `app/core/security.py`; consumed since v0.4 by `POST /api/v1/webhooks/workos`).
- Non-public PostgreSQL and Redis; no exposed database ports.
- Least-privilege database credentials per role.
- Encrypted backups; off-site copies.
- Secret scanning, dependency scanning, and container scanning in CI.
- Non-root containers.
- Safe error messages: the standard API error format never leaks internals, stack traces, or secrets (see `API_CONVENTIONS.md`).

## Identity, tenancy and permissions (v0.2)

The identity and tenancy core enforces the following rules; each is covered by a mandatory security test (see below):

- **Session validation**: RS256 signature, exact configured issuer, client binding/audience, expiry and the required `exp`, `iat`, `iss`, `sub`, `sid`, and `client_id` claims are validated centrally; a disabled user is blocked with `403` even with a valid session. The configured issuer must exactly match a validated WorkOS token's public `iss` claim.
- **Redirect and logout safety**: post-login state accepts only same-origin local paths, and logout uses a top-level WorkOS navigation to clear the provider session rather than a background cross-origin request.
- **Identity fields are never trusted from the client**: email/name/`email_verified` come from the validated WorkOS profile; request schemas use `extra="forbid"` so smuggled identity fields are rejected outright.
- **Organisation context**: tenant-scoped routes require the `X-Org-Id` header; the organisation id is always derived from this validated context, never from a request body. Missing/malformed header is a `400`, a non-membership is a `403`, and resources outside the caller's organisation are treated as not found (`404`) where the resource model requires it.
- **Default deny**: a caller may act only through permissions granted to the roles on their memberships; a code granted to no role is denied with `403`.
- **No universal bypass**: cross-organisation support or impersonation must be explicit, limited, visible and fully audited.

## Platform plane (v0.4)

The platform administration plane (ADR-0013) is a separate authorisation plane, never a bypass of the organisation permission system:

- **Separate plane**: `require_platform_permission("platform.admin")` resolves the caller through platform memberships and role bundles only; platform routes under `/api/v1/platform/*` take no `X-Org-Id` header. A caller with no granting platform membership is rejected with `403 platform_admin_required`.
- **Cross-plane denial**: an organisation `owner` cannot call platform routes, and a platform admin without an organisation membership cannot call organisation routes; both cases are proven by the mandatory security suite. No `is_admin`/superuser boolean exists anywhere in the model or services.
- **One-time bootstrap**: `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` grants `platform_admin` exactly once, on the first verified login of that exact WorkOS email, inside the provisioning chain, audited (`platform.bootstrap_granted`); a concurrent double first-login cannot double-grant.
- **Invitation safety**: membership is created only at login-time linking (authenticated verified email matches a sent, non-expired invitation); revoked or expired invitations never grant; webhook delivery (`POST /api/v1/webhooks/workos`, HMAC-SHA256 signature with 300s tolerance) is best-effort and never authoritative for grants. The WorkOS Management API key and webhook secret are server-side only.
- **Append-only audit**: every platform lifecycle action (bootstrap, organisation create/update, invitation sent/accepted/revoked, membership role change/suspend/reactivate/remove, feature-flag change) writes an `audit_events` row; there is no update or delete path for audit rows.

## Mandatory reusable security test suite

Blueprint §31 requires a reusable security test set that runs in CI. `backend/tests/test_security_suite.py` implements it for the whole protected API surface:

- unauthenticated requests rejected (`401`);
- invalid sessions rejected — garbage tokens and tokens with tampered signatures, wrong issuer, wrong audience/client id, expired expiry, or omitted required claims (`401`);
- cross-organisation access denied (`403`);
- viewer writes denied (`403`);
- disabled users denied (`403`);
- stack traces not exposed — every error response is the standard envelope and never leaks tracebacks or internals.

The suite is table-driven: `PROTECTED_ROUTES` in that file lists every protected route once, and a completeness guard test fails when a new `/api/v1` route is registered without being added to the table. **Adding an endpoint to the table is a mandatory part of adding the endpoint itself.** Webhook-signature rejection is covered in `backend/tests/test_security.py`. The v0.5 files and jobs routes join the same table with the full case list; the oversized-upload rejection is covered by the file test suite at intent time (a declared size above `STORAGE_MAX_UPLOAD_SIZE` is rejected before any signed URL is issued, blueprint §30). Platform routes join the same table with the non-platform-admin `403` case and a cross-plane denial check (org `owner` → `403` on platform routes; platform admin without org membership → `403` on org routes).

## File security

Uploaded files are untrusted. Controls include:

- MIME and extension validation;
- page and size limits;
- decompression-bomb protections;
- worker isolation for processing;
- no execution of uploaded content;
- a quarantine state;
- a malware scanning hook.

The v0.5 release ships the storage boundary (blueprint §17, §30):

- **Size and type validation at intent time**: a declared size above `STORAGE_MAX_UPLOAD_SIZE` or a content type outside `STORAGE_ALLOWED_CONTENT_TYPES` is rejected before any signed URL is issued (`422`; the security suite covers the oversized case).
- **Server-generated object keys**: keys are `organisations/{organisation_id}/documents/{file_id}/original`; the client submits the file id, never an object path or provider, and request schemas use `extra="forbid"`.
- **Private storage with short-lived signed URLs**: buckets are private (an unsigned GET returns `403`, proven by test), upload/download happen directly between the browser and storage through temporary signed PUT/GET URLs, and the API never proxies file bytes.
- **Completion verification**: `POST /api/v1/files/{file_id}/complete` heads the stored object and fails the file (`failed`, or `quarantined` where a scanner would own the decision) when it is missing or its size does not match the declared `size_bytes`; the checksum is recorded when the provider supplies one.
- **Worker isolation for processing**: `process_file` runs in the Dramatiq worker, never in the HTTP handler.
- **Audited lifecycle**: every transition (`file.upload_started`, `file.uploaded`, `file.upload_failed`, `file.processing`, `file.ready`, `document.deleted`) is written to the append-only audit log.
- **Deferred to post-v1**: malware scanning (the quarantine state and the scanning hook seam ship in v0.5, the scanner does not), decompression-bomb protections, page limits, and server-side document processing beyond verify-and-mark-ready.

## SSRF

User-supplied URLs must not access:

- localhost;
- loopback addresses;
- private network ranges;
- cloud metadata endpoints;
- internal service names.

The storage endpoint (`STORAGE_ENDPOINT_URL`) and presigning host (`STORAGE_PUBLIC_ENDPOINT_URL`) are configuration settings, never client-supplied values, so storage adds no SSRF surface; applications must apply the same rule to any URL they construct from user input.

## Administrative access

Cross-organisation support or impersonation must be:

- explicit;
- limited;
- visible;
- fully audited.

There is no hidden universal bypass.

## Secrets

- No secrets are committed to the repository.
- `.env.example` documents every variable the application reads, with safe placeholder values; real secrets live in environment-specific secret stores.
- Never log secrets, tokens, or credentials.

## Reporting a vulnerability

If you find a security issue, report it privately to the maintainers before disclosing publicly. Include a description of the issue, affected versions, and a minimal reproduction if possible. Do not open a public issue for security vulnerabilities.

## Deferred controls

The following controls land with their owning capabilities in later releases and must be present before v1.0: malware scanning and server-side document processing (post-v1 — v0.5 ships the quarantine/failed states and the scanning hook seam), decompression-bomb protections, and rate limiting at the edge for production profiles (v0.6). The v0.5 release shipped provider-neutral private object storage with signed URLs, size/type validation, worker isolation and durable job records; the v0.4 release shipped append-only audit logging, the signature-verified WorkOS webhook consumer, and the platform plane (see above).
