# Release checklist and fresh-clone smoke

This checklist is the evidence a template release must produce. It mirrors
blueprint §41–§42 (template lifecycle and validation): the starter is a
maintained product, and a fresh clone must pass the same checks as CI. Run it
on a clean checkout of the release commit, then record any unsupported or
deferred capability honestly (see "Known limitations and deferred features"
below).

Every step is labelled with the evidence it actually produces:

- **CI** — an automated job in `.github/workflows/ci.yml` that runs on every
  push to `main` and every pull request (unless noted otherwise).
- **Manual** — a fresh-clone step a releaser runs by hand on a clean checkout;
  CI does not perform it.
- **Opt-in** — a check that needs credentials or infrastructure CI does not
  hold; run it deliberately for a release, not on every change.

CI does **not** run the aggregate `make check`, copy or boot `.env.example`,
run `make dev-infra-check`, run `alembic check`, or run the live provider
contracts. Those are the Manual and Opt-in steps below.

## Fresh-clone smoke scenario

1. **Example environment validation** (**Manual**)
   - `cp .env.example .env`
   - `make dev-infra-check` (or `make dev`): both `BROKER_REDIS_URL` and
     `RATE_LIMIT_REDIS_URL` must be reachable from the host, proving a distinct
     broker/rate-limit topology.
   - The API must boot on the example settings; `pydantic-settings` fails fast
     with a clear error if a required variable is missing.
2. **Dependencies** (**Manual**)
   - `cd backend && uv sync`
   - `cd ../frontend && pnpm install`
3. **Migrations** (**CI + Manual**)
   - `make migrate` on an empty database (`alembic upgrade head`).
   - `cd backend && uv run alembic check` reports no autogenerate drift
     (**Manual**; the `migration-validity` job only upgrades, downgrades and
     re-upgrades on a fresh database).
   - CI job: `migration-validity`.
4. **Generated client** (**CI**)
   - `make generate-client`
   - `git diff --exit-code -- frontend/src/api/generated/openapi.d.ts` must be
     empty, proving the checked-in client matches the OpenAPI schema.
   - CI job: `generate-client-drift`.
5. **Protected-route security suite** (**CI**)
   - `cd backend && uv run pytest tests/test_security_suite.py tests/test_security.py`
   - Every protected `/api/v1` route is in `PROTECTED_ROUTES`; webhook
     signatures are rejected. The suite includes the completeness guard.
   - CI job: `backend-test`.
6. **External upload** (**CI**)
   - MinIO-backed storage tests: `cd backend && uv run pytest -m storage_integration`
     (CI job: `storage-integration`).
   - Browser journey against the real external storage origin:
     `cd frontend && pnpm test:e2e -- e2e/ai-ask.spec.ts` (CI job:
     `playwright-smoke`). The spec starts `e2e/storage-server.mjs` and exercises
     the browser-enforced CORS preflight and signed PUT to that origin, plus the
     denied-CORS negative case. `e2e/files.spec.ts` only intercepts a mock
     storage host, so it is not the external-origin proof.
7. **Deployment Compose configuration** (**CI**)
   - `docker compose -f deploy/compose/compose.hybrid-vps.yml config`
   - `docker compose -f deploy/compose/compose.local.yml --profile fullstack config`
   - `scripts/assert_redis_compose_policy.py` (broker `noeviction`/AOF vs
     rate-limit counter policy) and `scripts/assert_deployment_boundaries.py`
     (Caddy-only forwarded-IP trust and CSP storage origin).
   - CI jobs: `compose-hybrid-vps-validation`, `caddy-edge-validation`.
8. **Full gates** (**Manual**)
   - `make lint`, `make typecheck`, `make test`, `make e2e`,
     `make validate-ai-registries`, `make validate-execution-contracts`,
     `make check`.
   - The individual checks also run as CI jobs; the aggregate `make check`
     (which additionally enforces generated-client drift) is the Manual
     fresh-clone gate.
   - Opt-in live provider contracts (non-production accounts only):
     `make test-ai-contracts` (**Opt-in**; no CI job).

