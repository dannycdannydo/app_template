# Internal Custom Application Starter
## Architecture Blueprint — Version 2

## 1. Purpose

This repository is intended to become a reusable, production-quality starter for custom business applications, particularly bespoke software for UK commercial property companies.

The long-term goal is to support the following workflow:

```bash
git clone internal-app-template new-project
cd new-project
cp .env.example .env
make dev
```

Within minutes, a new project should have:

- authentication;
- users and organisations;
- permissions;
- database and migrations;
- background jobs;
- storage;
- email and notifications;
- audit logging;
- observability;
- frontend application shell;
- generated API client;
- testing;
- CI;
- local development;
- hybrid VPS deployment;
- managed cloud deployment.

The template should provide reusable infrastructure and architectural conventions only. Project-specific business functionality must be implemented as separate domain modules.

---

# 2. Architectural Principles

The template follows these principles:

1. Modular monolith by default.
2. Explicit boundaries over hidden abstraction.
3. PostgreSQL as the primary source of truth.
4. External authentication, internal authorisation.
5. Cloud-neutral application code.
6. One application build, multiple deployment profiles.
7. Long-running work belongs in background jobs.
8. Database models and API schemas are separate.
9. Backend-enforced tenant isolation.
10. Generated frontend API types.
11. Strong typing, testing and CI.
12. Coding agents follow documented conventions.
13. Infrastructure complexity is introduced only when justified.
14. The template remains small and opinionated.
15. Business-specific abstractions are not added prematurely.

---

# 3. Core Technology Stack

## Backend

- Python 3.13+
- FastAPI
- SQLAlchemy 2
- Pydantic 2
- Alembic
- PostgreSQL
- Dramatiq
- Redis
- `pydantic-settings`

## Frontend

- Vue 3
- TypeScript
- Vite
- Vue Router
- TanStack Vue Query
- Pinia
- Tailwind CSS
- shadcn-vue
- Reka UI
- Lucide icons
- Vitest
- Playwright

## Authentication

- WorkOS AuthKit

## Storage

Provider-neutral interface with adapters for:

- AWS S3 or S3-compatible storage
- Google Cloud Storage
- Azure Blob Storage
- MinIO for local development

## Tooling

Backend:

- `uv`
- Ruff
- Pyright
- pytest
- pre-commit

Frontend:

- pnpm
- ESLint
- Prettier
- `vue-tsc`
- Vitest
- Playwright

## CI/CD

- GitHub Actions
- container image registry
- immutable image tags based on Git commit SHA

---

# 4. System Shape

The default architecture is a modular monolith.

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

Microservices are not part of the default architecture.

A service may be extracted only when there is a demonstrated operational or organisational need.

---

# 5. Backend Project Structure

```text
backend/
├── app/
│   ├── main.py
│   ├── api/
│   │   └── dependencies.py
│   ├── core/
│   │   ├── config.py
│   │   ├── exceptions.py
│   │   ├── logging.py
│   │   ├── security.py
│   │   └── feature_flags.py
│   ├── db/
│   │   ├── base.py
│   │   ├── session.py
│   │   ├── conventions.py
│   │   └── migrations/
│   ├── modules/
│   │   ├── users/
│   │   ├── organisations/
│   │   ├── teams/
│   │   ├── permissions/
│   │   ├── files/
│   │   ├── jobs/
│   │   ├── notifications/
│   │   ├── audit/
│   │   └── domain-specific-modules/
│   ├── integrations/
│   ├── storage/
│   ├── email/
│   ├── events/
│   ├── workers/
│   ├── observability/
│   └── tests/
├── alembic/
├── pyproject.toml
├── uv.lock
└── Dockerfile
```

Each domain module should normally use:

```text
module/
├── models.py
├── schemas.py
├── router.py
├── service.py
├── queries.py
├── permissions.py
└── tests/
```

`queries.py` is optional and should contain reusable or complex SQLAlchemy queries.

A mandatory repository class is not part of the architecture.

---

# 6. Backend Request Flow

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

## Router responsibilities

Routers handle:

- HTTP request parsing;
- dependency injection;
- response codes;
- request and response schemas;
- authentication context;
- calling services.

Routers must remain thin.

## Service responsibilities

Services handle:

