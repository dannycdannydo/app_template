# Template v0.6 — Scope & Progress Log

## Relationship to other documents

- `Internal_Custom_Application_Starter_Architecture_v2.md` is the long-term **design standard**; for this release the governing sections are **BP §20** (Notifications and Email, lines 1130–1195), **BP §28** (Observability, lines 1472–1513), **BP §35.1** (Hybrid VPS Profile, lines 1844–1914), **BP §38** (Environment Separation, lines 2046–2067) and **BP §39** (Backup and Recovery, lines 2070–2087).
- `IMPLEMENTATION_GUIDE.md` is the **build plan** and the incremental release sequence. v0.6 in this guide is *Operations*; it is the final planned release — the guide states that at this point "the template is ready for the first real client application" (guide §4, and §8's Definition of the First Usable Template: "deploy through the hybrid VPS profile"). The guide names no separate design source for v0.6; ADR-0007 (two deployment profiles) carries the generic VPS/container-host deployment decision this release implements, and the release adds ADR-0015 (email provider interface) and ADR-0016 (basic notifications). The existing "hybrid VPS" name in the blueprint is treated as the portable Linux-host profile, not an AWS-only target.
- This file is the **scoped contract for the v0.6 release**. It defines exact deliverables, exclusions, acceptance tests, and the commands that must work. It also serves as a progress log: check items off as they are completed.

---

# 1. Goal of v0.6

An **operable, provider-neutral template**: complete observability (structured JSON logging context, Sentry, basic metrics), a provider-neutral email interface with one SMTP adapter and local Mailhog, basic notifications delivered through the durable job pipeline, a documented generic Linux VPS/container-host production profile with edge rate limiting, and tested backup-and-recovery procedures. After v0.6 a fresh clone can send a test notification, surface errors in Sentry, deploy the same OCI images to any compatible VPS or container host, and restore from documented recovery procedures — closing every remaining item on guide §8's first-usable-template list.

---

# 2. In Scope

```text
structured JSON logging
Sentry
email provider interface
one email provider
basic notifications
hybrid VPS deployment
backup and recovery documentation
```

The v0.1–v0.5 foundation already ships structlog with request IDs, `/health` and `/ready` (BP §28), the audit service, the Dramatiq worker with durable job records, MinIO-backed storage, the permission catalogue, the generated-client pipeline, and the Vue shell. v0.6 completes the operations story on that foundation; it is not a greenfield build.

Explicit deliverables:

- **Observability completion (BP §28)**: structured JSON logging already ships; v0.6 completes the §28 logging-context field set — `user_id` and `organisation_id` on authenticated requests, `job_id` and `resource_id` in worker logs, a consistent `event` name (with `request_id` and `route` re-verified) — and adds `GET /metrics` (basic Prometheus metrics: request counters/histograms, job counters). `/health` and `/ready` stay unchanged and public. Worker failure visibility comes from Sentry capture plus the existing durable job failure records.
- **Sentry**: `sentry-sdk` dependency; `SENTRY_DSN`, `SENTRY_ENVIRONMENT` (defaults to `APP_ENV`) and `SENTRY_TRACES_SAMPLE_RATE` settings; initialised in `create_app()` when the DSN is set (FastAPI integration); unhandled request exceptions and unhandled worker exceptions captured. The DSN is optional — production boots without it, and the deploy docs recommend setting it.
- **Email provider interface** (`app/email/`): a provider-neutral `EmailProvider` interface (`send_email(from_address, to_address, subject, text_body, html_body) -> EmailDeliveryResult(provider_message_id, status)`) plus a `FakeEmailProvider` in-memory adapter used by the pytest suite. No module outside `app/email/` imports a provider SDK; application code depends on the interface only (parallel to ADR-0006's storage contract).
- **One email provider**: an `SmtpEmailProvider` over the standard library `smtplib` (no new runtime dependency), selected by `EMAIL_PROVIDER=smtp`; it works against Mailhog locally and against any transactional provider's SMTP relay in production (Postmark, SES, SendGrid and Resend all expose SMTP). Azure Graph / direct Resend / SendGrid SDK adapters are explicitly deferred (rule of three). Application email is always sent through the Dramatiq worker — never in an HTTP handler (BP §20, ADR-0004).
- **Mailhog for local development**: a `mailhog` service joins `deploy/compose/compose.local.yml` (infra set, so `make dev` starts it and the fullstack profile) with the SMTP port 1025 and the web UI on 8025.
- **Basic notifications (BP §20)**: `notifications` table (id, organisation_id, user_id, type, title, body, resource_type, resource_id, read_at, created_at) and `notification_deliveries` table (notification_id, channel, recipient, status, provider_message_id, attempt_count, sent_at, plus id per §10 conventions) via Alembic migrations; an org-scoped `modules/notifications/` module following the existing module pattern (models / queries / service / schemas / router / tasks).
- **Notification permission codes**: `notifications.read` and `notifications.manage` added to the permission catalogue (`backend/app/modules/permissions/constants.py`) with a data migration updating `ROLE_PERMISSION_MAP` (owner/administrator/manager: both; member: `notifications.read`; viewer: none — default-deny). This is a permission-model change and is human-reviewed (BP §33, AGENTS.md).
- **Notifications API**: `GET /api/v1/notifications` (own notifications in the caller's organisation, paginated, standard envelope carrying `unread_count` for the bell, type filter), `GET /api/v1/notifications/unread-count`, `PATCH /api/v1/notifications/{notification_id}/read` (sets `read_at`; foreign notification id → 404), and `POST /api/v1/notifications/test` (gated `notifications.manage`; creates an in-app notification for the caller and enqueues an email delivery — the demonstrable "send a test notification" of BP §45). All routes are org-scoped (X-Org-Id) and join `PROTECTED_ROUTES`.
- **Notification production loop**: email deliveries run as durable jobs (`job_type="notification.email"`, `input_reference` = the delivery id) through the v0.5 job service; the task marks the delivery queued → running → succeeded/failed, records `provider_message_id`, is idempotent on retry (status/attempt checks before sending), and audits failures. The v0.5 `process_file` job is extended so a completed file (ready or failed) creates an in-app notification for the uploader (`file.ready` / `file.failed`) and enqueues the email delivery — the files ↔ jobs ↔ notifications loop the release exists to demonstrate.
- **Notifications frontend**: a notification bell in the `AppShellLayout` header (unread badge from the unread-count query, recent notifications, mark-read) and a `/notifications` route (`name: 'notifications'`, `meta.requiresAuth`, sidebar entry) with a list view (read/unread state, mark-read, test-send action for `notifications.manage` holders), built on the existing `DataTable`/dropdown/toast building blocks, query composables in `src/queries/notifications.ts`; generated-client refresh.
- **Provider-neutral VPS/container-host deployment profile (BP §35.1, ADR-0007)**: `deploy/compose/compose.hybrid-vps.yml` (Caddy, Vue static frontend, FastAPI, Dramatiq worker, Redis — Redis not published; Postgres/object storage/WorkOS/email/monitoring external), a `deploy/caddy/Caddyfile` (automatic TLS, `/api` reverse-proxied to the API, static assets served by Caddy, security headers, and a documented edge-rate-limiting implementation), a provider-neutral `deploy-vps.yml` workflow following BP §37's flow (CI → build immutable OCI images → push to a configurable registry → build and publish a versioned Vue artifact → SSH to a Linux host → pull/install → run one deliberate migration → restart → `/ready` health check), and a `.env.production.example` documenting production environment separation (BP §38: local/staging/production, staging never uses production data or credentials). The profile must work on any Linux VM or container host with Docker Engine/Compose, SSH, DNS and ports 80/443; AWS, Azure, Hetzner, DigitalOcean, OVH, private OpenStack and similar providers are deployment targets, not application-code variants. Provider-specific adapters (ECS/Fargate, Azure Container Apps, Kubernetes, etc.) remain optional additions under `deploy/<provider>/` and must consume the same images and configuration contract.
- **Backup and recovery documentation (BP §39)**: `docs/backup-and-recovery.md` covering database restore, object-storage recovery, secret recovery, deployment rollback, lost VPS replacement, and environment recreation, each with concrete commands, documented RPO/RTO targets, backup-frequency expectations, and Redis recovery semantics; the database restore and lost-host/environment recreation procedures are executed against scratch infrastructure and the runs are recorded (BP §39: "Backups are not considered valid until restore procedures have been tested").
- New settings documented in `.env.example`: `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`, `EMAIL_PROVIDER`, `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`. Production fail-fast: `EMAIL_PROVIDER` must not be `fake` when `APP_ENV=production`, and SMTP host/port/from are required.

---

# 3. Out of Scope (Explicitly Deferred)

These are **not** part of v0.6. They appear in later releases per `IMPLEMENTATION_GUIDE.md` or are deferred by the rule of three.

| Capability | Deferred to |
| --- | --- |
| Managed Azure reference deployment (`deploy/managed/`) | post-v1 (guide §5: "managed Azure reference infrastructure"; BP §35.2 — ADR-0007 keeps the shared-image contract for when it is added) |
| Additional email adapters (Resend HTTP, AWS SES, SendGrid, Postmark, Microsoft Graph) beyond the SMTP adapter | post-v1 (BP §20 adapter list; the interface contract ships in v0.6) |
| Advanced notification preferences (per-channel opt-out, digest settings, priority) | post-v1 (guide §5) |
| Real-time notification delivery / Server-Sent Events | post-v1 (BP §20: "not required by default"; guide §5: SSE) |
| Transactional outbox | post-v1 (guide §5) |
| Generic import-mapping UI, generic export framework | post-v1 (guide §5) |
| General (application-level) database-backed feature-flag framework | post-v1 (guide §5; only platform-controlled organisation flags ship in v0.4) |
| pgvector and PostGIS setup, Server-Side Rendering, i18n | post-v1 (guide §5) |
| Malware scanning, decompression-bomb/page-limit protections (the quarantine/failed states and scanning hook seam ship in v0.5) | post-v1 (BP §17 security, BP §30 file security, SECURITY.md) |
| Server-side document processing beyond verify-and-mark-ready | post-v1 |
| Built-in uptime-monitoring vendor integration (UptimeRobot etc.) | post-v1 (v0.6 requires documented external uptime checks and alert configuration, but does not bundle a vendor) |
| Teams (`teams`, `team_memberships`) and team-specific notification targeting | post-v1 (BP §9 adds teams only when required) |
| Org-level invitations / gating the unprivileged `POST /api/v1/organisations` | post-v1 (carried from v0.4/v0.5; breaking change, human review required) |
| Self-service registration / public signup flows | post-v1 |
| Advanced data grids (AG Grid, Handsontable), server-side rendering, multi-language UI / i18n | post-v1 |
| Template email design system (branded templates, HTML template registry) | post-v1 (v0.6 ships plain text + minimal HTML bodies) |

## 3.1 Portability contract

The generic VPS profile is the portable production baseline. It must not require
AWS, Azure, a particular VPS vendor, or a particular image registry. The
deployment contract requires only a Linux host/container runtime, Docker Engine
and Compose (or an equivalent OCI runtime), SSH access, DNS, ports 80/443, and
the configured external service URLs. Registry, host, release directory,
domain, and environment-file locations are deployment inputs, not hard-coded
application assumptions.

The API is stateless across containers. PostgreSQL is the source of truth,
Redis is the broker/rate-limit service, and object storage is addressed through
the existing provider-neutral storage interface. A provider-specific deployment
adapter may translate this contract to ECS/Fargate, Azure Container Apps,
Kubernetes, or another managed service, but must use the same backend image,
worker command, settings contract, health endpoints, and migration procedure.

---

# 4. Commands That Must Work

All v0.1–v0.5 commands remain part of the quality gate. `make dev` now also starts Mailhog (infra) and the Dramatiq worker (native, ADR-0008); the `dev-docker` fullstack profile gains the Mailhog service. The Alembic migration pipeline (`make migrate`) covers the new `notifications`/`notification_deliveries` tables and the permission data migration. `docker compose -f deploy/compose/compose.hybrid-vps.yml config` validates the generic VPS profile; deployment itself runs through the provider-neutral `deploy-vps.yml` workflow.

```bash
make dev              # Postgres + Redis + MinIO + Mailhog in Docker; API + frontend + Dramatiq worker native with live reload (ADR-0008)
make dev-docker       # entire stack in containers (CI parity, onboarding, Dockerfile validation) incl. worker + MinIO + Mailhog
make worker           # run the Dramatiq worker natively (uv run dramatiq app.workers)
make migrate          # run Alembic migrations
make lint             # Ruff (backend) + ESLint/oxlint (frontend)
make typecheck        # Pyright (backend) + vue-tsc (frontend)
make test             # pytest (backend) + Vitest (frontend)
make format           # Ruff format + Prettier
make generate-client  # export OpenAPI from FastAPI -> generate TS client (openapi-typescript)
make e2e              # Playwright journeys against the local stack
make check            # full local quality gate (lint + typecheck + test + drift)
```

`make dev` for v0.6 requires the existing WorkOS and `STORAGE_*` variables and, for email, the `EMAIL_*`/`SMTP_*` variables (`.env.example` provides the Mailhog-friendly defaults: `smtp` provider, `localhost:1025`, no auth, `EMAIL_FROM` set). Production fail-fast validation requires explicit SMTP configuration when `EMAIL_PROVIDER=smtp` in the production environment and rejects `fake`. The generic VPS Compose profile is validated by `docker compose config` in CI and in `make check` is exercised only through the existing container-build job.

---

# 5. Acceptance Criteria

v0.6 is done when **all** of the following are true:

1. **Logging context and metrics (BP §28)**: every JSON log line carries `request_id`; authenticated `/api/v1` requests additionally bind `user_id` and `organisation_id` (and the worker binds `job_id`) — proven by tests that assert the context fields in captured log output; `GET /metrics` returns Prometheus text format with request and job counters; `/health` and `/ready` behave exactly as in v0.5; the "never log" list from BP §28 (passwords, tokens, authorisation headers, signed URLs, full connection strings) is covered by tests.
2. **Sentry**: with `SENTRY_DSN` set, `create_app()` initialises sentry-sdk with the app name and `SENTRY_ENVIRONMENT`; an unhandled 500 and an unhandled worker exception each trigger `capture_exception` (proven with the SDK mocked); with no DSN the app boots without Sentry and nothing is captured; `make check` passes in both configurations.
3. **Email provider interface (BP §20)**: `app/email/` exposes one `EmailProvider` interface and at least two implementations (`SmtpEmailProvider`, `FakeEmailProvider`) selected by `EMAIL_PROVIDER`; the default pytest suite runs fully with the fake adapter (no Mailhog required for `make check`); with the `.env.example` defaults, `make dev` starts Mailhog and a test email round-trips through it end to end (SMTP adapter integration test behind an `email_integration` marker, Mailhog via compose or CI service); production rejects `EMAIL_PROVIDER=fake`; email is sent only from worker tasks, never inside an HTTP handler (proven by test).
4. **Notification records and permissions (BP §20)**: the `notifications` and `notification_deliveries` tables exist via Alembic migration with the §20 shape plus §10 conventions (UUIDv7 ids, timestamps, naming); the `notifications.read` / `notifications.manage` permission codes exist in the catalogue with the role-bundle data migration applied (owner/administrator/manager: both; member: read; viewer: none); the permission-model change was human-reviewed.
5. **Notifications API and delivery loop**: `GET /api/v1/notifications` returns only the caller's own notifications in the caller's organisation (paginated envelope with `unread_count`), `GET /api/v1/notifications/unread-count` returns the count, `PATCH /api/v1/notifications/{notification_id}/read` marks read (foreign or other-user notification → 404), and `POST /api/v1/notifications/test` creates an in-app notification and enqueues an email delivery job; every email delivery runs as a durable job whose delivery row goes queued → running → succeeded/failed with `provider_message_id` recorded, retries are idempotent (no double-send), and a completed `process_file` produces a `file.ready`/`file.failed` notification for the uploader with its email delivery enqueued (all proven by integration tests).
6. **Notifications frontend**: the `AppShellLayout` header shows a notification bell with an unread badge fed by the unread-count query; `/notifications` is a `requiresAuth` route with a sidebar entry listing the caller's notifications with mark-read (and test-send for `notifications.manage` holders); Vitest covers the `notifications` composables, the bell and the view; a Playwright journey covers the mocked list-and-mark-read flow; `make generate-client` produces no diff.
7. **Provider-neutral VPS deployment (BP §35.1)**: `docker compose -f deploy/compose/compose.hybrid-vps.yml config` validates on a clean checkout; the profile runs Caddy, the static Vue artifact, one or more API containers, one or more Dramatiq worker containers, and private Redis. API and worker services have health checks, restart policies, graceful shutdown settings, explicit CPU/memory limits, and documented initial replica/concurrency defaults. Caddy terminates TLS, serves the versioned Vue artifact, reverse-proxies `/api`, applies security headers, and uses a pinned, tested edge-rate-limiting implementation (standard Caddy plus an explicitly pinned custom module, an external WAF, or a documented application-level fallback — an unqualified `rate_limit` directive is not acceptable). The `deploy-vps.yml` workflow accepts configurable registry/host/release-path inputs, publishes immutable backend and frontend artifacts, verifies the image/artifact commit, obtains a deployment lock, runs exactly one deliberate `alembic upgrade head`, performs a rolling/recreate restart, waits for `/ready` with a timeout, and retains the previous release for rollback. `.env.production.example` documents every production variable and staging/production separation (BP §38); SECURITY.md records firewall, SSH keys only, non-public Redis, automatic security updates, monitoring/alerting, disk alerts, resource limits, rollback, and off-site configuration backups. The same contract is documented as portable to any Linux VPS/container host; provider-specific ECS/Fargate, Azure, Kubernetes, or other adapters are optional and consume the same images and settings.
8. **Operations and monitoring**: the deployment documentation defines API/worker scaling procedures, trusted proxy/client-IP handling, container log rotation and retention, external uptime checks for `/health` or `/ready`, metrics scraping and retention, alerts for readiness/API failures, worker/job failures, disk pressure, certificate expiry and backup failures, plus Redis authentication, persistence, memory limits, eviction policy, and the consequences of Redis loss.
9. **Backup and recovery documentation (BP §39)**: `docs/backup-and-recovery.md` covers all six procedures (database restore, object-storage recovery, secret recovery, deployment rollback, lost VPS replacement, environment recreation) each with concrete commands; it states RPO/RTO and backup-frequency expectations, PostgreSQL PITR requirements, object-storage versioning/replication requirements, secret source-of-truth/recovery, Redis recovery semantics, DNS/TLS recovery, and the external-service dependency model; database restore and lost-host/environment recreation procedures are executed against scratch infrastructure and the runs recorded in the doc.
10. **Protected-surface completeness**: every new `/api/v1` route (`/api/v1/notifications`, `/api/v1/notifications/unread-count`, `/api/v1/notifications/{notification_id}/read`, `/api/v1/notifications/test`) is present in `PROTECTED_ROUTES` (`backend/tests/test_security_suite.py`) with the unauthenticated → 401, invalid-session → 401, disabled-user → 403, viewer-write → 403, cross-organisation → 404 and stack-trace non-exposure cases; the completeness guard test stays green.
11. **Governance and audit**: `make check` passes from a clean checkout with zero lint errors, zero type errors, green tests and a diff-free generated client; `make e2e` passes; CI gains an SMTP/Mailhog-backed `email-integration` job and validates the generic VPS compose config; new dependencies (`sentry-sdk`, `prometheus-client`, and any Caddy rate-limit module) are documented with justification and pinned; the permission-model change (notifications codes), infrastructure changes (Mailhog, VPS compose, deploy workflow, edge limiter) and secret handling (SMTP credentials, Sentry DSN) were human-reviewed per BP §33; `.env.example` documents the `SENTRY_*`/`EMAIL_*`/`SMTP_*` settings; `ARCHITECTURE.md`, `API_CONVENTIONS.md`, `SECURITY.md` (edge rate limiting, VPS protections, email/notification security) and `README.md` (portable deployment, scaling, monitoring, backup) are updated; ADR-0015 and ADR-0016 are recorded; the architecture audit (`prompts/04-architecture-audit.md`) reports no CRITICAL or MAJOR findings.

---

# 6. Progress Log

Check items off as they are completed. Keep one task `in_progress` at a time where practical.

Subsections are ordered so later work builds on earlier work: observability precedes everything (errors surface during the rest of the release), the email interface precedes the notifications that deliver through it, the notifications data model precedes the production loop that unifies files/jobs/notifications, the frontend closes the feature, deployment and backup docs make the release operable, and governance closes the release. Dependencies are noted per subsection.

## 6.1 Observability Foundation: Structured Logging, Sentry and Metrics

Depends on the v0.5 foundation (structlog, request-id middleware, durable job records). The complete BP §28 picture. Structured JSON logging and `/health`/`/ready` already ship; this subsection closes the remaining gaps.

- [x] Logging context completion (BP §28, all seven fields): `request_id` (ships in v0.1) and `route` (already logged as path on `request_finished`) re-verified; bind the missing fields — `user_id` and `organisation_id` on authenticated `/api/v1` requests (dependencies or middleware, cleared per request), `job_id` and `resource_id` inside worker tasks for file/job/notification operations, and a consistent `event` name on every log line; assert the context fields in captured log lines via tests; keep the BP §28 never-log list enforced by test
- [x] `GET /metrics` (BP §28 basic metrics): `prometheus-client` dependency; request counter + latency histogram middleware plus job counters (enqueued/succeeded/failed); `/metrics` public like `/health`/`/ready`; test asserts Prometheus text format output
- [x] Sentry: `sentry-sdk` dependency; `SENTRY_DSN`, `SENTRY_ENVIRONMENT` (default `APP_ENV`), `SENTRY_TRACES_SAMPLE_RATE` settings in `core/config.py`; `create_app()` initialises the SDK when the DSN is set (FastAPI integration); worker failure capture (Dramatiq middleware mirroring the durable job failure record); tests with the SDK mocked (capture on unhandled 500 and worker exception, no-op without DSN)
- [x] `.env.example` documents `SENTRY_*`; worker logging context verified under `make dev` (job_id present in worker log lines)

## 6.2 Email Provider Interface and SMTP Adapter

Depends on §6.1 (logging context for the worker that sends mail) and the v0.5 job foundation. The provider-neutral email seam (parallel to ADR-0006).

- [x] `app/email/` package: `EmailProvider` interface — `send_email(from_address, to_address, subject, text_body, html_body) -> EmailDeliveryResult(provider_message_id, status)` — plus `FakeEmailProvider` (in-memory, test-only) and `SmtpEmailProvider` (standard library `smtplib`, no new runtime dependency); `get_email_provider()` factory wired from settings (lru_cache singleton like `get_storage`)
- [x] Settings in `core/config.py`: `email_provider` (`smtp` default / `fake`), `email_from`, `smtp_host`, `smtp_port`, `smtp_username`, `smtp_password`, `smtp_use_tls`; production fail-fast (no `fake` provider, SMTP host/port/from required); pytest conftest pins `email_provider=fake`
- [x] `mailhog` service in `deploy/compose/compose.local.yml` (infra set, so `make dev` starts it) + fullstack profile; SMTP 1025, UI 8025; `EMAIL_*`/`SMTP_*` dev defaults documented in `.env.example`; `make dev` message mentions the Mailhog UI
- [x] Tests: interface contract against `FakeEmailProvider` (round-trip, provider_message_id, failure path); SMTP adapter against Mailhog behind an `email_integration` marker (like `storage_integration`), excluded from the default suite; proof that email is only ever sent from worker tasks

## 6.3 Notifications Records, Permission Codes and API

Depends on §6.2 (email delivery) and the v0.4 audit service. The org-scoped notifications module.

- [ ] `notifications` and `notification_deliveries` tables (BP §20 shape + §10 conventions: UUIDv7 ids, timestamps, naming, indexes on organisation/user/read_at) via Alembic migration; `modules/notifications/` models/queries/service/schemas registered in `db/base.py` and `main.py`
- [ ] Permission catalogue change (human review required, BP §33): `notifications.read` + `notifications.manage` codes in `constants.py` with role-bundle data migration (owner/administrator/manager: both; member: read; viewer: none); default-deny unchanged
- [ ] `GET /api/v1/notifications` (notifications.read; own notifications in the caller's org only, paginated, standard envelope with `unread_count`, type filter), `GET /api/v1/notifications/unread-count` (notifications.read); explicit response schemas on every endpoint
- [ ] `PATCH /api/v1/notifications/{notification_id}/read` (notifications.read; sets `read_at`; foreign/other-user id → 404), `POST /api/v1/notifications/test` (notifications.manage; creates an in-app notification for the caller + enqueues the email delivery job); audit events on test-send and on delivery failure
- [ ] Security suite: all four notifications routes in `PROTECTED_ROUTES` (org_scoped=True) with the full matrix (unauth 401, invalid session 401, disabled 403, viewer-write 403, cross-org 404, no stack traces)

## 6.4 Notification Production Loop

Depends on §6.3 (API) and the v0.5 job foundation. The capability that makes notifications demonstrably deliver.

- [ ] `send_notification_email` Dramatiq task (`job_type="notification.email"`, `input_reference` = delivery id): update delivery queued → running → succeeded/failed, record `provider_message_id`, set `attempt_count`; idempotent on retry (status/attempt check before sending); failure → delivery `failed` + audit
- [ ] Extend the v0.5 `process_file` task: on completion (ready or failed) create an in-app notification for the uploader (`file.ready` / `file.failed`, resource_type `file`, resource_id = file id) and enqueue the email delivery job
- [ ] Integration test: file → ready → notification row → email delivery job → provider_message_id recorded (fake provider in unit tests, real broker + Redis in CI); delivery-failure path test (delivery failed, job failed with error_code, audit written); no double-send on retry

## 6.5 Notifications Frontend

Depends on §6.3 and §6.4 (the full notifications API surface). The bell and notifications view.

- [ ] `make generate-client` regenerates types for the notifications endpoints; drift gate stays in `make check`
- [ ] `src/queries/notifications.ts` composables keyed `['organisations', orgId, 'notifications', ...]` (list, unread-count with `refetchInterval`, mark-read, test-send; invalidation after mark-read/test-send); no component/store imports `src/api/client.ts` directly
- [ ] `NotificationBell` in the `AppShellLayout` header (unread badge, recent notifications dropdown, mark-read action); router: `/notifications` route (`name: 'notifications'`, `meta.requiresAuth`) + `SidebarNav` entry; `NotificationsListView` with the existing `DataTable`/dropdown/toast building blocks (read/unread state, mark-read, test-send for `notifications.manage` holders)
- [ ] Vitest: notifications composables, bell, notifications view; Playwright journey: mocked list-and-mark-read flow per existing e2e pattern

## 6.6 Provider-Neutral VPS/Container-Host Deployment Profile

Depends on §6.1 (Sentry/health readiness for operations) and the v0.5 backend image; independent of §6.2–§6.5. The BP §35.1 profile and the edge rate limiting SECURITY.md assigns to v0.6.

- [ ] `deploy/compose/compose.hybrid-vps.yml` (canonical generic VPS profile: Caddy, versioned Vue static artifact, FastAPI, Dramatiq worker, Redis — Redis not published; API/worker share the backend image, per BP §36); API and worker health checks, restart policies, graceful shutdown, resource limits, log rotation, and documented replica/concurrency defaults; validated by `docker compose config` in CI
- [ ] `deploy/caddy/Caddyfile`: automatic TLS for the configured domain, versioned static Vue assets, `/api` reverse-proxied to the API, security headers, and a pinned/tested edge-rate-limiting implementation (custom Caddy module, external WAF, or documented application-level fallback)
- [ ] `.github/workflows/deploy-vps.yml` (workflow_dispatch + tag): build immutable backend/frontend artifacts → push to a configurable registry/release store → SSH to a configurable Linux host → verify commit/artifact identity → obtain a deployment lock → run exactly one `alembic upgrade head` (deliberate release step, BP §37) → rolling/recreate restart → wait for `/ready` with timeout → retain previous release for rollback; no provider-specific application code
- [ ] `.env.production.example`: every production variable (registry, release path, host, WorkOS, storage, email/SMTP, Sentry, CORS/TRUSTED_HOSTS for the production domain) + BP §38 environment-separation notes (staging never uses production data or credentials); SECURITY.md gains the §35.1 mandatory-protections section, scaling procedure, trusted-proxy guidance, and edge-rate-limiting control
- [ ] Operations runbook: scale API replicas and worker concurrency, configure external uptime checks and metrics scraping, alert on readiness/API/worker/disk/certificate/backup failures, and document Redis authentication, persistence, memory/eviction policy, graceful shutdown, and Redis-loss consequences

## 6.7 Backup and Recovery Documentation

Depends on §6.6 (the deployment profile is what the procedures recover). Closes BP §39.

- [ ] `docs/backup-and-recovery.md`: six procedures with concrete commands — database restore (managed Postgres), object-storage recovery, secret recovery, deployment rollback, lost VPS replacement, environment recreation; RPO/RTO, backup frequency, PostgreSQL PITR, object-storage versioning/replication, secret source-of-truth, Redis recovery semantics, DNS/TLS recovery, and the external-service dependency model stated
- [ ] Database-restore and lost-host/environment-recreation procedures executed against scratch infrastructure and the runs recorded in the doc (BP §39 validity rule); README links the doc from the deployment section

## 6.8 Docs, ADR & Release Governance

Depends on §6.5 and §6.7 (exercises the release). Closes v0.6.

- [ ] ADR-0015 (provider-neutral email interface, SMTP as first adapter, Mailhog locally) and ADR-0016 (basic notifications: durable delivery jobs, delivery tracking, `notifications.*` permission codes and role bundles); new dependencies (`sentry-sdk`, `prometheus-client`, and any edge-rate-limit module) documented with justification and pinned
- [ ] `ARCHITECTURE.md` (observability, email interface, notifications flow), `API_CONVENTIONS.md`, `SECURITY.md` (edge rate limiting, portable VPS protections, email/notification security) and `README.md` (provider-neutral deployment, scaling, monitoring, backup, Mailhog) updated; `.env.example` documents the `SENTRY_*`/`EMAIL_*`/`SMTP_*` settings
- [ ] CI changes landed and green: `email-integration` job (Mailhog service), generic VPS Compose and Caddy/artifact validation; `make check` green from a clean checkout; generated-client drift clean; Playwright job green including the notifications journey
- [ ] Human review recorded for the permission-model change (notifications codes), infrastructure changes (Mailhog, generic VPS Compose, deploy workflow, edge limiter), secret handling (SMTP credentials, Sentry DSN) and major dependency additions per BP §33; architecture audit clean (no CRITICAL/MAJOR)

---

# 7. Blueprint Reference Map

Each scope subsection (left column) maps to specific sections of `Internal_Custom_Application_Starter_Architecture_v2.md`. Implementers and reviewers read **only** the listed sections for a given task — not the whole blueprint. This keeps context lean and focused.

## Disambiguating section numbers

Two documents use the `§` symbol. Do not confuse them:

- **Scope §6.x** — a subsection of *this file's* checklist (e.g. Scope §6.3 = "Notifications Records, Permission Codes and API").
- **BP §N** — a section of the *blueprint* (e.g. BP §20 = "Notifications and Email", starting at line 1130).

The map below always uses `BP §N` for blueprint sections and `Scope §6.x` for this file's checklist. **Never** write a bare `§6` or `§20` — always prefix with `Scope` or `BP` so the next reader knows which document you mean.

## Map

Line ranges were verified against the blueprint's table of contents and by reading each section's start and end (the range ends at the last content line before the next `#` heading). The v0.5 scope's ranges are reused only where the release maps the same section; every range below was re-confirmed against the current file.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Observability Foundation | **BP §28** (lines 1472–1513), **BP §5** (lines 156–215), **BP §12** (lines 597–667), **BP §13** (lines 669–717) | Observability tool list (structured logging, request IDs, Sentry, health/readiness, basic metrics, worker failure visibility), the logging-context field set and the never-log list, project structure for `core/logging.py` and the metrics endpoint placement, API conventions for the public `/metrics` route, error-handling rules the Sentry capture must preserve (safe generic messages, no stack-trace exposure) |
| **Scope §6.2** Email Provider Interface + SMTP | **BP §20** (lines 1130–1195), **BP §10** (lines 496–575), **BP §36** (lines 1952–2001), **BP §32** (lines 1707–1757) | Provider-neutral email interface, adapter list, "email sent through background jobs", notification/delivery table shapes (referenced, built in §6.3), database conventions for settings/columns, compose conventions for the Mailhog service (ADR-0008 native model), dependency rules (stdlib `smtplib`, no new email runtime dependency) |
| **Scope §6.3** Notifications Records, Permission Codes and API | **BP §20** (lines 1130–1195), **BP §9** (lines 385–494), **BP §10** (lines 496–575), **BP §11** (lines 577–595), **BP §12** (lines 597–667), **BP §13** (lines 669–717), **BP §29** (lines 1515–1561) | `notifications` and `notification_deliveries` table shapes, organisation as isolation boundary and the role-bundle model the new `notifications.*` codes extend (default-deny), database conventions for the new tables (UUIDv7 ids, timestamps, naming, indexes), service-owned transaction boundaries, pagination/filtering and envelope conventions, structured error envelope, audit examples the notification events follow |
| **Scope §6.4** Notification Production Loop | **BP §18** (lines 975–1075), **BP §20** (lines 1130–1195), **BP §17** (lines 851–974), **BP §29** (lines 1515–1561) | Durable job table shape, statuses and rules (idempotency, bounded retries, task-writing conventions) the email-delivery job reuses, delivery-tracking rules (idempotent deliveries, provider_message_id), the file-processing step that now emits notifications, audit events for delivery failures |
| **Scope §6.5** Notifications Frontend | **BP §14** (lines 719–774), **BP §15** (lines 776–810), **BP §16** (lines 812–849), **BP §12** (lines 597–667) | Frontend folder structure and state boundaries (server state in queries, client state in Pinia), generated-client rules (never hand-write duplicates, drift in CI), design-system rules (reusable application components above primitives, bell in the shell layout), pagination conventions for the notifications table |
| **Scope §6.6** Provider-Neutral VPS/Container-Host Deployment Profile | **BP §35** (lines 1838–1950, §35.1 at 1844–1914), **BP §36** (lines 1952–2001), **BP §37** (lines 2003–2044), **BP §38** (lines 2046–2067), **BP §30** (lines 1563–1637) | Generic Linux host service list, mandatory protections (firewall, SSH keys only, non-public Redis, monitoring, disk alerts, rollback, off-site config backups), compose-file layout and one-image-two-commands, process supervision/scaling, versioned frontend artifact, provider-neutral deployment flow (CI → immutable artifacts → SSH → one migration → restart → health check), environment separation (local/staging/production), and a pinned/tested edge-rate-limiting option |
| **Scope §6.7** Backup and Recovery Documentation | **BP §39** (lines 2070–2087), **BP §35.1** (lines 1844–1914) | The six documented procedures (database restore, object-storage recovery, secret recovery, deployment rollback, lost VPS replacement, environment recreation), RPO/RTO and backup expectations, the "backups not valid until restore tested" rule, Redis/DNS/TLS recovery, and the generic profile's external-service dependency model |
| **Scope §6.8** Docs, ADR & Release Governance | **BP §31** (lines 1640–1706), **BP §32** (lines 1707–1757), **BP §33** (lines 1758–1810), **BP §34** (lines 1811–1837), **BP §37** (lines 2003–2044), **BP §42** (lines 2180–2201) | Mandatory reusable security tests the new routes join, dependency rules, coding-agent governance and the human-review list (permission-model change, infrastructure, secrets, major dependencies), ADR format, CI checks (compose config validation, Mailhog-backed email integration job, migration validity, container build), template validation (fresh clone, migrations, client drift) |

If a task touches a concern not listed here (e.g. the security baseline details for a specific control), consult the blueprint's table of contents and read only the relevant section. When in doubt, read less rather than more — this file's §2–§5 already encodes the v0.6 contract, and ADR-0007 / ADR-0015 / ADR-0016 carry the design rationale.

---

# 8. Status

```text
Release:    v0.6.0 (operations)
State:      in progress
Started:    —
Completed:  —
```

When every acceptance criterion in §5 is met and every box in §6 is checked, update the version recording in `pyproject.toml` and `frontend/package.json`, and tag `v0.6.0`. The guide then declares the template ready for the first real client application; validate against guide §8 (Definition of the First Usable Template) before opening the v1.0 plan.
