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
11. A clean architecture audit (`prompts/04-architecture-audit.md`) passes with no CRITICAL or MAJOR findings before tagging.

---

# 6. Progress Log

Check items off as they are completed. Keep one task `in_progress` at a time where practical.

## 6.1 Repository & Governance

- [x] Initialise git repository
- [x] Root `.gitignore` (Python, Node, env, editor, OS, build output)
- [x] `.editorconfig`
- [x] `README.md` (overview, prerequisites, quick start, command reference)
- [x] `AGENTS.md` (mandatory agent rules from blueprint §33)
- [x] `ARCHITECTURE.md` (condensed system shape and request flow)
- [x] `API_CONVENTIONS.md` (REST, versioning, pagination, error format)
- [x] `SECURITY.md` (baseline controls and responsible disclosure)
- [x] `CONTRIBUTING.md` (workflow, commit style, review requirements)
- [x] `docs/decisions/0001-use-workos.md`
- [x] `docs/decisions/0002-use-sqlalchemy-and-pydantic.md`
- [x] `docs/decisions/0003-use-vue.md`
- [x] `docs/decisions/0004-use-dramatiq.md`
- [x] `docs/decisions/0005-use-shadcn-vue.md`
- [x] `docs/decisions/0006-provider-neutral-storage.md`
- [x] `docs/decisions/0007-two-deployment-profiles.md`

## 6.2 Backend Project & Tooling

- [x] `backend/pyproject.toml` with `uv` project metadata
- [x] Dependencies pinned: FastAPI, Uvicorn, SQLAlchemy 2, Pydantic 2, `pydantic-settings`, Alembic, asyncpg/psycopg, structured-logging library
- [x] Dev dependencies: pytest, pytest-asyncio, httpx, Ruff, Pyright
- [x] `backend/uv.lock` committed
- [x] Ruff configuration (lint rules + formatting)
- [x] Pyright configuration (strict mode)
- [x] pytest configuration (`backend/pytest.ini` or pyproject section) + `conftest.py`
- [x] `.pre-commit-config.yaml` (Ruff, Ruff format, basic hooks)

## 6.3 Backend Application Shell

- [x] `backend/app/main.py` — app factory, lifespan, router registration, exception handlers, middleware
- [x] `backend/app/core/config.py` — typed `pydantic-settings` model, fail-fast on invalid production config
- [x] `backend/app/core/exceptions.py` — domain exceptions (`NotFoundError`, `PermissionDenied`, `ConflictError`, `ValidationError`, etc.) and standard error schema
- [x] `backend/app/core/logging.py` — structured JSON logging, request ID context
- [x] `backend/app/api/dependencies.py` — request ID, DB session dependency
- [x] `backend/app/api/health.py` — `/health` and `/ready` routes

## 6.4 Database & Migrations

- [x] `backend/app/db/base.py` — declarative `Base`
- [x] `backend/app/db/session.py` — session factory / dependency
- [x] `backend/app/db/conventions.py` — naming, timestamp helpers, UUIDv7 type
- [x] Alembic initialised: `backend/alembic/`, `alembic.ini`, `env.py`
- [x] `env.py` wired to app settings and `Base.metadata`
- [x] Baseline migration created and applies cleanly to a fresh database

## 6.5 Frontend Project & Tooling

- [x] Scaffold via `pnpm create vue@latest frontend` (TS, Router, Pinia, Vitest, Playwright, ESLint, Prettier)
- [x] `frontend/package.json` with pinned dependencies
- [x] `frontend/pnpm-lock.yaml` committed
- [x] Tailwind CSS configured (PostCSS, `tailwind.config`, design tokens)
- [x] shadcn-vue initialised (`components.json`) with base components: `button`, `card`, `input`, `label`
- [x] TanStack Vue Query installed and provider wired
- [x] Base layout + a home view that calls `GET /health` via the generated client
- [x] ESLint + Prettier configured
- [x] `vue-tsc` strict typecheck passing

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
- [ ] Architecture audit (`prompts/04-architecture-audit.md`) clean — no CRITICAL or MAJOR findings
- [ ] Tag `v0.1.0`

---

# 7. Blueprint Reference Map

Each scope subsection (left column) maps to specific sections of `Internal_Custom_Application_Starter_Architecture_v2.md`. Implementers and reviewers read **only** the listed sections for a given task — not the whole blueprint. This keeps context lean and focused.

## Disambiguating section numbers

Two documents use the `§` symbol. Do not confuse them:

- **Scope §6.x** — a subsection of *this file's* checklist (e.g. §6.3 = "Backend Application Shell").
- **BP §N** — a section of the *blueprint* (e.g. BP §13 = "API Errors", starting at line 636).

The map below always uses `BP §N` for blueprint sections and `Scope §6.x` for this file's checklist. **Never** write a bare `§6` or `§13` — always prefix with `Scope` or `BP` so the next reader knows which document you mean.

## Map

Line ranges let you jump straight to the section with `view` + offset/limit — no grep needed. Each range covers the section up to the next `#` heading.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Repository & Governance | **BP §33** (lines 1627–1672), **BP §34** (lines 1673–1699), **BP §40** (lines 1947–1975) | Required docs, ADR format, agent rules, top-level layout |
| **Scope §6.2** Backend Project & Tooling | **BP §3** (lines 62–130), **BP §32** (lines 1576–1626), **BP §27** (lines 1340–1384) | Dependency list, tool choices, typed settings model |
| **Scope §6.3** Backend Application Shell | **BP §6** (lines 217–269), **BP §13** (lines 636–685), **BP §27** (lines 1340–1384), **BP §28** (lines 1385–1427) | Layered flow, error schema, exception mappings, health endpoints, logging context |
| **Scope §6.4** Database & Migrations | **BP §7** (lines 270–324), **BP §10** (lines 463–543) | ORM/Pydantic separation, naming, timestamps, UUIDv7, Alembic use |
| **Scope §6.5** Frontend Project & Tooling | **BP §14** (lines 686–742), **BP §16** (lines 779–817) | Directory layout, Vue conventions, state management split, design tokens |
| **Scope §6.6** Generated Client Pipeline | **BP §15** (lines 743–778) | Source-of-truth flow, openapi-typescript + openapi-fetch, drift rules |
| **Scope §6.7** Local Dev Infrastructure | **BP §36** (lines 1814–1860) | One backend Dockerfile, frontend Dockerfile, compose file layout, runtime commands |
| **Scope §6.8** Makefile | **BP §32** (lines 1576–1626) | The canonical command list and what each does |
| **Scope §6.9** CI | **BP §37** (lines 1861–1903) | CI checks list, immutable image tagging, migration-as-release-job |
| **Scope §6.10** Validation | **BP §42** (lines 2038–2059), **BP §45** (lines 2113–2134) | Fresh-clone test procedure, the v0.1-relevant readiness items only |

If a task touches a concern not listed here (e.g. security baseline), consult the blueprint's table of contents and read only the relevant section. When in doubt, read less rather than more — this file's §2–§5 already encodes the v0.1 contract.

---

# 8. Status

```text
Release:    v0.1.0 (foundation)
State:      in progress
Started:    —
Completed:  —
```

When every acceptance criterion in §5 is met and every box in §6 is checked, update the version recording in `pyproject.toml` and tag `v0.1.0`. Then open `TEMPLATE_V0_2_SCOPE.md`.
