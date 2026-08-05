# Architecture

A condensed view of the system shape and request flow. The authoritative design standard is `Internal_Custom_Application_Starter_Architecture_v2.md` (blueprint §4, §5, §6, §40); individual decisions are recorded in `docs/decisions/`.

## System shape

The default architecture is a **modular monolith**. Microservices are not part of the default; a service may be extracted only when there is a demonstrated operational or organisational need.

```text
Vue SPA
   │
   ▼
FastAPI
   │
   ├── PostgreSQL
   ├── Redis
   ├── Dramatiq workers
   ├── Object storage
   ├── WorkOS
   ├── Email provider
   └── External integrations
```

## Backend structure

```text
backend/app/
├── main.py              app factory, lifespan, middleware, exception handlers
├── api/                 routers and shared dependencies (thin)
├── core/                config, exceptions, logging, security, feature flags
├── db/                  declarative base, session, conventions, migrations
├── modules/             domain modules (users, organisations, teams, ...)
├── integrations/        provider adapters
├── storage/             provider-neutral storage interface + adapters
├── email/               email interface + adapters
├── events/              domain events / outbox
├── workers/             Dramatiq tasks
└── observability/       logging, tracing, metrics
```

Each domain module normally uses: `models.py`, `schemas.py`, `router.py`, `service.py`, `queries.py` (optional), `permissions.py`, `tests/`. A mandatory repository class is not part of the architecture.

## Request flow

```text
HTTP Request
    │
    ▼
Router
    │
    ▼
Service
    │
    ▼
SQLAlchemy / queries.py
    │
    ▼
PostgreSQL
```

- **Routers** are thin: HTTP parsing, dependency injection, response codes, request/response schemas, authentication context, and calling services.
- **Services** own business rules, permission enforcement, transaction boundaries, orchestration, domain state changes, and audit/event creation.
- **SQLAlchemy** may be used directly inside services for simple operations; reusable or complex queries live in `queries.py`.

## Identity flow and request context (v0.2)

WorkOS owns login and sessions; the application owns the internal user record, organisation membership, roles, and permissions. Every protected request resolves through the shared dependencies in `app/api/dependencies.py`:

```text
Authorization: Bearer <session-token>
        │
        ▼
validate signature / issuer / audience / expiry      → 401 invalid_session
        │
        ▼
map workos_user_id to internal user (provision on first login)
        │   (email/name from the WorkOS profile, never the client)
        ▼
user disabled?                                         → 403 user_disabled
        │
        ▼
X-Org-Id header (tenant-scoped routes only)
        │   missing / malformed                        → 400
        ▼
active membership for (user, org)?                     → 403 not_a_member
        │
        ▼
require_permission(code): code in the membership's
role bundles? (default deny)                           → 403 permission_denied
        │
        ▼
service call, org-scoped queries (queries.py) — resources outside
the caller's organisation behave as not found (404)
```

- `GET /api/v1/me` and `POST /api/v1/organisations` are the two bootstrap endpoints: they require only a valid session, because the caller is not yet a member of any organisation. Every other `/api/v1` route requires both the Bearer token and the `X-Org-Id` header.
- The organisation id is always derived from the validated header context, never from a request body; request schemas use `extra="forbid"` so identity fields cannot be smuggled in.
- The security properties of this flow are enforced by the mandatory reusable security suite in `backend/tests/test_security_suite.py` (blueprint §31), which parametrises over the whole protected surface.

## Frontend structure

Vue 3 + TypeScript SPA. Directory layout, conventions, and state-management split follow blueprint §14; UI follows the design system in blueprint §16. API types are generated, never hand-written (blueprint §15).

## Cross-cutting conventions

- **API**: REST, JSON, OpenAPI, `/api/v1` prefix (see `API_CONVENTIONS.md`).
- **Errors**: one structured error format with `code`, `message`, `details`, `request_id` (see `API_CONVENTIONS.md`).
- **Database**: SQLAlchemy 2 models + Pydantic 2 schemas, Alembic migrations, shared naming/timestamp/UUIDv7 conventions (blueprint §7, §10).
- **Configuration**: typed `pydantic-settings` model, fail-fast on invalid production config.
- **Observability**: structured JSON logging with request IDs (blueprint §28).
- **Security**: baseline controls in `SECURITY.md`, aligned with OWASP ASVS Level 2.
