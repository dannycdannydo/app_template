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

## AI / LLM application layer (v0.7)

The template ships a **provider-neutral AI application layer** (ADR-0017) as a
platform capability: a feature calls `AIService.execute(task=...)` and never
imports an LLM SDK, selects a model, formats a provider request, parses
provider JSON, calculates cost or writes retry logic. Task/prompt/model
registries, a deterministic capability/cost router, typed provider adapters
(OpenAI, Anthropic, DeepSeek, Azure OpenAI, Vertex AI Gemini, local
OpenAI-compatible), structured Pydantic outputs, organisation controls and
usage/cost/audit records live under `app/ai/`. Google Gemini is reached
through **Vertex AI only** (ADR-0018) — no Gemini Developer API / AI Studio
path exists. The layer is deliberately not an agent or retrieval framework;
those sit above or beside it in derived applications.

Bounded file input is a first-class v0.7 attachment contract: a feature
supplies a private storage reference, the service/job boundary resolves it
server-side into a provider-neutral `Attachment` (validated display name, MIME
type, bytes, SHA-256 digest; template caps 5 MB per file, 10 MB combined), and
capable adapters map it to their native inline request form. Attachments are
capability-gated via the `documents` model capability and per-model inline
ceilings; bytes never enter the database, job broker, logs or audit metadata,
and no adapter receives a storage credential or signed URL. Keep-flow source
objects stay feature-owned; temporary analyse-only objects use the
organisation-scoped AI scratch namespace governed by the v0.7 retention job.
Provider regions are explicit, validated configuration (OpenAI region,
Anthropic inference geography, Azure endpoint, Vertex location) and fallback
never changes region implicitly. Provider-hosted uploads, provider file
identifiers, `gs://` references and URL inputs are deferred to v0.8
(`plans/AI_LARGE_ATTACHMENTS_V0_8_PLAN.md`); inline is the only v0.7 transfer
mode and oversized inputs fail before dispatch.

Large-file and provider-reference input (v0.8, ADR-0017 amendment) adds four
**provider-neutral transfer modes** — `inline`, `provider_upload`,
`managed_signed_url` and `storage_reference` — behind the same `AIService`
entry point: a feature still supplies only a task name and a private storage
reference, and the caller can never request or override a mode (Scope §2.2).
Inline is eligible only through a 5,000,000-byte aggregate raw threshold;
above it a non-inline mode is eligible only when the source lifecycle, task
definition, organisation policy, model/provider capability and deployment
configuration all allow it, and the service fails before external transfer
when no permitted mode is eligible. Provider copies and GCS staging objects
are AI-owned derivatives: deletion never deletes the feature-owned source.
Provider-hosted file identifiers are reusable only within retries of one
logical execution. Managed signed URLs are exact-object, read-only,
short-lived (900 s default, 1,800 s maximum) bearer capabilities minted just
before dispatch and never returned, persisted, audited or logged; caller-
supplied HTTP(S) URLs remain prohibited. The 5,000,000-byte inline threshold
and the 50,000,000-byte PDF ceiling are reviewed template constants; provider
ceilings always win. The re-verified provider contracts live in
`app/ai/contracts/providers.yaml` and fail fast on inconsistent declarations.

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
   ├── AI / LLM providers (app/ai)
   └── External integrations
```

Microservices are not part of the default architecture.

A service may be extracted only when there is a demonstrated operational or organisational need.

The AI layer (v0.7, ADR-0017) is a platform package inside the monolith: it
plugs into the same Postgres, Redis/Dramatiq, audit and observability
foundations and exposes `AIService.execute(request: AIRequest) -> AIResult` as
its only application-facing entry point. Provider SDKs are confined to
`app/ai/providers/` (ADR-0017, ADR-0018); the modular-monolith and
no-direct-SDK rules are unchanged.

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
│   ├── ai/                (v0.7, ADR-0017: AI application layer)
│   ├── events/
│   ├── workers/
│   ├── observability/
│   └── tests/
├── alembic/
├── pyproject.toml
├── uv.lock
└── Dockerfile
```

