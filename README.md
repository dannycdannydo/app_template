# Application Starter Template

A reusable full-stack application starter: **FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL** on the backend, **Vue 3 + TypeScript + Tailwind + shadcn-vue** on the frontend, arranged as a modular monolith with WorkOS authentication, Dramatiq jobs, provider-neutral storage and a PostgreSQL row-level-security tenant-isolation backstop.

This repository is a **template**, not an application. New projects start from a tagged release of this template and then become independent repositories.

## What you get

- Modular monolith backend with typed configuration, structured logging, and a standard API error format
- **Identity and tenancy**: WorkOS login and sessions, internal users, organisations, default-deny permissions, and a separate platform-administration plane
- **Tenant isolation**: application-level scoping (validated `X-Org-Id`, membership/permission checks, foreign-row `404`) with PostgreSQL row-level security as a default-deny backstop (ADR-0022)
- **Durable jobs**: a PostgreSQL outbox and coordinator with reference-only Dramatiq messages, leases and at-least-once delivery (ADR-0004, ADR-0019)
- **Provider-neutral object storage** with signed direct uploads and worker-isolated processing (ADR-0006)
- **Notifications** with worker-delivered email (ADR-0015, ADR-0016)
- **Provider-neutral AI layer**: registry-driven tasks and prompts, adapters for OpenAI, Anthropic, DeepSeek, Azure OpenAI, Vertex AI and a local endpoint, with inline and large-file transfer modes (ADR-0017, ADR-0018)
- Vue 3 SPA with a generated, type-safe OpenAPI client and a strict TanStack Query / Pinia state split
- Alembic migrations, a single `make` command surface, and CI that runs the same quality gate on every push to `main` and on pull requests
- Governance docs (ADRs, area `AGENTS.md` guides, operations and recovery runbooks) and an honest inventory of deferred capabilities

The authoritative design standard is `Internal_Custom_Application_Starter_Architecture_v2.md`. The scoped contract and progress log for the current release is `TEMPLATE_V0_9_SCOPE.md`; `ARCHITECTURE.md` is the condensed system overview. Agents read the architecture documentation before structural changes (see `AGENTS.md`).

## Repository layout

```text
backend/                 FastAPI application (app/, alembic/, pyproject.toml)
frontend/                Vue 3 + Vite application (src/, Dockerfile, nginx.conf.template)
deploy/compose/          Compose files (compose.local.yml = local development)
deploy/caddy/            Pinned Caddy edge build (TLS, security headers, rate limits)
docs/decisions/          Architecture decision records (ADR 0001-0022)
docs/                    Operations, backup/recovery, RLS and release docs
.github/workflows/       CI pipeline
Makefile                 Command surface for development and quality gates
.env.example             Documented environment variables
```

## Prerequisites

Local development follows **ADR-0008**: PostgreSQL, Redis and MinIO run in Docker, while the API, the Dramatiq worker and the frontend run natively on the host. The host toolchain is therefore required and pinned:

- **Python 3.13** with `uv` for the backend (`uv` installs Python if needed; the version is recorded in `backend/.python-version`)
- **Node >= 24** with `pnpm` (11.x) for the frontend
- **Docker with Compose** for PostgreSQL, Redis and MinIO
- `make`

## Clean clone

```bash
# 1. Clone, then prepare the environment (single .env at the repo root)
cp .env.example .env

# 2. Install dependencies (backend lockfile via uv; frontend via pnpm)
cd backend && uv sync
cd ../frontend && pnpm install

# 3. Start PostgreSQL + Redis + MinIO, apply migrations, and run the API, the
#    Dramatiq worker and the frontend natively with live reload (this is the
#    day-to-day workflow)
cd .. && make dev

# 4. Run the full quality gate (lint + typecheck + test + client drift)
make check
```

If a port is already taken, override `BROKER_REDIS_PORT` or
`RATE_LIMIT_REDIS_PORT` in `.env`; every variable is documented in `.env.example`.
Existing clones should replace `REDIS_PORT` with both new port variables and
replace `REDIS_URL` with `BROKER_REDIS_URL` plus `RATE_LIMIT_REDIS_URL`, using
distinct ports. For example, keep a host-native Redis on 6379 and publish the
broker on 6381 by updating both `BROKER_REDIS_PORT` and `BROKER_REDIS_URL`.

The API is served at `http://localhost:8000` (docs at `/docs`), the frontend at `http://localhost:5173` (which proxies API traffic to the backend), and the MinIO admin console at `http://localhost:9001`.

For CI parity, onboarding, or Dockerfile validation the **entire stack runs in containers** instead:

```bash
make dev-docker
```