- business rules;
- permission enforcement;
- transaction boundaries;
- orchestration;
- domain state changes;
- audit and event creation;
- calls to internal provider interfaces.

## Query responsibilities

SQLAlchemy may be used directly inside services for simple operations.

Reusable or complex queries should be moved into `queries.py`.

This avoids unnecessary repository classes while keeping SQL out of route handlers.

---

# 7. Database Models and API Schemas

## Decision

Use:

- SQLAlchemy 2 for persistence models;
- Pydantic 2 for API and service schemas;
- Alembic for migrations.

SQLModel is not used in the default template.

## SQLAlchemy models

Represent:

- tables;
- columns;
- relationships;
- indexes;
- constraints;
- persistence behaviour.

## Pydantic schemas

Represent:

- API requests;
- API responses;
- service inputs;
- import rows;
- integration payloads;
- internal DTOs.

Typical schemas:

```text
PropertyCreate
PropertyUpdate
PropertyListItem
PropertyDetail
PropertyResponse
```

## Rules

- ORM models must never be used directly as API request models.
- Every public endpoint must declare an explicit response schema.
- Server-controlled fields must not be accepted from request bodies.
- API and database schema changes are separate decisions.
- ORM objects must not be serialised using `__dict__`.
- Relationship loading must be deliberate.

---

# 8. Authentication with WorkOS

## Responsibility split

WorkOS owns:

- login;
- passwords;
- passwordless authentication;
- Google and Microsoft login;
- enterprise SSO;
- MFA;
- email verification;
- session management;
- identity-provider integrations.

The application owns:

- internal user records;
- organisations;
- memberships;
- teams;
- roles;
- permissions;
- feature access;
- audit history.

## Identity flow

```text
WorkOS identity
      │
      ▼
Internal user
      │
      ▼
Organisation membership
      │
      ▼
Role and permissions
```

The application should store the WorkOS user identifier, not passwords.

## Backend rules

- Validate token signature, issuer, audience and expiry.
- Never trust identity fields submitted by the frontend.
- Disabled users must be blocked even with an otherwise valid session.
- Session and webhook validation must be centralised.
- Authentication is not authorisation.

---

# 9. Organisations, Teams and Permissions

## Organisation

An organisation is the primary data-isolation and security boundary.

Most custom apps may contain only one organisation.

Supporting multiple organisations remains useful for:

- group companies;
- consultants;
- future productisation;
- separate client entities;
- cross-company administration.

## Team

A team is an optional subdivision within an organisation.

Examples:

- investment;
- property management;
- finance;
- regional office.

Teams are not the primary tenant boundary.

## Core tables

```text
users
organisations
organisation_memberships
teams
team_memberships
roles
permissions
role_permissions
membership_roles
```

## Default roles

```text
owner
administrator
manager
member
viewer
```

Roles are permission bundles.

Example permissions:

```text
properties.read
properties.create
properties.update
properties.delete

documents.read
documents.upload
documents.delete

users.invite
users.manage_roles

organisation.manage
```

## Rules

- Default deny.
- Backend permissions are authoritative.
- Frontend visibility is only a UX aid.
- Organisation IDs are derived from validated context where possible.
- Cross-organisation tests are mandatory.
- Team-specific permissions are added only when required.

---

# 10. Database Conventions

## Identifiers

Use UUIDv7 for externally visible primary keys.

## Timestamps

Use timezone-aware UTC datetimes.

Typical fields:

```text
created_at
updated_at
deleted_at
```

Calendar concepts such as lease expiry use `date`, not `datetime`.

## Money and numeric values

Use PostgreSQL `NUMERIC` and Python `Decimal`.

Do not use floating-point types for:

- prices;
- rents;
- yields;
- percentages;
- valuation results.

Store percentages as decimal fractions:

```text
5.25% = 0.0525
```

## Soft deletion

Use selectively for important business records.

Typical fields:

```text
deleted_at
deleted_by_user_id
```

Do not add soft deletion to every table automatically.

## Optimistic concurrency

Use an integer `version` field on collaboratively edited records where overwrite conflicts matter.

Stale updates should return `409 Conflict`.

## Constraints

Important invariants should be enforced in PostgreSQL where possible.

## JSONB

Use for:

- external payload snapshots;
- feature configuration;
- audit metadata;
- flexible import metadata.

