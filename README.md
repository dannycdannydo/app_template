# Application Starter Template

A reusable full-stack application starter: **FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL** on the backend, **Vue 3 + TypeScript + Tailwind + shadcn-vue** on the frontend, arranged as a modular monolith with WorkOS authentication, Dramatiq jobs, and provider-neutral storage.

This repository is a **template**, not an application. New projects start from a tagged release of this template and then become independent repositories.

## What you get

- Modular monolith backend with typed configuration, structured logging, and a standard API error format
- Vue 3 SPA with a generated, type-safe OpenAPI client
- Alembic migrations wired to application settings
- Local development per ADR-0008: PostgreSQL and Redis in Docker, app code native (`make dev`), with a full-container path for CI parity (`make dev-docker`)
- A single Makefile surface for development and quality gates
- CI that runs the same gate on every push to `main` and on pull requests
- Governance docs and architecture decision records (ADRs)

The authoritative design standard is `Internal_Custom_Application_Starter_Architecture_v2.md`. The scoped contract and progress log for the current release is `TEMPLATE_V0_1_SCOPE.md`. Agents read the architecture documentation before structural changes (see `AGENTS.md`).

## Repository layout

```text
backend/                 FastAPI application (app/, alembic/, pyproject.toml)
frontend/                Vue 3 + Vite application (src/, Dockerfile, nginx.conf)
deploy/compose/          Compose files (compose.local.yml = local development)
docs/decisions/          Architecture decision records (ADR 0001-0008)
.github/workflows/       CI pipeline
Makefile                 Command surface for development and quality gates
.env.example             Documented environment variables
```

## Prerequisites

Local development follows **ADR-0008**: PostgreSQL and Redis run in Docker, while the API and frontend run natively on the host. The host toolchain is therefore required and pinned:

- **Python 3.13** with `uv` for the backend (`uv` installs Python if needed; the version is recorded in `backend/.python-version`)
- **Node >= 24** with `pnpm` (11.x) for the frontend
- **Docker with Compose** for PostgreSQL and Redis
- `make`

## Clean clone

```bash
# 1. Clone, then prepare the environment (single .env at the repo root)
cp .env.example .env

# 2. Install dependencies (backend lockfile via uv; frontend via pnpm)
cd backend && uv sync
cd ../frontend && pnpm install

# 3. Start PostgreSQL + Redis in Docker, and the API + frontend natively
#    with live reload (this is the day-to-day workflow)
cd .. && make dev

# 4. Apply the baseline migration to the fresh database
make migrate

# 5. Run the full quality gate (lint + typecheck + test + client drift)
make check
```

If a port is already taken on your machine, override it in `.env` (e.g. `REDIS_PORT=6380` when a local Redis runs on 6379) — every variable is documented in `.env.example`.

The API is served at `http://localhost:8000` (docs at `/docs`) and the frontend at `http://localhost:5173`, which proxies API traffic to the backend.

For CI parity, onboarding, or Dockerfile validation the **entire stack runs in containers** instead:

```bash
make dev-docker
```

`make dev-docker` starts the same Postgres and Redis, plus the API and frontend containers (built from `backend/Dockerfile` and `frontend/Dockerfile`), and runs migrations automatically on container start. Both commands share `deploy/compose/compose.local.yml`: the default service set is infrastructure only, and the `fullstack` Compose profile adds the application containers.

Verification: after `cp .env.example .env`, both `make dev` and `make dev-docker` must start the services, `make migrate` must apply the baseline migration, and `make check` must pass with zero lint errors, zero type errors, and green tests.

## Command reference

| Command | What it does |
| --- | --- |
| `make dev` | Start Postgres, Redis, API, and frontend |
| `make migrate` | Run Alembic migrations |
| `make lint` | Ruff (backend) + ESLint (frontend) |
| `make typecheck` | Pyright (backend) + vue-tsc (frontend) |
| `make test` | pytest (backend) + Vitest (frontend) |
| `make format` | Ruff format + Prettier |
| `make generate-client` | Export OpenAPI from FastAPI and generate the TypeScript client |
| `make check` | Full local quality gate: lint + typecheck + test + generated-client drift |

## Releases

The template is versioned and tagged. `make check` passing is the gate for a release. Current release: v0.1 (foundation). See `TEMPLATE_V0_1_SCOPE.md` §6 for the progress log.

Development follows the branch workflow in `CONTRIBUTING.md`: work units live on `feature/*` branches and reach `main` only through reviewed pull requests, so CI runs once per merged unit rather than on every push.
