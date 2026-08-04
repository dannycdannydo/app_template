# Template v0.1 — Scope & Progress Log

## Relationship to other documents

- `Internal_Custom_Application_Starter_Architecture_v2.md` is the long-term **design standard**.
- `IMPLEMENTATION_GUIDE.md` is the **build plan** and the incremental release sequence.
- This file is the **scoped contract for the v0.1 release**. It defines exact deliverables, exclusions, acceptance tests, and the commands that must work. It also serves as a progress log: check items off as they are completed.

---

# 1. Goal of v0.1

A **runnable foundation**. After v0.1, a fresh clone can start locally with one command, run migrations, pass the full quality gate, and ship through CI. No business modules, no authentication, no tenancy — those begin in v0.2.

v0.1 establishes every convention that later releases inherit: directory layout, typed configuration, DB session and migration wiring, the standard API error format, the generated-client pipeline, the Makefile surface, and the CI shape.

---

# 2. In Scope

```text
Repository structure and governance docs
Backend project: FastAPI, SQLAlchemy 2, Pydantic 2, Alembic
Typed configuration (pydantic-settings)
Database base, session, conventions
Alembic initialised with a baseline migration
Health and readiness endpoints
Standard API error format and exception handlers
Structured JSON logging with request IDs
Frontend project: Vue 3 + Vite + TypeScript
Tailwind CSS
shadcn-vue initialised with base components
Vue Router, Pinia, TanStack Query installed
Generated OpenAPI client pipeline (openapi-typescript + openapi-fetch)
Docker Compose for local development (PostgreSQL, Redis)
Backend and frontend Dockerfiles
Root Makefile with the required command surface
Ruff, Pyright, pytest (backend)
ESLint, Prettier, vue-tsc, Vitest (frontend)
GitHub Actions CI workflow
.env.example
Architecture Decision Records (ADRs 0001–0007)
pre-commit hooks
```

---

# 3. Out of Scope (Explicitly Deferred)

These are **not** part of v0.1. They appear in later releases per `IMPLEMENTATION_GUIDE.md`.

| Capability | Deferred to |
| --- | --- |
| WorkOS authentication, users, organisations, memberships | v0.2 |
| Roles, permissions, tenant isolation | v0.2 |
| Example domain module (model/schema/service/route with tenancy) | v0.2 |
| Frontend application shell (login, protected routes, layout, sidebar) | v0.3 |
| Storage interface, S3 adapter, MinIO, signed uploads | v0.4 |
| Dramatiq, durable job records | v0.4 |
| Audit log, Sentry, email, notifications | v0.5 |
| Hybrid VPS deployment | v0.5 |
| Managed Azure reference deployment | post-v1 |
| Transactional outbox, import/export framework, DB feature flags | post-v1 |

---

# 4. Commands That Must Work

```bash
make dev              # start Postgres, Redis, API, frontend
make migrate          # run Alembic migrations
make lint             # Ruff (backend) + ESLint (frontend)
make typecheck        # Pyright (backend) + vue-tsc (frontend)
make test             # pytest (backend) + Vitest (frontend)
make format           # Ruff format + Prettier
make generate-client  # export OpenAPI from FastAPI -> generate TS client
make check            # full local quality gate (lint + typecheck + test + drift)
```

---

# 5. Acceptance Criteria

v0.1 is done when **all** of the following are true:

1. A fresh clone, after `cp .env.example .env`, runs `make dev` and the API and frontend both respond.
2. `GET /health` returns `200` and `GET /ready` returns `200` with the database reachable.
3. `make migrate` applies the baseline migration cleanly against a fresh database.
4. An unhandled domain exception produces the standard structured error response with a `request_id`.
5. The frontend successfully calls `GET /health` through the generated client.
6. `make generate-client` regenerates the TypeScript client and produces no diff against the committed output.
7. `make check` passes with zero lint errors, zero type errors, and green tests.
8. CI runs the same gate on push and is green.
9. Governance docs (AGENTS, ARCHITECTURE, API_CONVENTIONS, SECURITY, CONTRIBUTING) and ADRs 0001–0007 exist and are non-empty.
10. No secrets are committed; `.env.example` documents every variable the app reads.

