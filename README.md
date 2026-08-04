# Application Starter Template

A reusable full-stack application starter: **FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL** on the backend, **Vue 3 + TypeScript + Tailwind + shadcn-vue** on the frontend, arranged as a modular monolith with WorkOS authentication, Dramatiq jobs, and provider-neutral storage.

This repository is a **template**, not an application. New projects start from a tagged release of this template and then become independent repositories.

## What you get

- Modular monolith backend with typed configuration, structured logging, and a standard API error format
- Vue 3 SPA with a generated, type-safe OpenAPI client
- Alembic migrations wired to application settings
- Docker Compose local development (PostgreSQL, Redis, API, frontend)
- A single Makefile surface for development and quality gates
- CI that runs the same gate on every push
- Governance docs and architecture decision records (ADRs)

The authoritative design standard is `Internal_Custom_Application_Starter_Architecture_v2.md`. The scoped contract and progress log for the current release is `TEMPLATE_V0_1_SCOPE.md`. Agents read the architecture documentation before structural changes (see `AGENTS.md`).

## Repository layout

```text
backend/                 FastAPI application (app/, alembic/, pyproject.toml)
frontend/                Vue 3 + Vite application (src/, package.json)
deploy/                  Compose files and deployment profiles
docs/decisions/          Architecture decision records (ADR 0001-0007)
.github/workflows/       CI pipeline
Makefile                 Command surface for development and quality gates
.env.example             Documented environment variables
```

## Prerequisites

- Docker (with Docker Compose) for local services
- `uv` for backend dependency management (`pip install uv` or your package manager)
- `pnpm` for frontend dependency management (`corepack enable` or `npm i -g pnpm`)
- `make`

## Quick start

```bash
# 1. Clone and prepare environment
cp .env.example .env

# 2. Start everything: Postgres, Redis, API, frontend
make dev
```

The API is served at `http://localhost:8000` (docs at `/docs`) and the frontend at `http://localhost:5173`.

Clean-clone verification: `cp .env.example .env && make dev` must start both services, `make migrate` must apply the baseline migration, and `make check` must pass with zero lint errors, zero type errors, and green tests.

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
