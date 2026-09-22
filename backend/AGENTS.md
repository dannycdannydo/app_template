# Backend — Agent Guide

Read this before changing backend code. It is the backend-wide orientation; the
root `AGENTS.md` carries the mandatory rules and review gates, and the design
authorities are `ARCHITECTURE.md`, `API_CONVENTIONS.md`,
`Internal_Custom_Application_Starter_Architecture_v2.md` (the blueprint) and
`CONTRIBUTING.md`. Area-specific detail lives in the nested guides indexed from
the root: `app/db/AGENTS.md` (models, queries, migrations, RLS), `app/ai/AGENTS.md`
and `app/job_coordinator/AGENTS.md`. Keep this guide current in the same change
that alters a backend rule or convention.

## Layering — router → service → queries → models

- App factory: `app/main.py` (`create_app()`); routers are imported explicitly and
  added with `app.include_router(...)` — there is **no auto-discovery**.
- Domain modules live in `app/modules/<name>/`, conventionally:
  `router.py` (thin: parse, authorise, delegate), `schemas.py` (Pydantic; requests
  use `extra="forbid"`), `service.py` (business logic, transaction boundary,
  audit), `queries.py` (reused/complex SQL), `models.py` (SQLAlchemy ORM),
  optional `tasks.py` (Dramatiq actors). `app/modules/records/` is the canonical
  example.
- New ORM models must be imported in the block at the bottom of `app/db/base.py`,
  or Alembic will not see them.
- Register every new model in `backend/tests/tenant_isolation_registry.py` and
  read `app/db/AGENTS.md` before any model/query/migration work.

## API conventions

- **Errors:** raise the `APIError` hierarchy from `app/core/exceptions.py`
  (`NotFoundError` 404, `BadRequestError` 400, `UnauthorizedError` 401,
  `PermissionDenied` 403, `ConflictError` 409, `ValidationError` 422, …). Routers
  never hand-build error bodies. `app/main.py` maps them to the standard envelope
  `{code, message, details, request_id}` and never leaks stack traces.
- **Request id:** middleware accepts/generates `X-Request-ID`, binds it to
  structlog, echoes it, and adds security headers.
- **Response schemas:** every endpoint declares `response_model=` (or an explicit
  return annotation). ORM models are never request/response models.
- **Pagination:** `?page=&page_size=` → `{items, page, page_size, total}`.
- **Concurrency:** collaborative resources carry an integer `version`; a stale
  update/delete is a `409` with a resource-specific code, under `FOR UPDATE`.
- **Dependencies:** `app/api/dependencies.py` provides `get_db`, `get_current_user`,
  `get_current_membership`, `require_permission`, `require_platform_permission`,
  `require_workos_webhook_signature`.

## Authentication and permissions

- `get_current_user`: validates the Bearer session, rejects impersonated/disabled
  users, provisions the internal user, runs the bootstrap-admin + login-time
  invitation chain, and binds `app.user_id` transaction-locally.
- `get_current_membership`: parses `X-Org-Id` (missing/malformed → 400), requires
  an `ACTIVE` membership (else 403 `not_a_member`), then binds `app.organisation_id`
  from the **validated membership row** — never from the header directly.
- `require_permission(code)`: composes the membership dependency and checks the
  caller's role bundles; default deny.
- `require_platform_permission(code)`: composes `get_current_user` only and never
  consults `X-Org-Id`; binds the platform read context after authorisation.
- **Planes never cross:** org routes (`/api/v1/...`, Bearer + `X-Org-Id`) and
  platform routes (`/api/v1/platform/*`, no `X-Org-Id`) grant nothing to each
  other. There is no `is_admin`/superuser boolean.
- Role/permission catalogue: `app/modules/permissions/constants.py`
  (`PERMISSIONS`, `ROLE_PERMISSION_MAP`, `PLATFORM_PERMISSIONS`). Adding a
  permission code means editing that file **and** an Alembic data migration that
  inserts and grants it (pattern:
  `alembic/versions/9e4f5c6a7d8b_notification_permission_codes.py`). Permission-model
  changes are human-review gated.

## Config, observability, cross-cutting

- Settings: one typed `Settings(BaseSettings)` in `app/core/config.py`, read only
  through the `@lru_cache get_settings()`. Document new settings in
  `.env.production.example`; `_validate_config` enforces production requirements.
- Logging: structlog via `app/core/logging.py`, with redaction; bind identity with
  `bind_identity_context` / `bind_worker_context`. Never log secrets, prompts,
  document bytes, signed URLs or row contents.
- Metrics: `app/observability/metrics.py` (public `GET /metrics`); labels are
  normalised and never carry tenant/content values. Errors via
  `app/observability/sentry.py`. Audit is append-only through
  `app/modules/audit/service.py` (inserts in the caller's transaction, never
  commits).
- Adapters stay behind factories — provider SDKs may only be imported inside the
  adapter package: `app/storage/`, `app/email/`, `app/scanning/`,
  `app/integrations/workos/`, `app/ai/providers/`. The AI boundary is enforced by
  `tests/test_ai_import_boundary.py`.
- Long-running work goes through durable jobs, never inline HTTP (see
  `app/job_coordinator/AGENTS.md`).

## Testing and the quality gate

- `backend/tests/` is flat; helpers include `auth_helpers.py`,
  `context_helpers.py`, `rls_helpers.py`, `org_isolation_helpers.py`,
  `tenant_isolation_registry.py`. `tests/conftest.py` is hermetic (fake
  storage/email/AI, `StubBroker`).
- **Every new protected `/api/v1` endpoint must be added to `PROTECTED_ROUTES`**
  (or `SIGNATURE_GATED_ROUTES`) in `tests/test_security_suite.py`;
  `test_no_protected_route_is_left_out` fails otherwise. Add cross-organisation
  coverage in `tests/test_org_isolation_matrix_db.py`.
- Real-PostgreSQL suites (`test_*_db.py`, `test_rls_*_enablement_db.py`) skip when
  no database is reachable — a green DB-less run does not prove them; CI provides
  `postgres:17` and Redis.
- `make check` = lint (ruff) + typecheck (pyright strict) + tests + AI-registry
  and execution-contract validation + generated-client drift. Do not weaken it.

## Gotchas

- **Cross-organisation resources are `404`, never `403`.** Identity fields are
  rejected by `extra="forbid"` so they cannot be smuggled into a body.
- **Commits clear transaction-local RLS context** — a service that commits must
  rebind before its next protected read/write (`app/db/AGENTS.md`).
- **`app/db/base.py`'s import block is load-bearing** for Alembic autogenerate.
- **Adapter import boundaries are tested** — don't import provider SDKs outside
  their adapter packages.
- **Regenerate the frontend client** (`make generate-client`) when the HTTP
  surface changes; drift fails `make check`.
- `ARCHITECTURE.md`'s "Backend structure" sketch is design-level and drifts from
  the tree (it lists `app/events/`/`app/workers/` that do not exist and puts
  routers in `app/api/`). Treat the filesystem as authoritative.

## Key files

- `app/main.py` — app factory, middleware, exception handlers, router registration
- `app/core/exceptions.py`, `app/core/config.py`, `app/core/logging.py`
- `app/api/dependencies.py` — identity, membership, permission dependencies
- `app/modules/records/` — canonical module layout
- `app/modules/permissions/constants.py` — role/permission catalogue
- `app/modules/audit/service.py` — audit append
- `app/observability/` — metrics and Sentry
- `backend/tests/test_security_suite.py`, `backend/tests/tenant_isolation_registry.py`
- `ARCHITECTURE.md`, `API_CONVENTIONS.md`, `CONTRIBUTING.md`