Do not use JSONB instead of relational design.

## Naming

- snake_case database names;
- plural table names;
- singular Python model names;
- foreign keys named `<entity>_id`.

---

# 11. Transactions

The service layer owns transaction boundaries.

A business operation should succeed or fail atomically.

Example:

```text
Create valuation
Create assumptions
Write audit event
Create outbox event
Commit
```

Routers should not manage commits directly.

---

# 12. API Design

## Style

- REST
- JSON
- OpenAPI
- `/api/v1`

GraphQL is not part of the default architecture.

## Versioning

The bundled frontend and backend may evolve together.

Backward compatibility is required for:

- external clients;
- published APIs;
- integrations;
- webhooks;
- retained domain events.

New API versions are introduced only for genuine breaking contracts that must coexist.

## Pagination

Default format:

```text
?page=1&page_size=50
```

Response:

```json
{
  "items": [],
  "page": 1,
  "page_size": 50,
  "total": 0
}
```

Use cursor pagination only where justified.

## Filtering and sorting

Example:

```text
?search=manchester
&status=active
&sort=-created_at
&page=1
&page_size=50
```

Only allow approved filter and sort fields.

## Search endpoints

Use domain-specific endpoints.

For large structured searches, use read-only POST endpoints such as:

```text
POST /api/v1/properties/search
```

---

# 13. API Errors

Use one structured error format.

```json
{
  "code": "property_not_found",
  "message": "The property could not be found.",
  "details": null,
  "request_id": "..."
}
```

Validation example:

```json
{
  "code": "validation_error",
  "message": "The request contains invalid data.",
  "details": [
    {
      "field": "asking_price",
      "message": "Value must be greater than or equal to zero."
    }
  ],
  "request_id": "..."
}
```

## Exception rules

Services raise domain exceptions.

Central FastAPI handlers translate them to HTTP.

Typical mappings:

```text
NotFoundError        → 404
PermissionDenied     → 403
ConflictError        → 409
ValidationError      → 422
RateLimitExceeded    → 429
ExternalServiceError → 502 or 503
```

Unexpected exceptions return a safe generic message and are recorded in Sentry.

---

# 14. Frontend Architecture

```text
frontend/
├── src/
│   ├── api/
│   │   ├── generated/
│   │   └── client.ts
│   ├── components/
│   │   ├── ui/
│   │   └── application/
│   ├── composables/
│   ├── queries/
│   ├── stores/
│   ├── router/
│   ├── views/
│   ├── layouts/
│   ├── features/
│   └── tests/
├── package.json
├── pnpm-lock.yaml
└── Dockerfile
```

## Vue conventions

- Vue 3 Composition API
- `<script setup lang="ts">`
- strict TypeScript
- Vue Router
- feature-oriented modules
- no API calls directly inside visual components

## State management

TanStack Vue Query owns server state:

- fetching;
- caching;
- pagination;
- refetching;
- mutations;
- loading and error state;
- invalidation.

Pinia owns client state:

- sidebar state;
- selected organisation;
- UI preferences;
- temporary wizard state;
- unsaved local workflows.

Pinia must not become a manual backend-data cache.

---

# 15. Generated API Client

FastAPI is the source of truth.

Flow:

```text
Pydantic schemas and routes
        │
        ▼
OpenAPI
        │
        ▼
Generated TypeScript client
        │
        ▼
TanStack Query composables
        │
        ▼
Vue components
```

Use:

- `openapi-typescript`
- `openapi-fetch`

Rules:

- Never hand-write duplicate frontend API interfaces.
- Generated files must not be manually edited.
- CI regenerates the client and fails on drift.
- API changes must include regenerated client output.

---

# 16. UI and Design System

Use:

- Tailwind CSS
- shadcn-vue
- Reka UI
- Lucide icons

Components copied through shadcn-vue become owned application code.

They are updated intentionally rather than automatically.

## Rules

- Use semantic design tokens.
- Avoid arbitrary colours and spacing.
- Build reusable application components above raw UI primitives.
- Accessibility behaviour should come from tested primitives.
- Do not build custom dialog, menu or focus-management logic without need.

## Data grids

Default:

```text
ordinary tables → shadcn-vue + TanStack Table
advanced grids   → project-specific dedicated library
```