`make dev` and `make dev-docker` apply pending Alembic migrations before the API serves traffic. `make migrate` remains available for a deliberate migration-only step. The container command starts the same Postgres, Redis and MinIO plus the API, worker and frontend containers (built from `backend/Dockerfile` and `frontend/Dockerfile`). Both commands share `deploy/compose/compose.local.yml`: the default service set is infrastructure only, and the `fullstack` Compose profile adds the application containers.

Local PostgreSQL, Redis and MinIO state persists across ordinary container
restarts. Use `make dev-down` to remove the containers while preserving that
state. When the local state is disposable, run
`CONFIRM_RESET=1 make dev-reset` to erase all three stores together, recreate
the infrastructure and migrate the empty database. Resetting PostgreSQL and
Redis separately is unsupported because queued Dramatiq messages carry job ids
whose durable records live in PostgreSQL. WorkOS users are external and are
never deleted by `make dev-reset`.

Before migrations and native processes start, `make dev` pings both Redis
services through `BROKER_REDIS_URL` and `RATE_LIMIT_REDIS_URL` from the host.
This catches missing port publication and broken Docker network attachment
that a container-internal health check cannot see.

Verification: after `cp .env.example .env`, both `make dev` and `make dev-docker` must start the services and `make check` must pass with zero lint errors, zero type errors, and green tests. The complete fresh-clone and release smoke inventory (example environment, migrations, generated client, security suite, external upload, deployment Compose) is in `docs/release-checklist.md`.

## Trying the demo (login flow)

Login goes through **WorkOS** end-to-end: the browser never submits identity fields, only the session token the backend validates. To try it:

1. Set `VITE_WORKOS_CLIENT_ID` in `.env` to the client id of your WorkOS application (it is public, not a secret). In the WorkOS dashboard, register `http://localhost:5173/auth/callback` as a Redirect URI (Applications → your app → Redirects) **and** add `http://localhost:5173` to the allowed origins under Authentication → Cross-Origin Resource Sharing (CORS) → Configure CORS, in the same environment as the client id. The browser exchanges the auth code directly with `api.workos.com` (ADR 0011), so WorkOS only answers that exchange when your origin is allowlisted; without it the callback fails with a CORS error ("Access-Control-Allow-Origin missing").
2. Run `make dev` and open `http://localhost:5173`. Without a session every route redirects to `/login`; click **Continue with WorkOS** and complete the flow on the WorkOS-hosted page.
3. You land in the application shell: sidebar, user menu (identity from `GET /api/v1/me`) and organisation selector. Pick an organisation and the records example module is ready in the selected tenant.

Signing out returns to `/login`. The WorkOS integration is confined to `frontend/src/features/auth/workos.ts` (ADR 0011); the unauthenticated-redirect journeys run without any configuration, and `make e2e` runs the full authenticated journeys with a stubbed WorkOS session.

## Creating the first platform admin

When WorkOS signups are disabled, the bootstrap admin cannot self-register: the operator pre-creates the account (email + password, verified email) through the WorkOS User Management API. This template ships a small command for exactly that, run once after the app is created and before the first login:

1. Set `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` and `BOOTSTRAP_PLATFORM_ADMIN_PASSWORD` in `.env` (the password must clear the WorkOS password policy and blocklist; it is never printed or logged).
2. Run `make provision-admin` (idempotent — re-running reports the user already exists and never resets the password).
3. Sign in on the WorkOS page with that email + password; the first verified login grants `platform_admin` exactly once (`bootstrap_states` row), and the Platform Admin Centre appears in the sidebar.

`make provision-admin` needs `WORKOS_API_KEY` (already required by the backend) and works with or without `.env` via `--email`/`--password` flags: `uv --directory backend run python -m scripts.provision_bootstrap_admin --email a@b.co --password '...'`.

To tear the test admin down again (e.g. to provision a different one and re-test the bootstrap), run `make provision-admin-delete` (uses the `.env` email) or `make provision-admin-delete EMAIL=a@b.co`: it deletes the WorkOS user and the internal `users` row, which cascades to the `bootstrap_states` row and resets the one-time bootstrap. Then update `BOOTSTRAP_PLATFORM_ADMIN_EMAIL`/`PASSWORD` in `.env` and re-run `make provision-admin`.

## Command reference