The AI package (v0.7) follows the same provider-neutral shape as storage and
email: `app/ai/providers/` holds the `LLMProvider` contract and all provider
adapters; `app/ai/tasks/`, `app/ai/prompts/` and `app/ai/models/` hold the
checked-in registries; `app/ai/service.py` owns `AIService`; schemas and the
error taxonomy live in `app/ai/schemas.py` and `app/ai/errors.py`. No code
outside `app/ai/providers/` imports a provider SDK (ADR-0017, ADR-0018).

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
- Never trust identity fields submitted by the frontend. Identity fields,
  including `email_verified`, come from the validated WorkOS profile only.
- Disabled users must be blocked even with an otherwise valid session.
- Session and webhook validation must be centralised.
- Authentication is not authorisation.
- A configured `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` grants the `platform_admin`
  role once, on the first verified login of that exact WorkOS email, audited
  as `platform.bootstrap_granted`; a second login is a no-op (v0.4, Scope
  §6.4). The grant is a documented provisioning step, not a route a caller
  can invoke.

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
invitations
```

`organisations.workos_organisation_id` (nullable, unique) is the 1:1 mapping
to a WorkOS Organization when the application deliberately adopts WorkOS
Organizations for invitations (ADR-0013, v0.4). The internal organisation id
remains the primary key; the mapping is server-side only and never
client-writable.

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

## Platform Admin Centre (v0.4)

Cross-tenant administration lives in a dedicated **platform authorisation
plane**, never in the organisation plane and never behind a superuser flag:

- `platform_roles`, `platform_role_permissions` and `platform_memberships`
  mirror the organisation plane; a seeded `platform_admin` role carries the
  `platform.admin` permission code.
- `require_platform_permission(code)` resolves the caller through platform
  memberships and role bundles only; it never consults `X-Org-Id`. Platform
  routes under `/api/v1/platform/*` take no organisation header.
- The planes are orthogonal: an organisation `owner` without a platform
  membership is rejected on platform routes (`403 platform_admin_required`),
  and a platform admin without an organisation membership is rejected on
  organisation routes (`403 not_a_member`). No `is_admin`/superuser boolean
  exists anywhere.
- The platform plane is a separate plane, not a bypass: it grants explicit
  cross-tenant administration to configured platform admins while the
  organisation permission system keeps enforcing every org-scoped action.

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

## Interface implementation and permissions (v0.5)

The v0.5 release implements the interface contract and the direct upload flow:

- `app/storage/` ships the `ObjectStorage` interface (create upload/download
  URLs, head, delete, ensure bucket) with exactly two adapters: `S3Storage`
  (boto3, S3-compatible including MinIO) and `FakeObjectStorage` (in-memory,
  test suite only). `grep -rn "boto3" backend/app | grep -v "app/storage"` is
  empty — the SDK stays behind the adapter.
- Storage endpoints are gated by the existing organisation permission codes:
  `documents.upload` gates upload intent and completion, `documents.read`
  gates list/detail/download URL, `documents.delete` gates soft delete. Files
  and jobs are org-scoped resources like any other; there are no
  storage-specific roles or a generic `jobs.*` permission yet.
- Upload completion verifies the stored object with `head_object`: a missing
  object or a size mismatch fails the file (`failed`, or `quarantined` where
  a scanner would own the decision) and writes `file.upload_failed`. Request
  schemas use `extra="forbid"`, so the client can never supply an object key
  or a storage provider.
- File lifecycle events are audited append-only (`file.upload_started`,
  `file.uploaded`, `file.upload_failed`, `file.processing`, `file.ready`,
  `document.deleted`).
- Signed URLs presign against `STORAGE_PUBLIC_ENDPOINT_URL` when the browser
  cannot reach the API's storage host (e.g. the dev-docker stack) and fall
  back to `STORAGE_ENDPOINT_URL`.

## AI attachments and scratch lifecycle (v0.7)

The AI layer (v0.7, ADR-0017) consumes the same provider-neutral storage
interface and adds two ownership rules:

- **Keep-flow objects stay feature-owned.** When a feature passes a private
  storage reference in an `AIRequest`, the object remains owned by that
  feature and its lifecycle events; AI-side deletion or retention never
  deletes the feature object.
- **Temporary analyse-only objects use the organisation-scoped AI scratch
  namespace.** Objects a feature or the template's demonstration flow creates
  solely for AI analysis (for example a redacted extraction copy) live under
  an AI scratch key namespace and are governed by the v0.7 retention job,
  which applies the organisation AI retention policy and deletes expired
  scratch objects with audit events. Scratch is not a document store.

The AI layer resolves a storage reference into a bounded in-memory
`Attachment` (5 MB per file, 10 MB combined) at the service/job boundary;
`ai_requests`/`ai_outputs` persist the reference and SHA-256 digest, never the
bytes, and no adapter receives a signed URL or storage credential. Large-file
and provider-reference transfer modes are deferred to v0.8
(`plans/AI_LARGE_ATTACHMENTS_V0_8_PLAN.md`).

## AI large-file transfer modes (v0.8)

The v0.8 amendment (ADR-0017) adds three non-inline transfer modes without
changing the storage contract: provider uploads (transient sources),
just-in-time managed download URLs (retained private S3-compatible sources)
and private GCS staging references (Vertex `gs://`). Object metadata is
inspected and organisation ownership authorised before any mode is selected;
a 50 MB source is streamed through a bounded temporary-file seam and never
accumulated in Python memory (Scope §2.3). Managed URLs are HTTPS, read-only,
exact-object and short-lived (default 900 s, maximum 1,800 s), minted only
after ownership, immutable object identity, size, MIME and digest validation,
and their query strings are redacted from every log/error/telemetry boundary.
Vertex stages to a user-provisioned, non-public, same-region GCS staging
bucket referenced as `gs://...`; the application never creates or configures
the bucket and runs no GCS cleanup scheduler — the deployer-owned Object
Lifecycle Management rule (`age = 1`, asynchronous) is the cleanup backstop
(Scope §2.4/§2.5).

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
- AI work (v0.7) always runs through `AIService`, which keeps a bounded
  synchronous path for small tasks and an `ai.execute` job on the `ai` queue
  for document-scale work; the durable record-then-enqueue rules below apply
  unchanged, and worker logs/metrics carry `ai_request_id` alongside `job_id`.
- AI large-file transfers (v0.8) keep broker messages reference-only: retries
  re-head and re-digest the private source before reuse or upload, worker
  memory/concurrency stay bounded, and terminal outcomes trigger cleanup
  without duplicate output/cost records. A bounded Dramatiq reconciliation job
  covers only expired, orphaned or deletion-failed *provider-file* references;
  it never processes managed signed URLs, GCS staging objects or feature-owned
  sources (Scope §2.5, §6.7).

Example queues:

```text
default
documents
integrations
ai
emails
```

## Durable job records (v0.5)

The v0.5 release adds the durable `jobs` table and the record-then-enqueue
service:

- `create_and_enqueue` writes the durable row (status `queued`) in the
  request's transaction before the task is enqueued, so a row exists even if
  the broker is unreachable; the bounded retry policy self-heals a job that
  was never picked up.
- The worker writes status, `attempt_count`, `started_at`/`completed_at` and
  progress through the `mark_running`, `update_progress`, `succeed` and
  `fail` helpers; terminal states (`succeeded`/`failed`/`cancelled`) are
  never re-run.
- Retries are bounded: transient errors retry up to `MAX_ATTEMPTS` total
  attempts; permanent validation errors are not retried and fail the job
  immediately. Completion and permanent failure write `job.succeeded` /
  `job.failed` audit rows in the same transaction as the status transition.
- The job endpoints (`GET /api/v1/jobs`, `GET /api/v1/jobs/{job_id}`) are
  org-scoped and gated by the file module's `documents.read` code; a generic
  `jobs.*` permission is deferred until a second job producer appears (rule
  of three).
- Long-running work never runs in HTTP handlers; the worker is the same
  backend image running `uv run dramatiq app.workers` (see §36).

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

## AI providers (v0.7)

LLM provider adapters are **not** general integrations: they live under
`app/ai/providers/` and implement the typed `LLMProvider` contract
(ADR-0017) with normalised requests/responses and a retryability-aware error
taxonomy. Each adapter declares the capabilities it actually supports
(structured output, vision, tools, reasoning, documents, context window)
rather than pretending providers are interchangeable. Provider SDKs,
provider-specific HTTP formats, authentication, streaming mechanics, token
reporting and model quirks are confined to the adapters; a deterministic
`FakeLLMProvider` is the default test adapter. Google Gemini is Vertex AI
only (ADR-0018) and the local OpenAI-compatible adapter is never exposed to
browsers.

Bounded document input uses a provider-neutral `Attachment` (validated display
name, MIME type, bytes, SHA-256 digest; 5 MB per file, 10 MB combined) that
adapters map to their native inline request form. Routing is gated by the
`documents` capability and per-model inline ceilings declared in the model
registry; unsupported modalities, MIME types and sizes are rejected before
dispatch. OpenAI/Azure, Anthropic and Vertex adapters map supported
attachments inline; local adapters declare only the modalities they actually
support and DeepSeek rejects attachments. No adapter ever receives a private
storage credential or signed URL, and attachment bytes never reach the
database, job broker, logs or audit metadata.

Provider regions are explicit, validated deployment configuration: OpenAI
region and Anthropic inference geography are typed settings, Azure's region
is inherent in its configured resource endpoint, Vertex is pinned by its
location setting, DeepSeek documents that it offers no template-controlled
regional pinning, and local/fake providers inherit their operator-controlled
location. Defaults are honest for ordinary accounts and unsupported regions
fail configuration validation; fallback never changes region implicitly, and
routing metadata records the configured or observed region only where the
provider exposes it.

Large-file and provider-reference input (v0.8, ADR-0017 amendment) is an
extension of the same adapter boundary, never a new caller-facing surface:
`app/ai/transfer.py` owns the provider-neutral transfer contracts
(`inline`, `provider_upload`, `managed_signed_url`, `storage_reference`, the
5,000,000-byte aggregate inline threshold and the 50,000,000-byte PDF
ceiling) and `app/ai/staging.py` owns the provider-neutral staging/upload
seam (`TransferStore`) that concrete adapters implement — OpenAI and Anthropic
upload transient sources through their file APIs and ingest retained private
S3 sources through just-in-time managed URLs, Vertex stages to its configured
private same-region GCS bucket as a `gs://` reference, and Azure OpenAI,
DeepSeek and local fail closed for non-inline files in v0.8. Provider
capabilities and limits are re-verified against official documentation and
recorded in `app/ai/contracts/providers.yaml` (verification date, API/version,
retention/deletion, MIME/size limits, regional caveats); a loader validates
the fixture at startup/CI and the registry rejects task/model declarations the
contracts cannot support. No adapter ever receives a caller-supplied URL, and
managed signed URLs are never returned, persisted, audited or logged (Scope
§2.1–§2.5).

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

## AI configuration (v0.7)

Settings carry provider enablement and endpoint/project/deployment
identifiers only; keys, Azure credentials and Google credential material are
server-side secrets. Production fails fast when an enabled provider lacks its
required configuration, when a local provider endpoint is insecure or
publicly reachable, when the configured default/fallback model cannot
satisfy declared task requirements, or when a configured provider region is
unsupported. Provider regions are explicit, validated configuration: OpenAI
region and Anthropic inference geography are typed settings, Azure's region
is inherent in its configured resource endpoint, Vertex is pinned by its
location setting and DeepSeek documents no template-controlled pinning;
defaults are honest for ordinary accounts and fallback never changes region
implicitly (ADR-0017/0018, v0.7 Scope §6.1/§6.3). Organisation-level AI
policy (enabled, allowed providers/models, override, budget, retention) is
database-backed in `organisation_ai_settings`, default-off for new
organisations, and enforced inside `AIService`, never only in a router
(ADR-0017, v0.7 Scope §6.5). Attachment limits are configuration-backed
template constants (5 MB per file, 10 MB combined) and per-model inline
ceilings come from the model registry. Large-file transfer modes (v0.8,
ADR-0017 amendment) follow the same shape: the aggregate inline threshold
(5,000,000 bytes) and PDF ceiling (50,000,000 bytes) are reviewed template
constants, per-model per-mode MIME types and ceilings come from the model
registry validated against the re-verified provider contracts in
`app/ai/contracts/providers.yaml`, and typed deployment settings for enabled
non-inline modes (provider upload expiry, managed signed-URL TTL, Vertex
staging project/bucket/location) fail fast in production on incomplete or
incompatible configuration (Scope §2.2).

## Feature flags

Support:

1. deployment-level flags;
2. database-backed organisation-level flags (the default for
   platform-controlled organisation flags, v0.4).

Example table:

```text
organisation_features
---------------------
organisation_id
feature_key
enabled
configuration_json
```

Feature state must be enforced by the backend. For platform-controlled
organisation flags, management endpoints are platform-gated
(`require_platform_permission`, v0.4) and enforcement happens in services via
the `is_feature_enabled` helper — default off, never in routers.

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
- complete document contents by default;
- AI prompts, raw provider responses, provider keys/headers, attachment bytes
  or retained input/output content (v0.7, ADR-0017): logs, Sentry and audit
  metadata bind `ai_request_id`, task, provider/model and routing metadata,
  never content. Attachment bytes exist only in worker memory for one provider
  call and are never persisted, placed on the job broker, or logged.
- managed signed URLs and their query strings (v0.8, ADR-0017 amendment): a
  URL minted for dispatch is a temporary bearer capability that is never
  returned to the caller, persisted, audited or logged, and every
  log/error/telemetry boundary redacts it. Database rows and broker messages
  carry opaque external references (file ids or `gs://` URIs), never URLs.

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

Platform lifecycle events (v0.4) are audited through the same append-only
service:

```text
platform.bootstrap_granted
organisation.created
organisation.updated
invitation.sent
invitation.accepted
invitation.revoked
membership.role_changed
membership.suspended
membership.reactivated
membership.removed
feature_flag.changed
```

Audit events are append-only from the application's point of view.

AI large-file transfer lifecycle (v0.8, ADR-0017 amendment) joins the same
append-only service with low-cardinality events: mode selection, transfer
outcome/reuse, expiry, terminal deletion and reconciliation backlog. Audit
and metric payloads carry opaque external references (provider file ids or
`gs://` URIs) and identifiers — never attachment content, managed signed URLs
or their query strings — and redaction tests prove every log/audit/telemetry/
broker boundary stays URL- and content-free (Scope §2.3, §6.7).

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

### File security implementation (v0.5)

The v0.5 release ships MIME and size validation at upload-intent time (a
declared size above `STORAGE_MAX_UPLOAD_SIZE` or a disallowed content type is
rejected before any signed URL is issued), size verification at completion (a
stored object whose size does not match the declaration fails the file),
private buckets with short-lived signed URLs, server-generated object keys
(the client submits the file id, never an object path) and worker isolation
for processing. Decompression-bomb protections, page limits and malware
scanning remain deferred until server-side document processing exists
(post-v1). The storage endpoint is a configured setting, never
client-supplied, so storage adds no SSRF surface.

Large-file and provider-reference input (v0.8, ADR-0017 amendment) adds the
same controls to the AI path without weakening them: non-inline sources are
verified for ownership, size, MIME and SHA-256 before any transfer, streamed
through bounded temporary-file storage (never accumulated in memory), and
provider-hosted copies and GCS staging objects are AI-owned derivatives whose
deletion never touches the feature-owned source. Caller-supplied HTTP(S)
URLs remain prohibited, so the SSRF boundary is unchanged; provider upload
credentials, managed signed URLs and Vertex staging credentials stay in
typed secret/configuration slots (BP §27), and managed URLs are exact-object,
read-only, short-lived bearer capabilities that are never returned,
persisted, audited or logged (Scope §2.2–§2.5).

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

Platform routes (v0.4) join the same `PROTECTED_ROUTES` matrix and add a
**cross-plane** case: an organisation `owner` without a platform membership is
rejected on platform routes (`403 platform_admin_required`), and a platform
admin without an organisation membership is rejected on organisation routes
(`403 not_a_member`). The two planes never grant across each other.

Files and jobs routes (v0.5) join the same matrix with the full case list;
the oversized-upload rejection (a declared size above
`STORAGE_MAX_UPLOAD_SIZE` rejected at intent time) is covered by the file
test suite rather than as a matrix row. MinIO-backed S3
adapter tests carry a `storage_integration` marker and are excluded from the
default suite, so the local quality gate stays provider-free; a dedicated CI
job runs them against a real MinIO service.

The AI layer (v0.7, ADR-0017) follows the same provider-free default: the
fake provider covers every adapter contract in the default suite, an
import-boundary test proves no provider SDK is imported outside
`app/ai/providers/`, and opt-in `ai_contracts`-marked tests exercise real
providers only against dedicated non-production accounts/projects in
protected CI (ADR-0018: Vertex AI only for Google, never a Gemini API key).

The v0.8 large-file transfer contracts (ADR-0017 amendment, Scope §6.1)
add fake-backed contract tests to that default: the deterministic mode
selector and fake staging store cover selection, reuse, expiry and deletion
hermetically, the checked-in provider contract fixture is validated in CI
(`make validate-ai-registries`), and registry/config mutation tests prove
every inconsistent mode, source-lifecycle, MIME, threshold/ceiling, provider,
expiry/TTL or regional declaration fails fast before dispatch. Opt-in
provider contract tests for the transfer adapters stay `ai_contracts`-marked
and run only against dedicated non-production accounts (Scope §6.4–§6.6);
`make e2e` keeps the protected-route journeys green.

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

Provider SDKs are regular dependencies and follow the same rules; an LLM
provider SDK may only be added together with its adapter under
`app/ai/providers/`, with the justification recorded (v0.7 Scope §6.3, ADR-0017).

The v0.8 transfer contracts (ADR-0017 amendment) add no provider SDK in the
contract checkpoint: `app/ai/transfer.py` and `app/ai/staging.py` are
provider-neutral (PyYAML plus the existing runtime), the fixture and registry
are validated by existing `make` targets, and an import-boundary test keeps
transfer-mode and provider concepts inside `app/ai/`. A provider SDK may be
added only together with its concrete `TransferStore` adapter in §6.4–§6.6,
with its justification and review gate recorded.

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
- AI requests go through `AIService` by task name; no feature module imports
  an LLM SDK or names a provider/model directly (v0.7, ADR-0017).
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

The v0.4 platform plane (ADR-0013) touches authentication (bootstrap,
webhooks), the permission model (a second authorisation plane), tenant
isolation (cross-tenant platform routes) and secret handling (WorkOS org
mapping and webhook secret); all such work units are reviewed through the
implement → review → apply-and-commit loop with the review recorded per
`CONTRIBUTING.md` before it is applied.

The v0.7 AI layer (ADR-0017, ADR-0018) touches tenant isolation
(`organisation_ai_settings`, AI usage/cost records), secret handling (provider
credentials, Vertex credential material) and public API surface (the
organisation-scoped demonstration endpoint); each work unit is reviewed
through the same loop, and any provider SDK dependency is justified and pinned
per §32.

The v0.8 large-file transfer modes (ADR-0017 amendment) touch tenant isolation
(organisation transfer policy, `ai_attachment_references`), secret/IAM
handling (provider upload credentials, managed signed URLs, Vertex staging
bucket and credentials), database migrations and provider data handling;
each checkpoint names its review gate (Scope §6.1–§6.8) and prompt 03 cannot
apply, commit or merge it until the required approval is recorded.

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