Possible advanced tools:

- AG Grid
- Handsontable

Dedicated grids should be wrapped behind internal components.

---

# 17. Storage Architecture

## Decision

Use a provider-neutral object-storage interface.

```text
ObjectStorage
├── S3Storage
├── GoogleCloudStorage
├── AzureBlobStorage
└── FakeObjectStorage
```

Application modules must not import provider SDKs directly.

## File metadata

Store in PostgreSQL:

```text
files
-----
id
organisation_id
storage_provider
storage_bucket
object_key
original_filename
content_type
size_bytes
checksum
status
created_by_user_id
created_at
deleted_at
```

Do not store provider URLs as the primary reference.

## Object keys

Server-generated example:

```text
organisations/{organisation_id}/documents/{file_id}/original.pdf
```

## Direct upload flow

```text
Vue requests upload
FastAPI authorises
FastAPI creates pending file record
Storage adapter creates signed URL
Browser uploads directly
Browser confirms completion
FastAPI verifies object
Worker processes file
File becomes ready
```

## File lifecycle

```text
pending
uploaded
processing
ready
failed
quarantined
deleted
```

## Security

- private buckets or containers;
- temporary signed URLs;
- backend-controlled keys;
- MIME and size validation;
- malware scanning hook;
- no public read;
- audit deletion;
- client submits internal file ID, not arbitrary object path.

## Initial implementation strategy

Ship:

- complete S3-compatible adapter;
- MinIO local support;
- provider interface;
- fake test adapter.

Add Azure and GCS adapters when first required, while maintaining the interface contract.

---

# 18. Background Jobs

Use:

- Dramatiq
- Redis
- PostgreSQL job records

Flow:

```text
FastAPI request
      │
      ▼
Create durable job record
      │
      ▼
Enqueue task in Redis
      │
      ▼
Dramatiq worker
      │
      ▼
Update durable job record
```

## Job table

```text
jobs
----
id
organisation_id
job_type
status
progress
input_reference
result_reference
error_code
error_message
attempt_count
created_by_user_id
created_at
started_at
completed_at
```

## Statuses

```text
queued
running
succeeded
failed
cancelled
```

## Rules

- Long-running work never runs directly in HTTP handlers.
- Workers must be idempotent where practical.
- Retry transient errors.
- Do not retry permanent validation errors indefinitely.
- Heavy workloads may use separate queues.
- Worker concurrency must be configurable.

Example queues:

```text
default
documents
integrations
ai
emails
```

---

# 19. Domain Events and Transactional Outbox

Use lightweight domain events for secondary consequences.

Example:

```text
valuation.approved
├── audit handler
├── notification handler
├── email handler
└── integration handler
```

Core business logic remains synchronous and explicit.

## Event naming

Use stable past-tense names:

```text
property.created
document.uploaded
valuation.approved
membership.role_changed
import.completed
```

## Outbox

Use the transactional outbox where missed delivery would matter.

```text
outbox_events
-------------
id
organisation_id
event_type
event_version
payload_json
status
created_at
processed_at
attempt_count
last_error
```

The business change and outbox event are written in the same PostgreSQL transaction.

Redis is the execution queue; PostgreSQL provides durability.

---

# 20. Notifications and Email

## Architecture

```text
Business event
      │
      ▼
Notification service
      ├── in-app notification
      └── email delivery
```

## Email

Use a provider-neutral interface.

Possible adapters:

- Postmark
- Resend
- SendGrid
- AWS SES
- Microsoft Graph
- SMTP

WorkOS handles authentication-related email.

Application email is sent through background jobs.

## Notifications table

```text
notifications
-------------
id
organisation_id
user_id
type
title
body
resource_type
resource_id
read_at
created_at
```

## Delivery tracking

```text
notification_deliveries
-----------------------
notification_id
channel
recipient
status
provider_message_id
attempt_count
sent_at
```

Deliveries must be idempotent.

Real-time notification delivery is not required by default.

---

# 21. Imports

Use staged imports.

```text
Upload
  │
  ▼
Create import job
  │
  ▼
Parse to staging rows
  │
  ▼
Validate and normalise
  │
  ▼
Preview and review
  │
  ▼
Commit accepted records
  │
  ▼
Audit result
```

## Tables

