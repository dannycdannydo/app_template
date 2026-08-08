# Architecture

A condensed view of the system shape and request flow. The authoritative design standard is `Internal_Custom_Application_Starter_Architecture_v2.md` (blueprint §4, §5, §6, §40); individual decisions are recorded in `docs/decisions/`.

## System shape

The default architecture is a **modular monolith**. Microservices are not part of the default; a service may be extracted only when there is a demonstrated operational or organisational need.

```text
Vue SPA
   │
   ▼
FastAPI
   │
   ├── PostgreSQL
   ├── Redis
   ├── Dramatiq workers
   ├── Object storage
   ├── WorkOS
   ├── Email provider
   ├── AI / LLM providers (app/ai)
   └── External integrations
```

## Backend structure

```text
backend/app/
├── main.py              app factory, lifespan, middleware, exception handlers
├── api/                 routers and shared dependencies (thin)
├── core/                config, exceptions, logging, security, feature flags
├── db/                  declarative base, session, conventions, migrations
├── modules/             domain modules (users, organisations, teams, ...)
├── integrations/        provider adapters (workos: organizations, invitations)
├── storage/             provider-neutral storage interface + adapters
├── email/               email interface + adapters
├── ai/                  AI application layer (v0.7): AIService, task/prompt/model registries, provider adapters
├── events/              domain events / outbox
├── workers/             Dramatiq tasks
└── observability/       logging, tracing, metrics
```

## AI application layer (v0.7)

The template ships a **provider-neutral AI layer** (ADR-0017) as a platform
package, not a business module. Application code calls only
`AIService.execute(request: AIRequest) -> AIResult` and names a task
(e.g. `document.classify`); it never imports an LLM SDK, selects a model,
formats a provider request, parses provider JSON or computes cost.

```text
Feature service
    │
    ▼
AIService.execute(task=...)          app/ai/service.py
    │
    ├── Task registry                app/ai/tasks/        (prompt name/version, capabilities, schema, retry/fallback)
    ├── Prompt registry              app/ai/prompts/      (versioned YAML, immutable versions)
    ├── Model registry + router      app/ai/models/       (capabilities, pricing, ordered fallback)
    ├── LLMProvider adapters         app/ai/providers/    (OpenAI, Anthropic, DeepSeek, Azure, Vertex, local)
    └── ai_requests / ai_outputs     usage, cost, audit, retention (v0.7 Scope §6.5)
```

- `app/ai/providers/` is the only place provider SDKs may be imported; a
  repository test enforces the boundary (ADR-0017, mirroring the storage
  boto3 rule). The deterministic `FakeLLMProvider` is the default test adapter.
- Google Gemini is reached **through Vertex AI only** (ADR-0018): project and
  location settings plus server-side ADC / workload-identity / service-account
  credentials; no `GEMINI_API_KEY` or Google AI Studio / developer-API path
  exists.
- Small bounded tasks run synchronously; document-scale work enqueues an
  `ai.execute` job on the `ai` queue with the durable record-then-enqueue
  lifecycle. Organisation AI settings are default-off, budget-enforced in
  `AIService`, and content never reaches logs, Sentry or audit metadata
  (retained output is a per-organisation policy with a documented deletion
  path).

Each domain module normally uses: `models.py`, `schemas.py`, `router.py`, `service.py`, `queries.py` (optional), `permissions.py`, `tests/`. A mandatory repository class is not part of the architecture. The platform plane lives in `modules/platform_admin` (organisation/membership administration), `modules/invitations`, `modules/feature_flags`, `modules/audit` and `modules/webhooks`, all gated by `require_platform_permission` in `api/dependencies.py`.

## Request flow

```text
HTTP Request
    │
    ▼
Router
    │
    ▼
Service
    │
    ▼
SQLAlchemy / queries.py
    │
    ▼
PostgreSQL
```

- **Routers** are thin: HTTP parsing, dependency injection, response codes, request/response schemas, authentication context, and calling services.
- **Services** own business rules, permission enforcement, transaction boundaries, orchestration, domain state changes, and audit/event creation.
- **SQLAlchemy** may be used directly inside services for simple operations; reusable or complex queries live in `queries.py`.

