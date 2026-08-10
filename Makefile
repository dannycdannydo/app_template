# Root Makefile — v0.1 command surface (blueprint §32, Scope §4).
#
# Two dev entry points per ADR-0008: native app code with containerised
# infrastructure (`make dev`, including the Dramatiq worker natively) and the
# full-container path for CI parity and onboarding (`make dev-docker`).
# `make check` is the complete local quality gate (lint + typecheck + test +
# generated-client drift).
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

.PHONY: dev dev-docker worker migrate provision-admin provision-admin-delete lint typecheck test e2e format generate-client validate-ai-registries check

## Start PostgreSQL + Redis + MinIO + Mailhog in Docker, then run the API, the
## Dramatiq worker and the frontend natively with live reload (ADR-0008).
## Infra stays up after Ctrl-C so `make migrate` and repeat `make dev` runs
## keep working; stop it with
## `docker compose -f deploy/compose/compose.local.yml down`.
dev:
	$(COMPOSE_CMD) up -d --wait postgres redis minio mailhog
	$(MAKE) migrate
	@echo "API on http://localhost:8000 (live reload), worker native, frontend on http://localhost:5173, MinIO console on http://localhost:9001, Mailhog UI on http://localhost:8025. Ctrl-C stops the apps; Postgres/Redis/MinIO/Mailhog stay up."
	@$(load_env) exec bash scripts/dev.sh

## Build and run the entire stack in containers (CI parity, onboarding,
## Dockerfile validation). Ctrl-C stops all services.
dev-docker:
	$(COMPOSE_CMD) --profile fullstack up --build

## Apply Alembic migrations to the database in DATABASE_URL.
migrate:
	@$(load_env) cd backend && uv run alembic upgrade head

## Run the Dramatiq worker natively (blueprint §36, ADR-0004, Scope §6.2).
## One local worker process runs WORKER_CONCURRENCY threads (default 8). Its
## ten-second shutdown timeout keeps Ctrl-C responsive during development.
worker:
	@$(load_env) cd backend && uv run dramatiq app.workers --processes 1 --threads $${WORKER_CONCURRENCY:-8} --worker-shutdown-timeout 10000

## Create the bootstrap platform admin in WorkOS (email + password; idempotent).
## Reads BOOTSTRAP_PLATFORM_ADMIN_EMAIL / BOOTSTRAP_PLATFORM_ADMIN_PASSWORD from
## the repo-root .env. Run once after the app is created, before the first
## login, when WorkOS signups are disabled.
provision-admin:
	@$(load_env) cd backend && uv run python -m scripts.provision_bootstrap_admin

## Delete the bootstrap platform admin again (WorkOS user + internal users row,
## which resets the one-time bootstrap so a fresh admin can be provisioned).
## Uses BOOTSTRAP_PLATFORM_ADMIN_EMAIL from the repo-root .env by default;
## override with EMAIL=someone@example.com. Needs the database reachable.
provision-admin-delete:
	@$(load_env) cd backend && uv run python -m scripts.provision_bootstrap_admin --delete $(if $(EMAIL),--email $(EMAIL),)

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

## Fail fast on invalid checked-in AI task, prompt, schema or model links.
validate-ai-registries:
	cd backend && uv run python -m scripts.validate_ai_registries

## Full local quality gate: lint + typecheck + test + generated-client drift.
check: lint typecheck test validate-ai-registries generate-client
	@git diff --exit-code -- frontend/src/api/generated/openapi.d.ts
	@echo "check passed: lint, typecheck, tests, and the generated client are all clean"