```text
import_jobs
import_rows
import_errors
```

## Statuses

```text
uploaded
parsing
validating
awaiting_review
committing
completed
partially_completed
failed
cancelled
```

## Requirements

- row-level validation;
- warnings distinct from errors;
- duplicate detection;
- template-based imports;
- manual column mapping;
- saved mappings;
- idempotency;
- stable external IDs where available;
- no direct write into production tables before validation.

---

# 22. Exports

Large exports run as background jobs.

```text
User requests export
Worker generates file
File stored privately
User notified
Temporary download URL issued
```

Record:

- requesting user;
- organisation;
- filters;
- row count;
- output file;
- expiry;
- status.

Sensitive exports must be audit logged.

---

# 23. External Integrations

Provider-specific adapters live under:

```text
app/integrations/
```

Each adapter owns:

- authentication;
- request construction;
- pagination;
- rate limits;
- retries;
- provider payload models;
- webhook verification;
- error translation.

Business services use internal interfaces, not raw HTTP calls.

## Integration records

Possible tables:

```text
integration_events
external_entities
sync_mappings
sync_jobs
sync_conflicts
webhook_events
```

## Sync principles

Two-way sync requires explicit rules for:

- source of truth;
- field ownership;
- conflict resolution;
- deletion behaviour;
- retry behaviour;
- reconciliation.

Use:

```text
webhook for speed
scheduled reconciliation for correctness
```

---

# 24. Search

PostgreSQL is the default search platform.

Use:

- indexed SQL for filtering and ranges;
- `pg_trgm` for fuzzy matching;
- PostgreSQL full-text search;
- `pgvector` for semantic search;
- PostGIS for geographic search.

## Rules

- Search is always tenant-scoped.
- Vector search complements structured filters.
- Dedicated search infrastructure is introduced only when measured scale or relevance requires it.
- PostgreSQL remains the source of truth.
- External indexes are rebuildable projections.

---

# 25. Caching

Redis is not a blanket application cache.

Default reads use:

- PostgreSQL;
- TanStack Query browser caching.

Redis may support:

- expensive aggregate caching;
- external API response caching;
- rate limiting;
- idempotency keys;
- distributed locks;
- temporary coordination;
- progress counters.

## Rules

- tenant-aware cache keys;
- short TTLs;
- explicit invalidation where required;
- versioned cache keys;
- no cached SQLAlchemy session objects;
- Redis cache failure should usually fall back to PostgreSQL.

Example key:

```text
organisation:{organisation_id}:dashboard:v1
```

---

# 26. Real-Time Updates

Default:

- standard HTTP;
- TanStack Query polling.

Use polling for:

- job status;
- import progress;
- export progress;
- document processing;
- integration sync state.

Use Server-Sent Events where one-way updates materially improve UX.

Use WebSockets only for genuinely bidirectional features such as:

- collaboration;
- live chat;
- shared editing;
- high-frequency interactive sessions.

---

# 27. Feature Flags and Configuration

## Configuration

Use one typed `pydantic-settings` model.

No scattered `os.getenv()` calls.

Example settings:

```text
APP_ENV
DATABASE_URL
REDIS_URL
WORKOS_API_KEY
STORAGE_BACKEND
EMAIL_BACKEND
SENTRY_DSN
FRONTEND_URL
```

The application must fail fast on invalid production configuration.

## Feature flags

Support:

1. deployment-level flags;
2. optional organisation-level database flags.

Example table:

```text
organisation_features
---------------------
organisation_id
feature_key
enabled
configuration_json
```

Feature state must be enforced by the backend.

---

# 28. Observability

Use:

- structured JSON logging;
- request IDs;
- Sentry;
- health and readiness endpoints;
- basic metrics;
- uptime monitoring;
- worker failure visibility.

## Standard endpoints

```text
/health
/ready
/metrics
```

## Logging context

```text
request_id
user_id
organisation_id
route
job_id
resource_id
event
```

Never log:

- passwords;
- tokens;
- authorisation headers;
- signed URLs;
- full database connection strings;
- complete document contents by default.

---

# 29. Audit Logging

Technical logs and business audit events are separate.

```text
audit_events
------------
id
organisation_id
actor_user_id
action
resource_type
resource_id
metadata
created_at
```

Audit examples:

```text
valuation.approved
document.deleted
membership.role_changed
user.invited
export.generated
```

Audit events are append-only from the application's point of view.

---

# 30. Security Baseline

Use OWASP ASVS Level 2 as the practical baseline.

## Required controls

- WorkOS session validation;
- default-deny authorisation;
- tenant-scoped queries;
- explicit CORS allowlist;
- CSRF protection where cookie authentication requires it;
- input limits;
- rate limiting;
- secure headers;
- private storage;
- upload scanning hook;
- restricted external URL fetching;
- webhook signature verification;
- non-public PostgreSQL and Redis;
- least-privilege database credentials;
- encrypted backups;
- secret scanning;
- dependency scanning;
- container scanning;
- non-root containers;
- safe error messages.

## File security

Uploaded files are untrusted.

Controls include:

- MIME and extension validation;
- page and size limits;
- decompression-bomb protections;
- worker isolation;
- no execution of content;
- quarantine state;
- malware scanning hook.

## SSRF

User-supplied URLs must not access:

- localhost;
- loopback;
- private network ranges;
- cloud metadata endpoints;
- internal service names.

## Administrative access

Cross-organisation support or impersonation must be:

- explicit;
- limited;
- visible;
- fully audited.

No hidden universal bypass.

---

# 31. Testing Strategy

## Unit tests

Use for pure logic:

- calculations;
- validation helpers;
- permission decisions;
- transformations;
- date logic.

## Integration tests

Run against real PostgreSQL and test:

- FastAPI routes;
- WorkOS-authenticated context;
- organisation isolation;
- permissions;
- SQLAlchemy behaviour;
- migrations;
- background-job creation;
- audit events.

Integration tests are the most important layer.

## End-to-end tests

Use Playwright for critical journeys:

- sign in;
- upload;
- create or update core records;
- invite users;
- process documents;
- approve workflows.

Do not test every visual detail through Playwright.

## Mandatory reusable security tests

- unauthenticated requests rejected;
- invalid sessions rejected;
- cross-organisation access denied;
- viewer writes denied;
- disabled users denied;
- oversized uploads rejected;
- invalid webhook signatures rejected;
- stack traces not exposed.

---

# 32. Developer Tooling

## Backend

```text
uv
Ruff
Pyright
pytest
pre-commit
```

## Frontend

```text
pnpm
ESLint
Prettier
vue-tsc
Vitest
Playwright
```

## Shared commands

Provide a root Makefile:

```bash
make dev
make test
make lint
make typecheck
make format
make migrate
make generate-client
make check
```

`make check` runs the complete local quality gate.

## Dependency rules

- one package manager per ecosystem;
- lock files committed;
- no dependency added without justification;
- security scanning;
- deliberate upgrades;
- agents must not add packages merely to avoid simple code.

---

# 33. Coding-Agent Governance

The repository must contain:

```text
AGENTS.md
ARCHITECTURE.md
API_CONVENTIONS.md
SECURITY.md
CONTRIBUTING.md
docs/decisions/
```

## Mandatory agent rules

- Read architecture documentation before structural changes.
- Follow existing module patterns.
- Routers remain thin.
- Business logic belongs in services.
- Complex or reused SQL belongs in `queries.py`.
- ORM models are never API request models.
- Every endpoint declares an explicit response schema.
- Organisation IDs come from validated context where possible.
- Every database change includes an Alembic migration.
- Long-running work uses Dramatiq.
- Provider SDKs stay behind adapters.
- Frontend API types are generated.
- Tests accompany behavioural changes.
- Do not weaken linting, typing or tests.
- Do not refactor unrelated code without a clear reason.
- Do not add dependencies without documenting why.

## Human review required

- authentication changes;
- permission-model changes;
- tenant-isolation changes;
- destructive migrations;
- secret handling;
- public API breaks;
- infrastructure changes;
- backup and recovery changes;
- major dependency additions.

---

# 34. Architecture Decision Records

Use:

```text
docs/decisions/
├── 0001-use-workos.md
├── 0002-use-sqlalchemy-and-pydantic.md
├── 0003-use-vue.md
├── 0004-use-dramatiq.md
├── 0005-use-shadcn-vue.md
├── 0006-provider-neutral-storage.md
└── 0007-two-deployment-profiles.md
```