---

# 6. Progress Log

Check items off as they are completed. Keep one task `in_progress` at a time where practical.

## 6.1 Repository & Governance

- [ ] Initialise git repository
- [ ] Root `.gitignore` (Python, Node, env, editor, OS, build output)
- [ ] `.editorconfig`
- [ ] `README.md` (overview, prerequisites, quick start, command reference)
- [ ] `AGENTS.md` (mandatory agent rules from blueprint §33)
- [ ] `ARCHITECTURE.md` (condensed system shape and request flow)
- [ ] `API_CONVENTIONS.md` (REST, versioning, pagination, error format)
- [ ] `SECURITY.md` (baseline controls and responsible disclosure)
- [ ] `CONTRIBUTING.md` (workflow, commit style, review requirements)
- [ ] `docs/decisions/0001-use-workos.md`
- [ ] `docs/decisions/0002-use-sqlalchemy-and-pydantic.md`
- [ ] `docs/decisions/0003-use-vue.md`
- [ ] `docs/decisions/0004-use-dramatiq.md`
- [ ] `docs/decisions/0005-use-shadcn-vue.md`
- [ ] `docs/decisions/0006-provider-neutral-storage.md`
- [ ] `docs/decisions/0007-two-deployment-profiles.md`

## 6.2 Backend Project & Tooling

- [ ] `backend/pyproject.toml` with `uv` project metadata
- [ ] Dependencies pinned: FastAPI, Uvicorn, SQLAlchemy 2, Pydantic 2, `pydantic-settings`, Alembic, asyncpg/psycopg, structured-logging library
- [ ] Dev dependencies: pytest, pytest-asyncio, httpx, Ruff, Pyright
- [ ] `backend/uv.lock` committed
- [ ] Ruff configuration (lint rules + formatting)
- [ ] Pyright configuration (strict mode)
- [ ] pytest configuration (`backend/pytest.ini` or pyproject section) + `conftest.py`
- [ ] `.pre-commit-config.yaml` (Ruff, Ruff format, basic hooks)

## 6.3 Backend Application Shell

- [ ] `backend/app/main.py` — app factory, lifespan, router registration, exception handlers, middleware
- [ ] `backend/app/core/config.py` — typed `pydantic-settings` model, fail-fast on invalid production config
- [ ] `backend/app/core/exceptions.py` — domain exceptions (`NotFoundError`, `PermissionDenied`, `ConflictError`, `ValidationError`, etc.) and standard error schema
- [ ] `backend/app/core/logging.py` — structured JSON logging, request ID context
- [ ] `backend/app/api/dependencies.py` — request ID, DB session dependency
- [ ] `backend/app/api/health.py` — `/health` and `/ready` routes

## 6.4 Database & Migrations

- [ ] `backend/app/db/base.py` — declarative `Base`
- [ ] `backend/app/db/session.py` — session factory / dependency
- [ ] `backend/app/db/conventions.py` — naming, timestamp helpers, UUIDv7 type
- [ ] Alembic initialised: `backend/alembic/`, `alembic.ini`, `env.py`
- [ ] `env.py` wired to app settings and `Base.metadata`
- [ ] Baseline migration created and applies cleanly to a fresh database

## 6.5 Frontend Project & Tooling

- [ ] Scaffold via `pnpm create vue@latest frontend` (TS, Router, Pinia, Vitest, Playwright, ESLint, Prettier)
- [ ] `frontend/package.json` with pinned dependencies
- [ ] `frontend/pnpm-lock.yaml` committed
- [ ] Tailwind CSS configured (PostCSS, `tailwind.config`, design tokens)
- [ ] shadcn-vue initialised (`components.json`) with base components: `button`, `card`, `input`, `label`
- [ ] TanStack Vue Query installed and provider wired
- [ ] Base layout + a home view that calls `GET /health` via the generated client
- [ ] ESLint + Prettier configured
- [ ] `vue-tsc` strict typecheck passing

## 6.6 Generated Client Pipeline

