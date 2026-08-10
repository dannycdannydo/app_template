# Application Starter Template

A reusable full-stack application starter: **FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL** on the backend, **Vue 3 + TypeScript + Tailwind + shadcn-vue** on the frontend, arranged as a modular monolith with WorkOS authentication, Dramatiq jobs, and provider-neutral storage.

This repository is a **template**, not an application. New projects start from a tagged release of this template and then become independent repositories.

## What you get

- Modular monolith backend with typed configuration, structured logging, and a standard API error format
- Vue 3 SPA with a generated, type-safe OpenAPI client
- Alembic migrations wired to application settings
- Local development per ADR-0008: PostgreSQL, Redis and MinIO in Docker, app code native (`make dev`), with a full-container path for CI parity (`make dev-docker`)
- A Dramatiq worker (same backend image) for durable background jobs
- Provider-neutral object storage with signed uploads (ADR-0006)
- A single Makefile surface for development and quality gates
- CI that runs the same gate on every push to `main` and on pull requests
- Governance docs and architecture decision records (ADRs)

The authoritative design standard is `Internal_Custom_Application_Starter_Architecture_v2.md`. The scoped contract and progress log for the current release is `TEMPLATE_V0_5_SCOPE.md`. Agents read the architecture documentation before structural changes (see `AGENTS.md`).

## Repository layout

```text
backend/                 FastAPI application (app/, alembic/, pyproject.toml)
frontend/                Vue 3 + Vite application (src/, Dockerfile, nginx.conf)
deploy/compose/          Compose files (compose.local.yml = local development)
docs/decisions/          Architecture decision records (ADR 0001-0013)
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

If a port is already taken on your machine, override it in `.env` (e.g. `REDIS_PORT=6380` when a local Redis runs on 6379) — every variable is documented in `.env.example`.

The API is served at `http://localhost:8000` (docs at `/docs`), the frontend at `http://localhost:5173` (which proxies API traffic to the backend), and the MinIO admin console at `http://localhost:9001`.

For CI parity, onboarding, or Dockerfile validation the **entire stack runs in containers** instead:

```bash
make dev-docker
```

`make dev` and `make dev-docker` apply pending Alembic migrations before the API serves traffic. `make migrate` remains available for a deliberate migration-only step. The container command starts the same Postgres, Redis and MinIO plus the API, worker and frontend containers (built from `backend/Dockerfile` and `frontend/Dockerfile`). Both commands share `deploy/compose/compose.local.yml`: the default service set is infrastructure only, and the `fullstack` Compose profile adds the application containers.

Verification: after `cp .env.example .env`, both `make dev` and `make dev-docker` must start the services and `make check` must pass with zero lint errors, zero type errors, and green tests.

## Trying the demo (login flow)

Login goes through **WorkOS** end-to-end: the browser never submits identity fields, only the session token the backend validates. To try it:

1. Set `VITE_WORKOS_CLIENT_ID` in `.env` to the client id of your WorkOS application (it is public, not a secret). In the WorkOS dashboard, register `http://localhost:5173/auth/callback` as a Redirect URI (Applications → your app → Redirects) **and** add `http://localhost:5173` to the allowed origins under Authentication → Cross-Origin Resource Sharing (CORS) → Configure CORS, in the same environment as the client id. The browser exchanges the auth code directly with `api.workos.com` (ADR 0011), so WorkOS only answers that exchange when your origin is allowlisted; without it the callback fails with a CORS error ("Access-Control-Allow-Origin missing").
2. Run `make dev` and open `http://localhost:5173`. Without a session every route redirects to `/login`; click **Continue with WorkOS** and complete the flow on the WorkOS-hosted page.
3. You land in the application shell: sidebar, user menu (identity from `GET /api/v1/me`) and organisation selector. Pick an organisation and the records example module is ready in the selected tenant.

Signing out returns to `/login`. The WorkOS integration is confined to `frontend/src/features/auth/workos.ts` (ADR 0011); the unauthenticated-redirect journeys run without any configuration, and `make e2e` runs the full authenticated journeys with a stubbed WorkOS session (see the command reference below).

## Creating the first platform admin

When WorkOS signups are disabled, the bootstrap admin cannot self-register: the operator pre-creates the account (email + password, verified email) through the WorkOS User Management API. This template ships a small command for exactly that, run once after the app is created and before the first login:

1. Set `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` and `BOOTSTRAP_PLATFORM_ADMIN_PASSWORD` in `.env` (the password must clear the WorkOS password policy and blocklist; it is never printed or logged).
2. Run `make provision-admin` (idempotent — re-running reports the user already exists and never resets the password).
3. Sign in on the WorkOS page with that email + password; the first verified login grants `platform_admin` exactly once (`bootstrap_state` row), and the Platform Admin Centre appears in the sidebar.

`make provision-admin` needs `WORKOS_API_KEY` (already required by the backend) and works with or without `.env` via `--email`/`--password` flags: `uv --directory backend run python -m scripts.provision_bootstrap_admin --email a@b.co --password '...'`.

To tear the test admin down again (e.g. to provision a different one and re-test the bootstrap), run `make provision-admin-delete` (uses the `.env` email) or `make provision-admin-delete EMAIL=a@b.co`: it deletes the WorkOS user and the internal `users` row, which cascades to the `bootstrap_state` row and resets the one-time bootstrap. Then update `BOOTSTRAP_PLATFORM_ADMIN_EMAIL`/`PASSWORD` in `.env` and re-run `make provision-admin`.

## Command reference