Each ADR records:

- context;
- options considered;
- decision;
- consequences;
- status.

Foundational changes must update or supersede the relevant ADR.

---

# 35. Deployment Profiles

The template supports two production deployment models.

The application code and container image remain the same.

## 35.1 Hybrid VPS Profile

The VPS runs:

```text
Caddy
Vue static frontend
FastAPI
Dramatiq worker
Redis
```

External services provide:

```text
Managed PostgreSQL
Cloud object storage
WorkOS
Transactional email
Monitoring
```

This is the preferred low-cost production profile.

### Advantages

- low monthly cost;
- simple operation;
- professional architecture;
- managed durable state;
- easy migration to fully managed infrastructure.

### Production Compose

Use Docker Compose for:

- API;
- worker;
- Redis;
- Caddy.

The API and worker use the same backend image with different commands.

### Deployment flow

```text
Run CI
Build immutable image
Push image
Build Vue assets
SSH to VPS
Pull image
Run Alembic migration
Restart services
Run health check
```

### Mandatory protections

- firewall;
- SSH keys only;
- non-public Redis;
- automatic security updates;
- monitoring;
- disk alerts;
- container resource limits;
- documented rollback;
- off-site configuration backups.

Because PostgreSQL and object storage are external, application data does not depend entirely on one VPS disk.

## 35.2 Fully Managed Profile

Use managed services for:

```text
API container
Worker container
PostgreSQL
Redis
Object storage
Static frontend/CDN
Monitoring
```

Possible platforms:

- Azure Container Apps;
- AWS ECS/Fargate;
- Google Cloud Run;
- equivalent managed container services.

Provider-specific infrastructure files live under:

```text
deploy/managed/
```

The same immutable backend image is used.

### Initial implementation

The starter should contain one complete managed reference deployment, likely Azure because many UK corporate clients use Microsoft infrastructure.

AWS and GCP implementations should be added when a real project requires them.

---

# 36. Docker and Build Strategy

Use:

```text
One backend Dockerfile
One optional frontend Dockerfile
One backend image
Different runtime commands
```

API command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Worker command:

```bash
dramatiq app.workers
```

The frontend is primarily a static build artefact:

```text
Vue source
   │
   ▼
dist/
   ├── served by Caddy on VPS
   └── uploaded to static hosting/CDN when managed
```

## Compose files

```text
deploy/compose/
├── compose.yml
├── compose.local.yml
└── compose.hybrid-vps.yml
```

Do not use production Compose to describe fully managed cloud infrastructure.

## Local development model

The blueprint is intentionally silent on whether application code runs natively or inside containers during local development. That decision is fixed by **ADR-0008** (`docs/decisions/0008-local-development-model.md`): day-to-day development runs Vue, FastAPI, and the worker natively with PostgreSQL and Redis in Docker (`make dev`), while a full-container path (`make dev-docker`) exists for CI parity and onboarding.

---

# 37. CI/CD

Use one shared CI workflow and separate deployment workflows.

```text
.github/workflows/
├── ci.yml
├── deploy-hybrid-vps.yml
└── deploy-managed-azure.yml
```

## CI checks

- backend formatting;
- backend linting;
- backend type checks;
- backend tests;
- frontend formatting;
- frontend linting;
- Vue type checks;
- frontend tests;
- Playwright smoke tests;
- generated-client drift;
- migration validity;
- secret scanning;
- dependency scanning;
- container build;
- container scanning.

## Deployment

Build one immutable image:

```text
ghcr.io/company/application:<git-sha>
```

The same image must be deployable to staging and production.

Alembic migrations run as a deliberate release job, not automatically on every API startup.

---

# 38. Environment Separation

Support:

```text
local
staging
production
```

Each environment has separate:

- database;
- Redis;
- storage bucket or container;
- WorkOS environment;
- secrets;
- frontend URL;
- API URL.

Staging must never use production data or credentials.

---

# 39. Backup and Recovery

For fully managed infrastructure, use provider-native backups and recovery.

For the hybrid profile, managed PostgreSQL provides durable database backup.

The project must still document:

- database restore;
- object-storage recovery;
- secret recovery;
- deployment rollback;
- lost VPS replacement;
- environment recreation.

Backups are not considered valid until restore procedures have been tested.