- [ ] `openapi-typescript` and `openapi-fetch` installed in frontend
- [ ] Script to export `openapi.json` from the FastAPI app
- [ ] Generation script writes `frontend/src/api/generated/`
- [ ] `frontend/src/api/client.ts` — typed `openapi-fetch` client wrapper
- [ ] `make generate-client` works end-to-end

## 6.7 Local Development Infrastructure

- [ ] `backend/Dockerfile` (dev image, non-root user)
- [ ] `frontend/Dockerfile` (dev image)
- [ ] `deploy/compose/compose.local.yml` — PostgreSQL, Redis, API, frontend with healthchecks
- [ ] `.env.example` documenting every variable
- [ ] Volume mounts for live reload (backend + frontend)

## 6.8 Makefile

- [ ] `make dev`
- [ ] `make migrate`
- [ ] `make lint`
- [ ] `make typecheck`
- [ ] `make test`
- [ ] `make format`
- [ ] `make generate-client`
- [ ] `make check`

## 6.9 CI

- [ ] `.github/workflows/ci.yml` with jobs for:
  - [ ] backend formatting + linting (Ruff)
  - [ ] backend type checks (Pyright)
  - [ ] backend tests (pytest)
  - [ ] frontend formatting + linting (ESLint + Prettier)
  - [ ] Vue type checks (vue-tsc)
  - [ ] frontend tests (Vitest)
  - [ ] generated-client drift detection
  - [ ] migration validity (Alembic upgrade against fresh DB)
  - [ ] container build (backend + frontend)

## 6.10 Validation

- [ ] Clean-clone procedure documented in README
- [ ] Fresh-clone run verified: `cp .env.example .env` → `make dev` works
- [ ] `make check` green from a clean checkout
- [ ] CI green on the default branch
- [ ] Tag `v0.1.0`

---

# 7. Blueprint Reference Map

Each §6 subsection maps to specific sections of `Internal_Custom_Application_Starter_Architecture_v2.md`. Implementers and reviewers read **only** the listed sections for a given task — not the whole blueprint. This keeps context lean and focused.

| Subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| §6.1 Repository & Governance | §33 (agent governance), §34 (ADRs), §40 (repo structure) | Required docs, ADR format, agent rules, top-level layout |
| §6.2 Backend Project & Tooling | §3 (stack), §32 (tooling), §27 (config) | Dependency list, tool choices, typed settings model |
| §6.3 Backend Application Shell | §6 (request flow), §13 (API errors), §27 (config), §28 (observability) | Layered flow, error schema, exception mappings, health endpoints, logging context |
| §6.4 Database & Migrations | §7 (models & schemas), §10 (DB conventions) | ORM/Pydantic separation, naming, timestamps, UUIDv7, Alembic use |
| §6.5 Frontend Project & Tooling | §14 (frontend arch), §16 (UI & design) | Directory layout, Vue conventions, state management split, design tokens |
| §6.6 Generated Client Pipeline | §15 (generated client) | Source-of-truth flow, openapi-typescript + openapi-fetch, drift rules |
| §6.7 Local Dev Infrastructure | §36 (Docker & build) | One backend Dockerfile, frontend Dockerfile, compose file layout, runtime commands |
| §6.8 Makefile | §32 (shared commands) | The canonical command list and what each does |
| §6.9 CI | §37 (CI/CD) | CI checks list, immutable image tagging, migration-as-release-job |
| §6.10 Validation | §42 (template validation), §45 (v1 readiness — subset) | Fresh-clone test procedure, the v0.1-relevant readiness items only |

If a task touches a concern not listed here (e.g. security baseline), consult the blueprint's table of contents and read only the relevant section. When in doubt, read less rather than more — the scope file §2–§5 already encodes the v0.1 contract.

---

# 8. Status

```text
Release:    v0.1.0 (foundation)
State:      in progress
Started:    —
Completed:  —
```

When every acceptance criterion in §5 is met and every box in §6 is checked, update the version recording in `pyproject.toml` and tag `v0.1.0`. Then open `TEMPLATE_V0_2_SCOPE.md`.