## Release bookkeeping

- `backend/tests/test_release_versions.py` proves the backend, frontend and
  `[tool.project-template]` versions agree and match the `TEMPLATE_V0_N_SCOPE.md`
  named by the recorded version (`Release:` and `State: complete`), so an
  unreleased higher scope cannot mask a drifted release.
- The upgrade guide for the release exists under `docs/upgrades/` (blueprint
  §41).
- Tags are immutable; correct bookkeeping drift in a new reviewed commit.

## Fresh-clone smoke record

The scenario above was executed on a clean `git worktree` checkout of the P10
work unit on 2026-09-18. Host ports were remapped to avoid colliding with the
development stack on the same machine; the example environment, the distinct
broker/rate-limit topology and every gate were otherwise unchanged.

- **Example environment** (**Manual**): `cp .env.example .env`,
  `make dev-infra-check` passed (both Redis endpoints reachable) and the API
  booted on the example settings with `/health` returning `{"status":"ok"}`.
- **Dependencies** (**Manual**): `uv sync --frozen` and
  `pnpm install --frozen-lockfile` completed cleanly from the lockfiles.
- **Migrations**: `make migrate` on an empty database applied every migration,
  and `uv run alembic check` reported no autogenerate drift.
- **Generated client** (**CI**): `make generate-client` left `openapi.d.ts`
  unchanged (`git diff --exit-code` clean), now verified on a committed tree.
- **Protected-route security suite** (**CI**):
  `pytest tests/test_security_suite.py tests/test_security.py` — 503 passed.
- **External upload** (**CI**): MinIO `pytest -m storage_integration` — 8
  passed; the `e2e/ai-ask.spec.ts` journey against the real external storage
  origin ran on Chromium, Firefox and WebKit — 30 passed in `make e2e`.
- **Deployment Compose configuration** (**CI**): hybrid-VPS production and
  local fullstack `config` plus the Redis-policy and deployment-boundary
  assertions passed.
- **Full gates** (**Manual**): `make check` passed; `make e2e` passed; the
  opt-in `make test-ai-contracts` skipped cleanly (no live credentials).

## Known limitations and deferred features

The starter states these honestly rather than implying capabilities it does not
have. Each is a deliberate, reviewed boundary.

- **Malware scanning**: no scanner provider is selected. `FILE_SCAN_PROVIDER=none`
  is the template position, and production requires
  `FILE_SCAN_ACKNOWLEDGE_UNSCANNED=true` so an unscanned deployment is
  deliberate. The quarantine state, the `ContentScanner` seam and the
  deny-by-default gate ship; choosing and wiring a provider is clone work
  (`SECURITY.md` → "File security").
- **Exactly-once external effects**: not guaranteed and not claimed. Delivery is
  at-least-once with stable idempotency where a provider supports it and durable
  attention-required outcomes for ambiguous external effects (for example an
  acceptance-unknown email is never auto-resent).
- **Large non-inline AI files**: Azure OpenAI, DeepSeek and local adapters fail
  closed for non-inline files in this release; only OpenAI, Anthropic and Vertex
  implement transfer modes, and only when explicitly enabled.
- **Synchronous AI ask bound**: `document.ask` is bounded by
  `AI_ASK_MAX_SYNCHRONOUS_BYTES` (default and maximum 5,000,000 bytes). This
  release exposes no durable asynchronous ask operation, so a larger document
  must be reduced in size.
- **Decompression-bomb protections and process document/page limits**: deferred
  to post-v1 (`SECURITY.md` → "Deferred controls"); the release ships the
  quarantine/failed states and the scanning seam.
- **General workflow orchestration**: no DAGs, priorities or cancellation; the
  template keeps the one-job execution model. No worker dashboard is provided —
  observability is metrics plus the documented alert queries in
  `docs/operations.md`.
- **Provider reference reuse**: a provider-side reference is reused only by
  retries of one logical AI execution; reuse across distinct AI requests is
  prohibited.