## Identity flow and request context (v0.2)

WorkOS owns login and sessions; the application owns the internal user record, organisation membership, roles, and permissions. Every protected request resolves through the shared dependencies in `app/api/dependencies.py`:

```text
Authorization: Bearer <session-token>
        │
        ▼
validate RS256 signature, exact configured issuer, client binding,
and required exp / iat / iss / sub / sid claims      → 401 invalid_session
        │
        ▼
map workos_user_id to internal user (provision on first login)
        │   (email/name from the WorkOS profile, never the client)
        ▼
user disabled?                                         → 403 user_disabled
        │
        ▼
X-Org-Id header (tenant-scoped routes only)
        │   missing / malformed                        → 400
        ▼
active membership for (user, org)?                     → 403 not_a_member
        │
        ▼
require_permission(code): code in the membership's
role bundles? (default deny)                           → 403 permission_denied
        │
        ▼
service call, org-scoped queries (queries.py) — resources outside
the caller's organisation behave as not found (404)
```

- `GET /api/v1/me` and `POST /api/v1/organisations` are the two bootstrap endpoints: they require only a valid session, because the caller is not yet a member of any organisation. Every other `/api/v1` route requires both the Bearer token and the `X-Org-Id` header.
- The organisation id is always derived from the validated header context, never from a request body; request schemas use `extra="forbid"` so identity fields cannot be smuggled in.
- The security properties of this flow are enforced by the mandatory reusable security suite in `backend/tests/test_security_suite.py` (blueprint §31), which parametrises over the whole protected surface.

## Platform plane and request flow (v0.4)

The platform administration plane is a second, orthogonal authorisation plane (ADR-0013, blueprint §9). It exists to administer organisations, memberships, invitations, feature flags and audit history across tenants without ever bypassing the organisation permission system.

```text
Authorization: Bearer <session-token>
        │
        ▼
validate RS256 signature, exact configured issuer, client binding,
and required exp / iat / iss / sub / sid claims      → 401 invalid_session
        │
        ▼
map workos_user_id to internal user (provision on first login;
bootstrap hook may grant platform_admin for the configured email)
        │   (email/name/email_verified from the WorkOS profile, never the client)
        │
        ▼
user disabled?                                         → 403 user_disabled
        │
        ▼
require_platform_permission("platform.admin"): active platform membership
whose role bundles grant the code? (default deny; no X-Org-Id consulted)
                                                        → 403 platform_admin_required
        │
        ▼
service call — platform routes operate across organisations
(organisation id comes from the path, never a request body)
```

- Platform routes live under `/api/v1/platform/*` and take no `X-Org-Id` header: the caller acts as a platform administrator, not as a member of the organisation they administer.
- The two planes never grant across each other: an organisation `owner` without a platform membership gets `403 platform_admin_required` on platform routes, and a platform admin without an organisation membership gets `403 not_a_member` on organisation routes. No `is_admin`/superuser boolean exists anywhere.
- `GET /api/v1/me` returns `platform_roles` (empty for non-admins); the frontend uses it only for UI gating — the backend remains the enforcement point.
- Every platform mutation is audited through the append-only `record_event` service (`platform.bootstrap_granted`, `organisation.created`, `invitation.sent`, `membership.role_changed`, `feature_flag.changed`, ...).

## Invitation flow (v0.4)

The WorkOS Invitation API is the standard onboarding path; WorkOS owns invitation delivery and expiry, the application owns the membership grant.

```text
Platform admin invites → POST /api/v1/platform/organisations/{id}/invitations
        │   validates platform.admin, ensures/lazily backfills the WorkOS org
        │   mapping, calls the WorkOS Invitation API through the adapter
        ▼
invitations row (status=sent, expiry, workos_invitation_id) + audit invitation.sent
        │   — no membership row exists yet
        ▼
Invitee accepts and signs in → get_current_user provisioning chain
        │   links invitation by authenticated (verified) WorkOS email
        ▼
link_invitation_on_login: active membership with the intended role,
invitation marked accepted, audit invitation.accepted + membership.role_changed
```