| Command                          | What it does                                                                                                                                                                                                               |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `make dev`                       | Start Postgres, Redis, MinIO, Mailhog, API, Dramatiq worker, outbox coordinator, and frontend                                                                                                                              |
| `make dev-docker`                | Entire stack in containers (CI parity, onboarding)                                                                                                                                                                         |
| `make dev-infra-check`           | Verify the host-facing broker/rate-limit Redis URLs and other local infrastructure                                                                                                                                         |
| `make dev-down`                  | Remove local containers and network while preserving application data                                                                                                                                                      |
| `CONFIRM_RESET=1 make dev-reset` | Erase PostgreSQL, Redis and MinIO together, recreate infrastructure, and migrate                                                                                                                                           |
| `make migrate`                   | Run Alembic migrations                                                                                                                                                                                                     |
| `make worker`                    | Run the Dramatiq worker natively (`uv run dramatiq app.workers`)                                                                                                                                                           |
| `make coordinator`               | Run the PostgreSQL outbox coordinator natively (`uv run python -m app.job_coordinator`)                                                                                                                                    |
| `make jobs-reconcile`            | Inspect stranded queued jobs without mutating state                                                                                                                                                                        |
| `make jobs-reconcile-apply`      | Create deduplicated recovery intents (`CONFIRM_RECONCILE=1` required)                                                                                                                                                      |
| `make verify-db-roles`           | Prove the runtime/coordinator credentials are restricted, non-owner, non-`BYPASSRLS` roles                                                                                                                                 |
| `make provision-admin`           | Pre-create the bootstrap platform admin in WorkOS (email + password; idempotent)                                                                                                                                           |
| `make provision-admin-delete`    | Tear down the bootstrap admin (WorkOS + internal user, resets the one-time bootstrap); `EMAIL=...` to override                                                                                                             |
| `make recover-admin`             | Break-glass: re-grant `platform_admin` while zero enabled administrators remain (`EMAIL=... REASON=...`)                                                                                                                    |
| `make lint`                      | Ruff (backend) + ESLint/oxlint (frontend)                                                                                                                                                                                  |
| `make typecheck`                 | Pyright (backend) + vue-tsc (frontend)                                                                                                                                                                                     |
| `make test`                      | pytest (backend) + Vitest (frontend)                                                                                                                                                                                       |
| `make test-ai-contracts`         | Opt-in live AI provider contract tests (fake provider by default; each real-provider test skips until its `AI_CONTRACTS_*` credentials are configured)                                                                     |
| `make e2e`                       | Playwright journeys against the local stack (stubbed WorkOS + API; authenticated journeys need `VITE_WORKOS_CLIENT_ID` set)                                                                                                |
| `make format`                    | Ruff format + Prettier                                                                                                                                                                                                     |
| `make generate-client`           | Export OpenAPI from FastAPI and generate the TypeScript client                                                                                                                                                             |
| `make validate-ai-registries`    | Fail fast on invalid checked-in AI task/prompt/model registry definitions                                                                                                                                                  |
| `make validate-execution-contracts` | Fail fast on invalid standalone plan shape, checkpoints and reference-map coverage                                                                                                                                     |
| `make check`                     | Full local quality gate: lint + typecheck + test + registry/contract validation + generated-client drift                                                                                                                   |

## Capabilities

### Identity, tenancy and permissions

WorkOS owns login and sessions; the application owns the internal user record, organisation memberships, roles and permissions. Every protected request validates the session centrally, resolves the internal user, requires an active membership for the `X-Org-Id` header and checks an explicit permission code — default deny, with resources outside the caller's organisation surfacing as `404`. The mandatory reusable security suite (`backend/tests/test_security_suite.py`) parametrises the whole protected surface, and its completeness guard fails when a new `/api/v1` route is not added to `PROTECTED_ROUTES`. See `ARCHITECTURE.md` → "Identity flow and request context" and `SECURITY.md`.

### Platform administration

A dedicated platform-authorisation plane, separate from organisation roles, gates `/api/v1/platform/*` and the `/platform` section of the app (ADR-0013). Platform admins create and edit organisations, manage memberships and invitations through the WorkOS Invitation API, control per-organisation feature flags, manage the platform-administrator list (the final administrator cannot be revoked) and read the append-only audit history. The two planes never grant across each other, and no `is_admin`/superuser boolean exists anywhere. See `plans/PLATFORM_ADMIN_WORKFLOW_PLAN.md` and `SECURITY.md` → "Platform plane".

### Files and durable jobs

Provider-neutral object storage with signed direct uploads (ADR-0006): the browser PUTs bytes straight to MinIO/S3 through a short-lived signed URL, the backend verifies the stored object on completion, and a `process_file` worker job drives the file `pending → uploaded → processing → ready` while the UI polls job progress. Files and jobs are org-scoped and gated by `documents.*` permissions (ADR-0014). Uploaded objects are immutable: the signed PUT targets a unique staging key and only a server-side promotion writes the final key, with a pinned content identity. See `API_CONVENTIONS.md` → "Files and jobs".

### Notifications and observability

