# Security

The practical baseline is **OWASP ASVS Level 2**. This file records the controls the template enforces and the expectations for applications built from it. Capabilities still deferred (malware scanning, document processing) are listed under Deferred controls; the controls below state the target for the final system.

## Baseline controls

- WorkOS session validation for all authenticated routes (v0.2: `app/core/security.py`, enforced by the `get_current_user` dependency).
- Default-deny authorisation; permissions are explicit, never implicit (v0.2 Scope §6.4: `require_permission`).
- Tenant-scoped queries; cross-tenant access is a bug (v0.2: `X-Org-Id` context, org-scoped queries in `queries.py`).
- Explicit CORS allowlist and Trusted Host allowlist; neither permits wildcards.
- CSRF protection where cookie authentication requires it.
- Input limits on request bodies, string lengths, and upload sizes.
- Redis-backed, distributed coarse rate limiting for `/api/v1` requests (300 requests per minute per source IP); production fails closed if its Redis service is unavailable. Production Redis is either private (the hybrid VPS profile's non-published compose-network Redis, ADR-0012) or TLS (`rediss://` for any externally reachable Redis).
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
- **Session hardening (plan P1)**: a validated session is a bounded context (subject, session id, issued-at, expiry, optional authentication time and impersonator) and never exposes the raw claim set. A WorkOS `act` impersonation claim is captured and the auth boundary rejects the session with a generic `401`; no impersonation mode is supported. Tokens whose total lifetime (`exp - iat`) exceeds `WORKOS_JWT_MAX_LIFETIME_SECONDS` (default 3600) are rejected even while unexpired. The application keeps no online revocation denylist, so a revoked WorkOS session stays valid only until its access token expires (bounded by the maximum lifetime); that accepted window is recorded in ADR-0021. No operation currently requires recent authentication (the default WorkOS access token carries no `auth_time` and no re-authentication flow exists); the context still exposes `auth_time` where available.
- **Authoritative invitation acceptance (plan P1)**: login-time linking never grants from a local `sent` row alone. Each candidate is revalidated against the live WorkOS invitation — identity, `pending` state, verified email, organisation mapping captured at invite time and expiry — before it can grant; a provider outage, mismatch or missing provider identity is fail-closed (no membership, retried on the next login). Verified webhook event ids are persisted with a uniqueness constraint, so a duplicate WorkOS delivery is a deterministic no-op and never re-applies a refresh or writes a second audit row.

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
- **Completion verification**: `POST /api/v1/files/{file_id}/complete` heads the staging object and fails the file (`failed`) when it is missing or its size does not match the declared `size_bytes`; the checksum is recorded when the provider supplies one.
- **Worker isolation for processing**: `process_file` runs in the Dramatiq worker, never in the HTTP handler.
- **Audited lifecycle**: every transition (`file.upload_started`, `file.uploaded`, `file.upload_failed`, `file.processing`, `file.ready`, `file.quarantined`, `document.deleted`) is written to the append-only audit log.
- **Deferred to post-v1**: malware scanning (the quarantine state and the scanning hook seam ship, no scanner provider is selected), decompression-bomb protections, page limits, and server-side document processing beyond verify-and-mark-ready.

### Document authority and immutable uploads (plan P6)

The P6 closure adds four invariants on top of the v0.5 storage boundary (migration `b3c4d5e6f7a8`):

- **Server-side promotion, not presigned finals**: upload intent signs a PUT against a unique staging key (`organisations/{org}/documents/{file_id}/staging/{token}`) with a bounded lifetime (`STORAGE_UPLOAD_URL_TTL_SECONDS`, default 900 s). Completion verifies the staging object and promotes it with a server-side copy to the deterministic final key, which is never presigned for a PUT — replaying an old capability can only recreate an unreferenced staging object, never mutate an approved file's bytes.
- **Pinned content identity**: completion stores the promoted object's provider checksum in `files.content_identity`. Downloads and AI reads require `ready` **and** a matching identity; a same-size same-key overwrite after approval fails closed. The source authority (`app/modules/files/authority.py`) re-resolves every `documents/` reference against the live `files` row (org-scoped, `ready`, identity match) — a prefix, object HEAD, MIME type or size is never itself authorisation — and every scratch reference against a live `ready`, unexpired durable intent.
- **Scanning gate**: the worker resolves the configured `ContentScanner` before a file may become `ready` (and therefore AI-readable or downloadable). A rejection quarantines the file (`file.quarantined`) and fails the job permanently; a scanner outage raises transiently, so unscanned content is never promoted to trusted. `FILE_SCAN_PROVIDER=none` is the reviewed template position (no scanner), and production requires `FILE_SCAN_ACKNOWLEDGE_UNSCANNED=true` so an unscanned deployment is deliberate.
- **Atomic completion and durable scratch**: the file transition, audit row, durable processing job and its dispatch outbox event commit in one transaction, and a replayed completion returns the same job instead of scheduling a duplicate. AI scratch uploads persist a durable `ai_scratch_uploads` intent with a bounded global expiry (`AI_SCRATCH_MAX_LIFETIME_SECONDS`), independent of any optional per-organisation retention policy; the retention sweep removes expired intents and their objects, with an object-store lifecycle rule as the asynchronous backstop. Download is available only at `ready`, never at `uploaded`/`processing`.

## Durable-job delivery security

- **PostgreSQL owns scheduling intent**: a durable job and its strict,
  reference-only outbox event commit together. Redis is transient execution
  transport; no API or business service sends durable actors directly.
- **No executable persisted routing**: coordinator actor selection comes from
  a checked-in allow-list. Dispatch payloads carry only a job id; they never
  carry object keys, signed URLs, recipients, credentials, content or import
  paths.
- **At-least-once with ownership**: a coordinator crash may duplicate a broker
  message. Worker execution leases and owner-checked mutations prevent
  concurrent business execution and stale settlement, while domain handlers
  remain idempotent. This is not an exactly-once external-side-effect claim.
- **Safe operations data**: outbox errors are bounded/sanitised; logs and
  metrics use event types and opaque ids only. `dead` events are investigated,
  never automatically replayed; guarded reconciliation creates deduplicated
  recovery intents only for eligible queued jobs.

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
- `.env.example` documents every variable the application reads, with safe placeholder values; real secrets live in environment-specific secret stores. The production surface is `.env.production.example` (Scope §6.6), which documents the hybrid VPS deployment inputs (registry, host, release path, domain) and every production container setting.
- Never log secrets, tokens, or credentials. The BP §28 never-log list (passwords, tokens, authorisation headers, signed URLs, full connection strings) is enforced by test.

## Hybrid VPS production profile (Scope §6.6, blueprint §35.1)

The generic Linux VPS / container-host profile (`deploy/compose/compose.hybrid-vps.yml`, `deploy/caddy/`, `.github/workflows/deploy-vps.yml`) is the portable production baseline. It runs Caddy, the static Vue artifact, the FastAPI backend, the Dramatiq worker and isolated private broker/rate-limit Redis services on the host; PostgreSQL, object storage, WorkOS, transactional email and monitoring stay external (ADR-0007). The following controls are mandatory for any deployment built from this profile:

- **Firewall**: the host firewall allows only 22/TCP (SSH), 80/TCP and 443/TCP from the public internet, plus the egress ports the external services need. Configure it at the provider or host level (ufw/firewalld/nftables); never expose PostgreSQL, Redis, MinIO or the API port directly.
- **SSH keys only**: password and root SSH login are disabled (`PasswordAuthentication no`, `PermitRootLogin no`); the deploy workflow authenticates with a dedicated deploy key (GitHub secret `DEPLOY_SSH_KEY`) that has no password and is restricted to the release directory and docker group on the host.
- **Non-public isolated Redis**: both Redis containers bind only to the internal
  compose network, use separate strong passwords and fail fast when either is
  absent. Broker Redis is AOF-backed/`noeviction`; rate-limit Redis uses a
  separate process and volume with counter-oriented eviction.
- **Automatic security updates**: unattended-upgrades for the host OS and a documented weekly patch cadence; the application images are rebuilt from pinned bases (`python:3.13-slim`, `node:24-alpine`, `redis:7-alpine`, Caddy `v2.11.4`) and scanned by the CI container-scan job.
- **Monitoring and alerting**: external uptime checks against `/health` and `/ready`, metrics scraping of `GET /metrics`, and alerts for readiness/API failures, worker/job failures, disk pressure, certificate expiry and backup failures (docs/operations.md).
- **Disk alerts**: the host disk and the Caddy/Redis log volumes are monitored with thresholds (default alert at 80% usage).
- **Container resource limits**: every service declares explicit CPU/memory `deploy.resources.limits` and JSON-file log rotation (`max-size`/`max-file`) in the compose file.
- **Documented rollback**: every release is immutable (image tagged by commit SHA, frontend artifact checksum-verified into `releases/<sha>`); `releases/current` is an atomic symlink and the previous release is retained, so rollback is a one-line symlink flip plus `docker compose up -d` (docs/operations.md, docs/backup-and-recovery.md).
- **Off-site configuration backups**: the `.env.production` file, the Caddyfile, the compose file and the `releases/` metadata are backed up off-site; without them a lost host cannot be rebuilt (docs/backup-and-recovery.md — secret recovery, lost VPS replacement).

### Trusted proxy and client-IP handling

Caddy terminates TLS and is the only entry point, so the API treats it as the
only trusted proxy:

- Caddy sets `X-Forwarded-For` for proxied requests; the API's trusted-host allowlist (`TRUSTED_HOSTS`) contains only the real production domains, so a request cannot spoof a Host header.
- The API and Caddy share a private `edge` network; the API starts with `--proxy-headers --forwarded-allow-ips=<edge subnet>`, so uvicorn rewrites `request.client` from `X-Forwarded-For` **only** for the Caddy peer and resolves the right-most untrusted entry (the real client Caddy appended). A browser-supplied chain is discarded, and `--forwarded-allow-ips=*` is prohibited because it would trust the forgeable left-most value. `backend/tests/test_proxy_trust.py` and `scripts/assert_deployment_boundaries.py` enforce both properties.
- The **edge** rate limiter keys on `{remote_host}` (the real client IP) and the **application** limiter keys on the resolved `request.client.host`, so both are per-client-IP and distinct clients keep distinct quotas.

### Browser storage access (CSP and CORS)

- **CSP `connect-src` is scoped to the configured storage origin.** `deploy/caddy/Caddyfile` injects `{$STORAGE_PUBLIC_ORIGIN}` (the bare `scheme://host[:port]` origin of `STORAGE_PUBLIC_ENDPOINT_URL`, no path/query/wildcard) so the browser can PUT to the signed upload URL. WorkOS and `'self'` are unchanged. The hybrid edge fails fast when the origin is unset; the local nginx template substitutes the same value.
- **Object-store CORS is a provider-side control** and must allow the browser's direct `PUT`/`GET`/`HEAD` from the exact frontend origins, with `Content-Type` (plus provider signing headers) — never `*`. See docs/operations.md → Browser storage access.

### Edge rate limiting

An unqualified Caddy `rate_limit` directive is not acceptable because stock Caddy ships no such directive (Scope §6.6). The profile uses the pinned, tested implementation:

- `deploy/caddy/Dockerfile` builds Caddy `v2.11.4` with the pinned `mholt/caddy-ratelimit v0.1.0` module via xcaddy; both versions are pinned and the upgrade procedure is documented in the Dockerfile.
- `deploy/caddy/Caddyfile` applies per-client-IP zones: 600 events/min for `/api/*`, `/health` and `/metrics` (looser than the application's 300/min so the app stays authoritative), 2400 events/min for static assets, and no limit on `/ready` so deployment health checks are never throttled.
- CI builds the image and runs `caddy validate`; the rate limiting itself was verified functionally (200 × 3 then 429 on the fourth request). An external WAF (e.g. Cloudflare) may sit in front instead; if one is used, keep the Caddy security headers and TLS termination behind it and document the WAF rules in `docs/operations.md`.

## Email and notification security (v0.6)

Email and notifications follow the same default-deny, tenant-scoped, worker-isolated rules as every other capability (ADR-0015, ADR-0016):

- **Email is sent only from Dramatiq worker tasks, never from an HTTP handler** (blueprint §20, ADR-0004) — proven by test. A slow or failing SMTP relay can never block an API request, and the durable delivery-row lifecycle (queued → running → succeeded/failed) with bounded, idempotent retries prevents double-sends.
- **SMTP credentials are server-side secrets**: they live only in the environment file / secret store, are never logged (BP §28 never-log list), and are never sent to the frontend. Production fails fast: `EMAIL_PROVIDER=fake` is rejected, and `EMAIL_PROVIDER=smtp` requires explicit `SMTP_HOST`/`SMTP_PORT`/`EMAIL_FROM`. STARTTLS is used for port 587 relays (`SMTP_USE_TLS=true`).
- **Sentry DSN is an optional secret**: the SDK is initialised only when `SENTRY_DSN` is set; the never-log list keeps tokens, passwords, authorisation headers, signed URLs and full connection strings out of Sentry payloads.
- **Notifications are org-scoped**: the API returns only the caller's own notifications in the caller's organisation; a foreign or other-user notification id resolves to 404; the new `notifications.read` / `notifications.manage` permission codes follow default-deny (owner/administrator/manager: both; member: read; viewer: none). All four notification routes are in the mandatory security suite's `PROTECTED_ROUTES` matrix (unauth/invalid-session/disabled/viewer-write/cross-org/stack-trace cases).
- **Delivery failures are audited**: a failed email delivery writes a `failed` delivery row and an audit event; test-send is audited too.

## AI security (v0.7)

The AI layer (ADR-0017, ADR-0018; v0.7 Scope §6.4/§6.5/§6.7) applies the same default-deny, tenant-scoped, provider-neutral rules as every other capability. `AIService.execute` is the only supported entry point — feature modules never import an LLM SDK or name a provider/model — and the checked-in task/prompt/model registries validate at startup and CI:

- **Prompt injection is untrusted input**: document content, text and messages reach the provider through an allowlisted template renderer (no arbitrary template execution, no secret interpolation), optional pre-dispatch redaction hook, and bounded attachment sets. The demonstration task treats every input as untrusted; derived applications must threat-model their prompts the same way — a model is not a security boundary, and outputs are never executed, rendered as HTML, or used to grant privileges.
- **External-data disclosure and redaction**: storage references are resolved to bounded in-memory attachments server-side and validated (5 MB per file / 10 MB combined, MIME allowlist, SHA-256 digest); a storage reference is never rendered as if it were document content and never reaches the provider outside the adapter's approved inline mapping. The redaction hook is the deployment's chosen PII control; the template wires the seam (`None` by default) and documents it (v0.7 Scope §6.4).
- **Provider data handling**: provider credentials (OpenAI/Anthropic/DeepSeek/Azure keys, Vertex credential material) are server-side secrets — never in the API, frontend, logs, Sentry or audit metadata (BP §28 never-log list enforced by test). Adapter SDK/HTTP clients are confined to `app/ai/providers/` (import-boundary test). Gemini is reached **through Vertex AI only** (ADC/workload identity/service account; no Gemini Developer API key). Production fails fast on incomplete/misconfigured enabled adapters and publicly reachable local endpoints; the local adapter is never exposed to browsers.
- **Output validation**: every structured result is validated against the task's Pydantic schema before it is returned; malformed output triggers at most one bounded repair request plus bounded task retries, and unvalidated data is never returned as success. Safe error codes carry no provider output, prompts or content.
- **Audit and retention**: every attempt writes an org-scoped `ai_requests` row; `ai_outputs` stores references and digests by default — never attachment bytes or retained content unless the task-level opt-in **and** the organisation retention policy both permit it. Retention is enforced by the `ai.retention` job per organisation policy, audited (`ai.retention_deleted`). Audit events identify actor, task, request id, routing decision and outcome — never prompts or document contents. AI is **default-off** for new organisations; request-time enforcement (enabled state, allowlists, overrides, monthly budget) happens in `AIService`, never only in a router/UI, with cross-org ids indistinguishable from missing (BP §9).
- **No client credentials**: the API and frontend never receive provider credentials or raw provider configuration; the browser only ever talks to the organisation-scoped demonstration endpoint, never to a provider or to storage.
- **Approval boundaries**: AI work is gated by the existing organisation permission model (the demo endpoint uses `documents.upload` and joins the mandatory security suite's `PROTECTED_ROUTES` matrix); organisation AI settings are platform-admin managed and audited. No generic arbitrary-prompt endpoint exists — only the checked-in task registry can name work. Work units touching tenant isolation, secret handling or the public API surface are reviewed through the implement → review → apply-and-commit loop (AGENTS.md, BP §33).

## AI large-file transfer security (v0.8)

The v0.8 transfer modes (ADR-0017 amendment, `TEMPLATE_V0_8_SCOPE.md` §6.1–§6.7) extend the same default-deny, tenant-scoped, provider-neutral rules to large files:

- **Caller-supplied URLs are prohibited**: `AIRequest` carries only a task name and a private `storage_reference`; there is no caller-supplied URL, `gs://` URI, provider file id, transfer mode or credential field (request schemas use `extra="forbid"`). The reference must live in the caller's own organisation namespace or resolution fails closed before any storage metadata is read. This keeps the SSRF boundary unchanged — the storage endpoint and presigning host remain configuration settings, never client-supplied values.
- **Bounded verification before any transfer**: non-inline sources are streamed through bounded temporary-file storage with ownership, size, MIME and incremental SHA-256 verification; a 50 MB PDF is never accumulated in worker memory or embedded in broker/JSON payloads. The source digest is validated before upload, staging, URL minting or dispatch.
- **Managed signed URLs are temporary bearer capabilities**: a URL is minted just-in-time per dispatch from a retained private S3-compatible object, exact-object, HTTPS, read-only, with a short TTL (default 900 s, max 1,800 s). It is never returned to the caller, persisted, audited or logged, and every log/error/telemetry boundary redacts it (BP §28 never-log list enforced by test). Retry reuse applies to opaque external references (provider file ids / `gs://` URIs), never to URLs; a URL is minted anew per dispatch.
- **Provider retention and deletion**: OpenAI uploads use `purpose=user_data` with the shortest supported `expires_after` and best-effort terminal deletion; Anthropic uses the pinned beta Files API with delete-only retention and mandatory provider-file reconciliation; Vertex stages into the deployer-provisioned private same-region bucket and relies on a console-configured Object Lifecycle rule (`age = 1` day → Delete) as an asynchronous backstop. The application never creates, configures or manages a GCS bucket and runs no scheduled GCS cleanup or reconciliation; it deletes only the exact AI-owned staging object it uploaded (best-effort terminal deletion). Soft-delete/versioning/retention holds on the bucket extend object retention/recoverability rather than guaranteeing that a live object persists.
- **AI-owned derivatives only**: provider-hosted copies and GCS staging objects are AI-owned. Their deletion never deletes the feature-owned source object or changes its lifecycle; the durable reference rows store opaque external ids and digests, never bytes, credentials, headers, raw responses or managed URLs.
- **Default-deny enablement**: non-inline modes are disabled at deployment unless `AI_ENABLED_TRANSFER_MODES` explicitly enables them, and production fails fast on an enabled mode without its supporting provider, Vertex staging bucket, expiry/TTL bounds or same-region/location configuration. Azure OpenAI, DeepSeek and local adapters declare no non-inline mode and reject large files before any transfer.
- **Tenant isolation and audit**: `ai_attachment_references` rows are organisation-scoped with org-scoped queries; cross-organisation source/reference access is denied. Mode selection, transfer outcome/reuse, expiry, deletion and reconciliation backlog are low-cardinality audit events and metrics that never carry content, request ids, object keys or signed URLs.
- **Durable ask and bounded synchronous work (plan P9)**: `/api/v1/ai/ask` queues a reference-only `ai.execute` job by default. The bounded question is stored temporarily on the tenant-scoped queued AI request, has a hard expiry, is cleared on terminal settlement, and is never placed on the broker or in logs/audit metadata. The optional `sync=true` path rejects a source above `AI_ASK_MAX_SYNCHRONOUS_BYTES` (default 5,000,000, never above the inline aggregate threshold) with `ai_ask_attachment_too_large`, so no large-file transfer runs inside an HTTP request. Policy and durable source authority are checked before attachment bytes are read on both paths. Validated answer content is available from the result endpoint only when the organisation has explicitly configured AI output retention and is deleted by the existing retention sweep.

The security suite (`test_security_suite.py`, `test_ai_import_boundary.py`, `test_ai_transfer_contracts.py`, `test_ai_gcs_managed_url.py`, `test_ai_anthropic_upload.py`, `test_ai_openai_upload.py`, `test_ai_vertex_staging.py`) proves the matrix: import boundaries keep transfer/provider concepts inside `app/ai/`, redaction tests keep logs/Sentry/audit/broker rows URL- and content-free, migration validity and generated-client drift stay green in CI, and the opt-in provider contract suites run only against dedicated non-production accounts.

## Database row-level security and role separation (v0.9)

The v0.9 release (ADR-0022, `TEMPLATE_V0_9_SCOPE.md`) adds PostgreSQL Row-Level
Security (RLS) as a **default-deny backstop** to the application's organisation
isolation. It supplements, and never replaces, the validated membership,
permission and `404` controls above: even with RLS enabled, every service query
keeps its explicit `organisation_id` predicate and a foreign row remains a
`404`.

- **Least-privilege database roles.** The ordinary API and worker path runs as
  `app_runtime`: non-owner, non-superuser, `NOBYPASSRLS`, `NOINHERIT`. The outbox
  coordinator, reliability-metrics refresh and `reconcile_jobs` run as
  `app_coordinator`, a second non-bypass role whose policies are scoped to
  dispatch state and whose UPDATE authority is granted per column. `DATABASE_URL`
  (`app_owner`) is the schema-owner/migration credential and is never the runtime
  path; production refuses to start without the runtime credential.
- **No hidden universal bypass.** There is no implicit or request-selectable
  bypass, and no request handler chooses its own role. Cross-tenant platform
  access goes through explicit policies keyed to a transaction-local platform
  context bound only after `require_platform_permission` authorises the caller;
  the one-time bootstrap, the signature-verified `user.deleted` webhook and the
  operator recovery/teardown CLI bind a separate, narrower `app.platform_service`
  context. Platform status alone grants no tenant-row access.
- **The one operational exception is isolated and audited.** `app_operator`
  (`DATABASE_OPERATOR_URL`) is the only application role allowed to carry
  `BYPASSRLS`, because its reviewed backup/restore/support operations are exactly
  the cross-tenant reads a policy cannot express. It owns no table, has no
  membership in either direction, the runtime role cannot `SET ROLE` it, it is
  resolved only by `resolve_operator_database_url` (never falling back to the
  runtime or owner credential), and no HTTP process or worker loads it. Every
  use records who/when/where and the operation — never row contents, secrets,
  tokens or provider responses (BP §28 never-log list).
- **Fail-closed context.** Tenant, user and job context is bound with a
  parameterised transaction-local `set_config(..., true)` after validation.
  Missing, empty or malformed context returns no tenant rows and fails closed for
  writes; it never means unrestricted access. Context cannot survive a commit,
  rollback, exception, cancellation, timeout or pooled-connection reuse.
- **Default-deny, matched policies.** Every enabled table has RLS enabled and
  forced with matching `USING`/`WITH CHECK` policies, so inserts and updates
  cannot create or move a row into another organisation. A `NULL` tenant key on
  `audit_events`/`outbox_events` is never read as "all rows": global and
  cross-tenant history is reachable only under the validated platform context or
  the isolated operator credential. `notifications` additionally requires the
  transaction-local user id, and indirect tables use a denormalised key or a
  tested parent policy (ADR-0022 decision 6).
- **Pre-tenant and control-plane lookups are narrow.** Authentication binds
  `app.user_id` before the caller's own membership/invitation lookup under
  SELECT-only user-keyed/email-keyed policies; a pre-tenant user context can
  never write a membership, role grant or invitation. The verified
  `invitation.revoked` webhook binds a single-row provider-keyed policy for a
  read/lock and status flip only. Worker context comes from the durable `jobs`
  row, never the broker.
- **Startup/deployment gate.** `app.db.role_checks` refuses to start any normal
  runtime process (API, Dramatiq worker, outbox coordinator) when its credential
  owns a table, carries `SUPERUSER`/`BYPASSRLS`/`CREATEDB`/`CREATEROLE` or
  inherits a privileged role, and asserts that `app_operator` is the only
  `BYPASSRLS` application role. It is also `make verify-db-roles`.

The real-PostgreSQL suites prove the controls: `test_rls_records_db.py` (P2
prototype), the per-group `test_rls_*_enablement_db.py` suites,
`test_rls_indirect_rows_db.py` (indirect-row strategies),
`test_rls_operator_credential_boundary.py` (the operator credential) and the
unchanged `test_org_isolation_matrix_db.py`, `test_tenant_isolation_registry.py`
and `test_security_suite.py`.

## Reporting a vulnerability

If you find a security issue, report it privately to the maintainers before disclosing publicly. Include a description of the issue, affected versions, and a minimal reproduction if possible. Do not open a public issue for security vulnerabilities.

## Deferred controls

The following controls land with their owning capabilities in later releases and must be present before v1.0: malware scanning and server-side document processing (post-v1 — v0.5 ships the quarantine/failed states and the scanning hook seam), and decompression-bomb protections. The v0.6 release shipped edge rate limiting for the hybrid VPS production profile (a pinned, tested Caddy build — see "Edge rate limiting" above), plus the mandatory §35.1 protections documented above. The v0.5 release shipped provider-neutral private object storage with signed URLs, size/type validation, worker isolation and durable job records; the v0.4 release shipped append-only audit logging, the signature-verified WorkOS webhook consumer, and the platform plane (see above).