---

# 40. Template Repository Structure

```text
project/
├── backend/
├── frontend/
├── deploy/
│   ├── compose/
│   ├── hybrid-vps/
│   └── managed/
├── docs/
│   ├── decisions/
│   ├── upgrades/
│   ├── architecture/
│   └── operations/
├── .github/
│   └── workflows/
├── AGENTS.md
├── ARCHITECTURE.md
├── API_CONVENTIONS.md
├── SECURITY.md
├── CONTRIBUTING.md
├── Makefile
├── .env.example
└── README.md
```

---

# 41. Template Lifecycle

Maintain a master template repository with tagged releases.

```text
v1.0.0
v1.1.0
v2.0.0
```

New applications begin from a specific template release and then become independent repositories.

Do not keep all projects permanently coupled as Git forks.

## Version recording

Each application should record:

```toml
[tool.project-template]
name = "internal-app-template"
version = "2.0.0"
```

## Upgrade guides

```text
docs/upgrades/
├── 1.0-to-1.1.md
├── 1.1-to-1.2.md
└── 1.x-to-2.0.md
```

Each guide records:

- changed files;
- new dependencies;
- configuration changes;
- migrations;
- security implications;
- manual adoption steps.

## Shared packages

Do not extract shared internal packages prematurely.

Use the rule of three:

> Extract a shared package only when at least three applications need substantially the same implementation and coordinated updates are clearly valuable.

Possible future packages:

```text
company-auth
company-storage
company-audit
company-observability
company-testing
```

---

# 42. Template Validation

The template itself is treated as a maintained software product.

CI should test:

```text
create fresh project
install dependencies
start local services
run migrations
generate API client
run backend tests
run frontend tests
build frontend
build containers
```

The template should include a small example module to demonstrate conventions.

---

# 43. What Must Not Be in the Base Template

Do not include a speculative universal commercial-property model.

Project-specific concerns include:

- properties;
- leases;
- tenancies;
- valuations;
- inspections;
- service charges;
- agency pipelines;
- CRM workflows;
- property-management integrations.

These belong in individual applications.

The shared template should contain platform infrastructure only.

---

# 44. Initial Implementation Order

Recommended implementation sequence:

1. Repository and tooling setup
2. Docker local development
3. FastAPI application shell
4. SQLAlchemy, PostgreSQL and Alembic
5. WorkOS authentication
6. Users, organisations and memberships
7. Permissions
8. Vue application shell
9. Generated OpenAPI client
10. shadcn-vue design system
11. Storage interface and S3-compatible adapter
12. File metadata and signed uploads
13. Dramatiq and job records
14. Audit logging
15. Notifications and email abstraction
16. Structured logging and Sentry
17. Feature flags and typed configuration
18. Import and export framework
19. Domain events and transactional outbox
20. Security baseline and reusable security tests
21. Hybrid VPS deployment
22. Managed Azure reference deployment
23. Fresh-clone CI validation
24. Tag first stable release

---

# 45. Definition of Version 1 Readiness

The starter is ready for real projects when a fresh clone can:

- start locally with one command;
- authenticate through WorkOS;
- create an organisation and membership;
- enforce a permission;
- run PostgreSQL migrations;
- upload a file using a signed URL;
- enqueue and complete a Dramatiq job;
- send a test notification;
- create an audit event;
- expose health and readiness endpoints;
- regenerate the Vue API client;
- pass all lint, type and test checks;
- deploy through the hybrid VPS profile;
- deploy through the managed reference profile;
- restore from documented recovery procedures.

---

# 46. Final Architectural Decision

The internal starter will be a versioned, modular-monolith template using FastAPI, SQLAlchemy 2, Pydantic 2, PostgreSQL, Vue 3, WorkOS, Dramatiq, Redis, Tailwind and shadcn-vue.

It will provide cloud-neutral application interfaces, strict tenant isolation, generated API contracts, strong testing and explicit coding-agent governance.

It will support two first-class production deployment profiles:

1. **Hybrid VPS** for cost-efficient custom applications.
2. **Fully managed cloud** for larger, more critical or contractually demanding systems.

The goal is not maximum architectural sophistication.

The goal is a stable, secure and repeatable foundation from which high-quality custom business applications can be produced rapidly and consistently.