- Acceptance is authoritative and happens at login time: revoked or expired invitations never grant, an email mismatch never grants, and a login without any webhook delivery still links the invitation.
- `POST /api/v1/webhooks/workos` (signature-verified, HMAC-SHA256, 300s tolerance) refreshes best-effort invitation state only; it never grants membership.
- WorkOS invitations are sent into the mapped WorkOS org; `organisations.workos_organisation_id` is created eagerly at platform org creation and lazily backfilled at first invite. The mapping is never client-writable.

## Frontend structure

Vue 3 + TypeScript SPA. Directory layout, conventions, and state-management split follow blueprint §14; UI follows the design system in blueprint §16 (reusable application components above shadcn-vue primitives). API types are generated, never hand-written (blueprint §15).

```text
frontend/src/
├── api/                 generated client (client.ts) + typed error envelope
├── queries/             TanStack Query composables — the only place HTTP happens
├── stores/              Pinia client state (session, ui, organisation)
├── features/            feature modules; auth/ is the WorkOS adapter seam
├── router/              routes + requiresAuth + requiresPlatformAdmin guards
├── layouts/             application shell (sidebar, header, user menu)
├── components/          reusable application components (DataTable, forms, ...)
└── views/               route-level screens (login, callback, records, Platform*View.vue)
```

### Platform Admin Centre (v0.4)

The `/platform` route section (dashboard, organisations, org detail with memberships/invitations/feature flags, invite form, feature-flag catalogue, platform-administrator lifecycle, audit view) is served by `Platform*View.vue` screens gated by a `requiresPlatformAdmin` router guard that reads `platform_roles` from `GET /api/v1/me`; the `SidebarNav` entry appears only for platform admins. This is UI awareness only — every platform endpoint is enforced server-side by `require_platform_permission`. Platform queries live in `src/queries/platform.ts` keyed `['platform', ...]` as cross-org server state.

### Frontend auth flow (v0.3)

WorkOS owns login and session management end-to-end; the frontend only ever presents the session token to the backend and never submits identity fields.

- `src/features/auth/workos.ts` is the only module importing the WorkOS browser SDK (`@workos-inc/authkit-js`, ADR 0011); it exposes `startLogin`, `completeLogin`, `signOut` and `getSession`.
- `/login` starts the flow with "Continue with WorkOS"; the callback route `/auth/callback` completes the authorization-code exchange and stores the session. A protected-route return target is carried in OAuth state and accepted only when it is a same-origin local path, preventing open redirects.
- Sign-out uses a top-level WorkOS logout navigation to the registered `/login` URI. This lets WorkOS clear its own session cookie even where browsers block third-party-cookie operations; the central `401` handler uses the same path after clearing local session state.
- The generated API client (`src/api/client.ts`) injects the session token as a Bearer `Authorization` header and the selected organisation as `X-Org-Id` on every call; a central `401` handler clears the session and returns to `/login`.

### Frontend state boundaries (blueprint §14)

- **Server state** (fetched, cached, invalidated data) belongs to TanStack Query and lives in `src/queries/` composables built on the generated client. No Vue component or Pinia store imports `src/api/client.ts` directly.
- **Client state** (session, sidebar collapsed state, selected organisation) lives in Pinia stores (`stores/session.ts`, `stores/ui.ts`, `stores/organisation.ts`) and is persisted client-side only. The selected organisation is client state: it is not fetched server data, but it drives the `X-Org-Id` header and invalidates org-scoped queries on switch.
- API errors arrive in the standard envelope (`code`, `message`, `details`, `request_id`, blueprint §13) and are normalized into a typed client error used by toasts and forms.

## Storage and direct upload flow (v0.5)

Object storage is provider-neutral (ADR-0006, blueprint §17): `app/storage/` defines one `ObjectStorage` interface — `create_upload_url`, `create_download_url`, `head_object`, `delete_object`, `ensure_bucket` — and adapters implement it. Exactly two adapters ship: `S3Storage` (boto3, S3-compatible including MinIO, selected by `STORAGE_PROVIDER=s3`) and `FakeObjectStorage` (in-memory, used by the pytest suite). No module outside `app/storage/` imports a provider SDK; application code depends on the interface only. `STORAGE_PROVIDER=fake` is rejected in production and the production config fails fast without explicit S3 credentials, bucket and endpoint.

