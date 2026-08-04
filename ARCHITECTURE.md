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

## Frontend structure

Vue 3 + TypeScript SPA. Directory layout, conventions, and state-management split follow blueprint §14; UI follows the design system in blueprint §16. API types are generated, never hand-written (blueprint §15).

## Cross-cutting conventions

- **API**: REST, JSON, OpenAPI, `/api/v1` prefix (see `API_CONVENTIONS.md`).
- **Errors**: one structured error format with `code`, `message`, `details`, `request_id` (see `API_CONVENTIONS.md`).
- **Database**: SQLAlchemy 2 models + Pydantic 2 schemas, Alembic migrations, shared naming/timestamp/UUIDv7 conventions (blueprint §7, §10).
- **Configuration**: typed `pydantic-settings` model, fail-fast on invalid production config.
- **Observability**: structured JSON logging with request IDs (blueprint §28).
- **Security**: baseline controls in `SECURITY.md`, aligned with OWASP ASVS Level 2.