An in-app notification bell and `/notifications` page, plus worker-delivered email (ADR-0015, ADR-0016): email is always sent from Dramatiq tasks, never from an HTTP handler. Locally `make dev` starts **Mailhog** (UI at `http://localhost:8025`) to catch outbound mail; production uses the standard-library SMTP adapter. Observability completes blueprint §28: JSON logs with `request_id` (plus user/org/job context), Sentry when `SENTRY_DSN` is set, and Prometheus metrics on `GET /metrics`. See `docs/operations.md`.

### AI layer

A provider-independent AI capability (ADR-0017): a feature service calls `AIService.execute(task="document.classify", storage_reference=...)` and receives a validated, auditable result — it never imports an LLM SDK, selects a model, formats a provider request or calculates cost. The service resolves the task through checked-in task/prompt/model registries, routes to a compatible model (respecting organisation settings, budgets, regional constraints and attachment limits), dispatches through a provider adapter, and validates structured output. Providers are opt-in configuration: OpenAI, Anthropic, DeepSeek, Azure OpenAI, Google Gemini **through Vertex AI only** (ADR-0018), and a local OpenAI-compatible endpoint. Provider SDKs stay behind `app/ai/providers/` adapters, enforced by an import-boundary test.

Large files above the 5,000,000-byte inline threshold (up to 50,000,000 bytes, one PDF) use policy-driven transfer modes (`provider_upload`, `managed_signed_url`, `storage_reference`) selected by `AIService` from the source lifecycle, task, organisation policy, model capability and deployment config. Managed URLs are ephemeral just-in-time bearer capabilities that are never returned, persisted or logged. AI is default-off per organisation. See `SECURITY.md` → "AI security" and `docs/operations.md` → "AI observability".

### Database row-level security (tenant-isolation backstop)

PostgreSQL row-level security is a **default-deny backstop** for organisation isolation (ADR-0022). The ordinary API and worker path runs as a non-owner, `NOBYPASSRLS` role; tenant context is bound transaction-locally with a parameterised `set_config(..., true)` only after the membership/permission path validates it, and clears automatically on commit/rollback and pooled-connection reuse. The schema-owner, coordinator and isolated audited operator credentials are separate roles, and a startup gate refuses to serve work from a misconfigured credential. RLS supplements and never replaces the application controls. See `ARCHITECTURE.md` → "Tenant isolation and PostgreSQL row-level security", `docs/rls-table-inventory.md` and `docs/rls-rollout.md`.

### Deployment (hybrid VPS profile)

The provider-neutral production baseline (blueprint §35.1, ADR-0007): a generic Linux VPS / container-host profile in `deploy/compose/compose.hybrid-vps.yml` (Caddy edge, static Vue artifact, FastAPI, Dramatiq worker, and isolated private broker/rate-limit Redis services) with managed PostgreSQL, object storage, WorkOS, transactional email and monitoring as external services. Deployment runs through `.github/workflows/deploy-vps.yml`: it builds immutable images and a versioned frontend artifact, SSHes to a configurable host, runs exactly one deliberate `alembic upgrade head`, recreates the services and waits for `/ready`, retaining the previous release for rollback. See `.env.production.example`, `docs/operations.md`, `docs/backup-and-recovery.md` and `SECURITY.md` → "Hybrid VPS production profile".

## Releases

The template is versioned and tagged with immutable `vMAJOR.MINOR.PATCH` tags. `make check` passing is the gate for a release. Current release: **v0.9.0** (PostgreSQL row-level security tenant-isolation backstop). See `TEMPLATE_V0_9_SCOPE.md` §6 for the progress log, `CONTRIBUTING.md` → "Versioning and releases" for the version convention, and `docs/upgrades/0.8-to-0.9.md` for the upgrade guide. The released version is recorded in `backend/pyproject.toml` (`[project].version` and `[tool.project-template].version`) and `frontend/package.json`.

Development follows the branch workflow in `CONTRIBUTING.md`: work units live on `feature/*` branches and reach `main` only through reviewed pull requests, so CI runs once per merged unit rather than on every push.

## Known limitations and deferred features

The starter states its boundaries honestly. Notable deferred or unsupported capabilities: no malware-scanner provider is selected (`FILE_SCAN_PROVIDER=none`; the quarantine state, `ContentScanner` seam and deny-by-default gate ship, and production requires `FILE_SCAN_ACKNOWLEDGE_UNSCANNED=true`); external effects are at-least-once, never exactly-once, with ambiguous outcomes durably attention-required rather than auto-resent; Azure OpenAI, DeepSeek and local AI providers fail closed for non-inline large files; the optional synchronous `document.ask` mode remains bounded by `AI_ASK_MAX_SYNCHRONOUS_BYTES` while larger questions use its durable job path; and decompression-bomb/document-processing limits are post-v1. The full inventory and the fresh-clone release smoke steps are in `docs/release-checklist.md`.