| Command | What it does |
| --- | --- |
| `make dev` | Start Postgres, Redis, MinIO, Mailhog, API, Dramatiq worker, and frontend |
| `make dev-docker` | Entire stack in containers (CI parity, onboarding) |
| `make worker` | Run the Dramatiq worker natively (`uv run dramatiq app.workers`) |
| `make migrate` | Run Alembic migrations |
| `make provision-admin` | Pre-create the bootstrap platform admin in WorkOS (email + password; idempotent) |
| `make provision-admin-delete` | Tear down the bootstrap admin (WorkOS + internal user, resets the one-time bootstrap); `EMAIL=...` to override |
| `make lint` | Ruff (backend) + ESLint (frontend) |
| `make typecheck` | Pyright (backend) + vue-tsc (frontend) |
| `make test` | pytest (backend) + Vitest (frontend) |
| `make e2e` | Playwright journeys against the local stack (stubbed WorkOS + API, no backend needed; authenticated journeys need `VITE_WORKOS_CLIENT_ID` set) |
| `make format` | Ruff format + Prettier |
| `make generate-client` | Export OpenAPI from FastAPI and generate the TypeScript client |
| `make check` | Full local quality gate: lint + typecheck + test + generated-client drift |

## Files and jobs (v0.5)

v0.5 adds provider-neutral object storage with signed uploads and a durable Dramatiq job pipeline. From the `/files` page (sidebar entry, `requiresAuth`) an organisation member can:

- upload a file: the browser PUTs the bytes **directly to MinIO/S3** through a short-lived signed URL (nothing large passes through the API), the backend verifies the stored object on completion, and a `process_file` worker job drives the file `pending → uploaded → processing → ready` while the UI polls the job's progress,
- see all of the organisation's files in a table (status badge, size, uploaded-at, actions) and delete or download them (download is another short-lived signed URL).

Files and jobs are org-scoped like every other resource and gated by the existing `documents.read` / `documents.upload` / `documents.delete` permissions (ADR-0014). Everything runs with the `.env.example` defaults: MinIO at `http://localhost:9000` with the `minioadmin` dev credentials and bucket `app-files`. For the raw API, see `API_CONVENTIONS.md` → Files and jobs, or the `/docs` page.

## Notifications and observability (v0.6)

v0.6 closes the operations story (ADR-0015, ADR-0016). From the notification bell in the header (unread badge, recent notifications, mark-read) or the `/notifications` page (sidebar entry) an organisation member can see their in-app notifications; holders of `notifications.manage` (owner/administrator/manager) can send a test notification from the page. A test send creates an in-app notification and delivers an email through the Dramatiq worker — email is always sent from worker tasks, never from an HTTP handler. Locally, `make dev` starts **Mailhog** (web UI at `http://localhost:8025`), which catches every outbound message on port 1025; in production, the SMTP adapter (standard library, `EMAIL_PROVIDER=smtp`) targets any transactional provider's SMTP relay.

Observability completes blueprint §28: every JSON log line carries `request_id` (plus `user_id`/`organisation_id` on authenticated requests and `job_id`/`resource_id` in the worker), Sentry captures unhandled request and worker errors when `SENTRY_DSN` is set, and `GET /metrics` serves Prometheus metrics (request + job counters). Deployment, scaling, monitoring and alerts: `docs/operations.md`.

## Platform Admin Centre

v0.4 adds platform administration (see `PLATFORM_ADMIN_WORKFLOW_PLAN.md` and ADR-0013): a dedicated platform authorisation plane — separate from organisation roles — gates the `/platform` section of the app, which is served only to users whose `GET /api/v1/me` reports `platform_roles`. From there a platform admin can:

- create and edit organisations (each mapped 1:1 to a WorkOS Organization for invitations; the mapping is server-side only),
- view an organisation's memberships, invite users through the WorkOS Invitation API (standard onboarding: the membership is created at the invitee's first verified login), assign/remove roles, suspend/reactivate/remove memberships,
- control platform feature flags per organisation,
- manage the explicit platform-administrator membership list (the final administrator cannot be revoked),
- read the append-only audit history.

The backend remains the enforcement point: every `/api/v1/platform/*` endpoint requires `platform.admin`, the UI gating is cosmetic, and an organisation owner without a platform membership is rejected with `403 platform_admin_required`. Invitation and membership changes are all audited.

## Deployment (hybrid VPS profile)

v0.6 ships the provider-neutral production baseline (blueprint §35.1, ADR-0007): a generic Linux VPS / container-host profile in `deploy/compose/compose.hybrid-vps.yml` (Caddy edge with automatic TLS and edge rate limiting, the static Vue artifact, the FastAPI backend, the Dramatiq worker, and a private Redis) with managed PostgreSQL, object storage, WorkOS, transactional email and monitoring as external services. The deployment runs through `.github/workflows/deploy-vps.yml` (workflow dispatch or `v*` tag): it builds immutable images and a versioned frontend artifact, SSHes to a configurable host, runs exactly one deliberate `alembic upgrade head`, recreates the services, and waits for `/ready`, retaining the previous release for rollback. See `.env.production.example` for every production variable and the environment-separation rules.

Day-to-day operations, scaling, monitoring and alerts: `docs/operations.md`. Backup and recovery (database restore, object-storage recovery, secret recovery, deployment rollback, lost VPS replacement, environment recreation — including the recorded tested runs): `docs/backup-and-recovery.md`. Production hardening: `SECURITY.md` → Hybrid VPS production profile.

## Releases

The template is versioned and tagged. `make check` passing is the gate for a release. Current release: v0.6 (operations: observability, email, notifications, hybrid VPS deployment). See `TEMPLATE_V0_6_SCOPE.md` §6 for the progress log.

Development follows the branch workflow in `CONTRIBUTING.md`: work units live on `feature/*` branches and reach `main` only through reviewed pull requests, so CI runs once per merged unit rather than on every push.