Files are org-scoped records in the `files` table. Object keys are always server-generated (`organisations/{organisation_id}/documents/{file_id}/original`) and the client never supplies an object path or storage provider (`extra="forbid"` on request schemas). The direct upload flow keeps bytes out of the API:

```text
POST /api/v1/files                      intent: validate filename/content-type/size
                                        (documents.upload), create `pending` file
                                        record, return {file_id, upload_url, expires_at}
        │   browser PUTs bytes straight to the signed URL (object storage)
        ▼
POST /api/v1/files/{file_id}/complete   documents.upload: head the object, verify
                                        existence + size (+ checksum when supplied);
                                        `uploaded` + enqueue the processing job
        │   worker runs process_file (job_type "file.processing")
        ▼
file ready → GET /api/v1/files/{file_id}/download-url returns a short-lived signed
GET URL; DELETE /api/v1/files/{file_id} soft-deletes (deleted_at) and removes the
object from storage (document.deleted audit)
```

File lifecycle statuses: `pending → uploaded → processing → ready` with the failure states `failed` / `quarantined` and the soft-delete state `deleted`. Every transition is audited append-only (`file.upload_started`, `file.uploaded`, `file.upload_failed`, `file.processing`, `file.ready`, `document.deleted`). A stored object whose size does not match the declared `size_bytes` fails the file at completion.

## Worker and durable job flow (v0.5)

Long-running work runs in Dramatiq workers, never in HTTP handlers (ADR-0004, blueprint §18). The worker is the same backend image running `uv run dramatiq app.workers` (`make worker`, the `dev-docker` `worker` service, and natively as part of `make dev`), with Redis as the broker on `REDIS_URL`.

Jobs are durable, tenant-scoped rows in the `jobs` table. The service writes the row before enqueuing (record-then-enqueue), so a durable `queued` record exists even if the broker is down, and the bounded retry policy self-heals a job that was never picked up:

```text
HTTP request → jobs_service.create_and_enqueue()   writes `queued` row, then
                                                   enqueues the task
        │
        ▼
Dramatiq worker → mark_running → update_progress(0–100) → succeed | fail
        │                                                        │
        ▼                                                        ▼
job `succeeded`, file `ready`                     job `failed` + error_code/error_message,
(job.succeeded audit)                             file `failed` (job.failed audit)
```

Retries are bounded: transient errors retry up to `MAX_ATTEMPTS` total attempts; permanent validation errors are not retried. Terminal states (`succeeded` / `failed` / `cancelled`) are never re-run, and completion/failure is idempotent. The org-scoped job endpoints `GET /api/v1/jobs` (list, paginated, `status` / `job_type` filters) and `GET /api/v1/jobs/{job_id}` (status + progress 0–100) let the frontend poll a processing file to completion; they are gated by `documents.read` (ADR-0014 — the files module is the only job producer today, so a generic `jobs.*` permission waits for a second producer).

## Cross-cutting conventions

- **API**: REST, JSON, OpenAPI, `/api/v1` prefix (see `API_CONVENTIONS.md`).
- **Errors**: one structured error format with `code`, `message`, `details`, `request_id` (see `API_CONVENTIONS.md`).
- **Database**: SQLAlchemy 2 models + Pydantic 2 schemas, Alembic migrations, shared naming/timestamp/UUIDv7 conventions (blueprint §7, §10).
- **Configuration**: typed `pydantic-settings` model, fail-fast on invalid production config.
- **Abuse controls**: Redis provides a distributed, fail-closed coarse `/api/v1` rate limit (ADR-0012); production uses TLS Redis (`rediss://`).
- **Observability**: structured JSON logging with request IDs (blueprint §28).
- **Security**: baseline controls in `SECURITY.md`, aligned with OWASP ASVS Level 2.
