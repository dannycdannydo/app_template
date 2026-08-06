# Root Makefile — v0.1 command surface (blueprint §32, Scope §4).
#
# Two dev entry points per ADR-0008: native app code with containerised
# infrastructure (`make dev`) and the full-container path for CI parity and
# onboarding (`make dev-docker`). `make check` is the complete local quality
# gate (lint + typecheck + test + generated-client drift).
#
# Native backend commands load the repo-root `.env` by sourcing it in the
# recipe shell (see `load_env`), which parses dotenv syntax correctly (inline
# comments, quoted values) where make's own `include` would mangle values.
# pytest stays hermetic: its conftest sets test-only defaults and must not be
# overridden by a developer's `.env`.

COMPOSE ?= docker compose
COMPOSE_FILE := deploy/compose/compose.local.yml
# Compose only reads `.env` from its project directory (the compose file's
# directory when -f is used), so point it at the repo-root `.env` explicitly.
# Guard with wildcard: `--env-file` on a missing file is an error, and fresh
# clones / CI without .env fall back to compose's built-in defaults.
ENV_FILE := $(if $(wildcard .env),--env-file .env,)
COMPOSE_CMD := $(COMPOSE) -f $(COMPOSE_FILE) $(ENV_FILE)

# Source the repo-root .env into the recipe environment. Missing .env is a
# no-op; pydantic-settings then fails fast with a clear error pointing at
# DATABASE_URL.
define load_env
	set -a; [ -f .env ] && . ./.env; set +a;
endef

.PHONY: dev dev-docker migrate lint typecheck test e2e format generate-client check

## Start PostgreSQL + Redis in Docker, then run the API and frontend natively
## with live reload (ADR-0008). Infra stays up after Ctrl-C so `make migrate`
## and repeat `make dev` runs keep working; stop it with
## `docker compose -f deploy/compose/compose.local.yml down`.
dev:
	$(COMPOSE_CMD) up -d --wait postgres redis
	@echo "API on http://localhost:8000 (live reload), frontend on http://localhost:5173. Ctrl-C stops the apps; Postgres/Redis stay up."
	@$(load_env) (cd backend && uv run uvicorn app.main:app --reload --port 8000) & \
	(cd frontend && pnpm dev) & \
	wait

## Build and run the entire stack in containers (CI parity, onboarding,
## Dockerfile validation). Ctrl-C stops all services.
dev-docker:
	$(COMPOSE_CMD) --profile fullstack up --build

## Apply Alembic migrations to the database in DATABASE_URL.
migrate:
	@$(load_env) cd backend && uv run alembic upgrade head

## Ruff (backend) + ESLint/oxlint (frontend).
lint:
	cd backend && uv run ruff check .
	cd frontend && pnpm lint

## Pyright (backend) + vue-tsc (frontend).
typecheck:
	cd backend && uv run pyright
	cd frontend && pnpm type-check

## pytest (backend) + Vitest (frontend, single run).
test:
	cd backend && uv run pytest
	cd frontend && pnpm vitest run

## Playwright end-to-end journeys against the local stack (blueprint §31,
## §37). The specs mock the WorkOS token endpoint and the `/api/v1/**` surface
## at the browser network boundary, so no backend is required; the frontend
## dev server is started by Playwright itself. Authenticated journeys need
## `VITE_WORKOS_CLIENT_ID` in the repo-root `.env` (or the environment) or
## they skip, so copy `.env.example` first.
e2e:
	cd frontend && pnpm test:e2e

## Ruff format (backend) + Prettier (frontend).
format:
	cd backend && uv run ruff format .
	cd frontend && pnpm format

## Export OpenAPI from FastAPI and regenerate the TypeScript client.
generate-client:
	cd backend && uv run python -m scripts.export_openapi --output openapi.json
	cd frontend && pnpm generate:client

## Full local quality gate: lint + typecheck + test + generated-client drift.
check: lint typecheck test generate-client
	@git diff --exit-code -- frontend/src/api/generated/openapi.d.ts
	@echo "check passed: lint, typecheck, tests, and the generated client are all clean"
