# Practical Implementation Guide

## Relationship to the Architecture Blueprint

This document defines **how** the starter template described in `Internal_Custom_Application_Starter_Architecture_v2.md` is actually built.

The blueprint is the long-term **design standard**. It describes the foundation, the likely reusable modules, future platform capabilities, deployment patterns, and mature operational practices.

This guide defines the **first practical scope**: the proven common core that should exist before the template is used for a real client application.

Not every section of the blueprint belongs in the first working version. Implementing all of it before the first real application would contradict the blueprint's own rule against premature abstraction.

The first template should implement only the proven common core:

```text
auth
users
organisations
permissions
database
API conventions
Vue shell
generated client
storage
jobs
audit
observability
testing
hybrid deployment
```

Everything else is added after real projects demonstrate genuine reuse.

---

# 1. Core Decision: Build, Do Not Fork

## Do not fork the official FastAPI full-stack template

The official `fastapi/full-stack-template` is well-built, but three of its most central architectural decisions conflict with ours:

```text
Official template              Our template
─────────────────────────────────────────────────────
SQLModel                       SQLAlchemy 2 + Pydantic 2
React                          Vue 3
Internal password/JWT auth     WorkOS (external auth)
```

These are not cosmetic differences. They affect every database model, every request and response schema, authentication dependencies, user creation, frontend routing, frontend components, tests, and generated API client assumptions.

Changing them is not like changing a colour scheme. It is untangling assumptions. We would spend significant time deleting code.

## Borrow implementation patterns, not architecture

Use the official template as a reference source for:

- Docker build techniques;
- Compose structure;
- health checks;
- Alembic setup;
- CI conventions;
- generated OpenAPI client workflow;
- testing layout;
- environment configuration;
- project scripts.

Inspect each useful part and reproduce the relevant pattern in our own smaller repository.

Do not copy it wholesale.

## What can be reused directly

| Area | Mostly prebuilt | Mostly custom |
| --- | :---: | :---: |
| FastAPI application startup | Yes | |
| SQLAlchemy and Alembic | Yes | Conventions/models |
| PostgreSQL | Yes | Schema |
| WorkOS login UI and sessions | Yes | Internal mapping and RBAC |
| Vue scaffold | Yes | Application shell |
| shadcn-vue components | Yes | Theme and compositions |
| TanStack Query | Yes | Query composables |
| OpenAPI generation | Yes | CI wiring |
| Dramatiq | Yes | Job model and lifecycle |
| Object storage SDKs | Yes | Common interface and file model |
| Audit history | | Custom |
| Organisation membership | | Custom |
| Permissions | Partially | Mostly custom |
| Tenant isolation | | Custom conventions and tests |
| Hybrid VPS deployment | Partially | Custom deployment files |
| Import framework | Libraries exist | Mostly domain workflow |

Some important pieces are custom. The technically difficult low-level mechanisms are supplied by established libraries. Our work is creating a coherent, opinionated implementation around them.

---

# 2. Assemble, Do Not Write Everything from Scratch

We are assembling existing, mature components:

```text
FastAPI                application framework
SQLAlchemy             ORM
Alembic                migrations
Pydantic               validation
WorkOS                 authentication
Dramatiq               job execution
Redis                  queue broker
create-vue             frontend scaffold
shadcn-vue             UI source components
TanStack Query         frontend server state
openapi-typescript     API types
Sentry                 error monitoring
```

What we custom-build is primarily the **glue and conventions**:

- how a WorkOS user maps to an internal user;
- organisation and membership tables;
- permission dependencies;
- tenant-scoped query conventions;
- standard API error format;
- storage interface;
- durable job records;
- notification abstraction;
- audit events;
- template documentation;
- deployment configurations.

That glue is precisely what makes the starter valuable. Without it, we merely have a collection of libraries. With it, we have a repeatable architecture.

---

# 3. Frontend Creation

The scaffolding commands are only used **once**, while creating the master template. Once the master template exists, new applications begin from the finished Vue shell; we do not run `create-vue` per project.

## One-time master template scaffolding

```bash
pnpm create vue@latest frontend
```

Choose:

```text
TypeScript       yes
Vue Router       yes
Pinia            yes
Vitest           yes
Playwright       yes
ESLint           yes
Prettier         yes
```

Then:

1. generate the official Vue project;
2. add and configure Tailwind;
3. initialise shadcn-vue (`npx shadcn-vue@latest init`);
4. add only the components we actually need;
5. commit the resulting frontend as part of the master template.

## What the finished Vue shell contains

```text
layouts
routing
authentication handling
error handling
generated client
query setup
design tokens
forms
dialogs
tables
notifications
loading states
```

---

# 4. Incremental Release Plan

The template is built as a sequence of small, independently testable releases. Each release is runnable and tested before the next begins.

## Template v0.1 — Foundation

Includes:

```text
Repository structure
FastAPI
SQLAlchemy 2
Pydantic 2
Alembic
PostgreSQL
Vue 3
Tailwind
shadcn-vue
Docker Compose
Ruff / Pyright / pytest
ESLint / vue-tsc / Vitest
GitHub Actions
typed configuration (pydantic-settings)
health and readiness endpoints
standard API error format
```

At this point the app starts locally and passes CI.

Commands that must work:

```bash
make dev        # starts Postgres, Redis, API, frontend
make migrate    # runs Alembic migrations
make check      # lint + typecheck + tests
```

## Template v0.2 — Identity and Tenancy

Adds:

```text
WorkOS login
internal users
organisations
organisation memberships
basic roles
request context
tenant-scoped example module
cross-organisation security tests
```

WorkOS owns login, sessions, SSO, MFA. The application owns the internal user record, organisation membership, roles, permissions, and audit history.

The application stores the WorkOS user identifier, not passwords.

Backend rules:

- validate token signature, issuer, audience and expiry;
- never trust identity fields submitted by the frontend;
- disabled users are blocked even with a valid session;
- session and webhook validation are centralised;
- authentication is not authorisation.

This is the highest-value part of the template and deserves careful implementation. It is where most risk lives.

## Template v0.3 — Frontend Application Shell

Adds:

```text
login flow
protected routes
main layout
sidebar
user menu
organisation selector
standard table
standard form
toast and error handling
generated OpenAPI client
TanStack Query setup
```

At this point the starter should feel like a genuine application rather than a technical demo.

## Template v0.4 — Platform Administration

Adds:

```text
audit events (blueprint §29, pulled forward)
bootstrap platform admin (BOOTSTRAP_PLATFORM_ADMIN_EMAIL)
platform admin centre
platform authorisation plane (platform_roles, platform.admin)
WorkOS organisation mapping (organisations.workos_organisation_id)
WorkOS Invitation API onboarding
membership administration (suspend / reactivate / remove / roles)
platform-controlled organisation feature flags (blueprint §27, pulled forward)
WorkOS webhook consumer
Vue admin pages
```

WorkOS invitations become the standard onboarding flow and user and
organisation administration moves out of the WorkOS dashboard and into the
application. WorkOS remains the identity provider (identities, authentication,
sessions, invitation delivery); the application remains the source of truth
for organisations, memberships, roles, permissions, feature flags and audit
history. The platform plane is deliberately separate from the organisation
permission system — no global admin bypass. Design source:
`PLATFORM_ADMIN_WORKFLOW_PLAN.md`.

## Template v0.5 — Files and Jobs

Adds:

```text
storage provider interface
one S3-compatible adapter
MinIO for local development
signed uploads
file metadata records
Dramatiq
Redis
durable job records
job progress polling
```

This gives the core foundation for document-heavy applications.

## Template v0.6 — Operations

Adds:

```text
structured JSON logging
Sentry
email provider interface
one email provider
basic notifications
hybrid VPS deployment
backup and recovery documentation
```

At this point the template is ready for the first real client application.

---

# 5. Explicitly Deferred Scope

These capabilities are **out of scope** for the first template. They are added only when a real project demonstrates genuine need, following the rule of three.

```text
transactional outbox
generic import-mapping UI
generic export framework
database-backed feature flags
all three object-storage adapters (ship S3-compatible + MinIO only)
Server-Sent Events
pgvector and PostGIS setup
advanced notification preferences
managed Azure reference infrastructure
full internal package extraction
sophisticated impersonation
general-purpose integration reconciliation
```

These appear in the blueprint because the blueprint is the long-term design standard. Their absence from the first template is deliberate, not an oversight.

Exception: platform-controlled organisation feature flags (blueprint §27) land in v0.4 as part of Platform Administration; the *general* database-backed feature-flag framework for application features remains deferred.

---

# 6. Process

## Step 1 — Freeze the scope of each release

Do not hand the build agent the entire 46-section blueprint and ask it to implement everything.

Before each release, define a smaller scope document containing only:

- exact deliverables;
- exclusions;
- acceptance tests;
- commands that must work.

This guide is the top-level scope. Per-release scope files (for example `TEMPLATE_V0_1_SCOPE.md`) are produced when each release begins.

## Step 2 — Create the repository manually

Start with an empty repository rather than the FastAPI full-stack template.

Use official generators where useful:

```bash
uv init
pnpm create vue@latest frontend
npx shadcn-vue@latest init
alembic init
```

Then let the coding agent assemble them according to our specification.

## Step 3 — Add one example module

Include a simple, neutral module such as `projects` or `records`. It should demonstrate:

- SQLAlchemy model;
- Pydantic schemas;
- route;
- service;
- tenant scoping;
- pagination;
- permission;
- integration tests;
- Vue list and edit screens.

A working example module is more useful to future agents than abstract documentation alone.

## Step 4 — Build vertical slices

Do not build all backend infrastructure and only then attempt the UI.

Build complete slices:

```text
WorkOS login
  -> internal user
  -> organisation membership
  -> protected API
  -> Vue protected screen
  -> integration test
  -> Playwright test
```

Then:

```text
Create example record
  -> database
  -> API
  -> generated client
  -> Vue table
  -> tests
```

Vertical slices expose integration problems early.

## Step 5 — Release an internal alpha

Use the template for a small real application before adding every planned platform feature.

Mark it:

```text
v0.1.0
```

Expect to change conventions.

## Step 6 — Backport proven improvements

When the first app reveals a better approach, update the master template deliberately and write an upgrade note under `docs/upgrades/`.

Build remaining capabilities (imports, integrations, exports, advanced search) through real projects, and generalise the genuinely generic parts back into the template only once at least three applications need substantially the same implementation.

---

# 7. Realistic Effort

Estimates for the usable first template, not the final theoretical platform:

| Stage | Focused implementation effort |
| --- | ---: |
| Foundation and local development | 1–2 days |
| Database and backend conventions | 1–2 days |
| WorkOS, users and organisations | 2–4 days |
| Roles and tenant-isolation tests | 2–3 days |
| Vue shell and generated API client | 2–3 days |
| Storage and files | 2–3 days |
| Dramatiq and durable jobs | 1–2 days |
| Audit, Sentry and email basics | 1–2 days |
| Hybrid VPS deployment | 1–2 days |
| Documentation and clean-clone testing | 1–2 days |

Roughly **13–23 focused developer days** in conventional effort.

With capable coding agents and milestone-by-milestone review, the elapsed implementation work could reasonably be compressed to perhaps **one to two intensive weeks**, provided the architecture and tests are reviewed by a human at each milestone.

A whole repository generated in one prompt should not be trusted as production-ready.

---

# 8. Definition of the First Usable Template

The template is ready for the first real client application when a fresh clone can:

- start locally with one command;
- authenticate through WorkOS;
- create an organisation and membership;
- enforce a permission;
- run PostgreSQL migrations;
- upload a file using a signed URL;
- enqueue and complete a Dramatiq job;
- write an audit event;
- expose health and readiness endpoints;
- regenerate the Vue API client;
- pass all lint, type and test checks;
- deploy through the hybrid VPS profile.

Managed cloud deployment, the transactional outbox, the import/export framework, and database-backed feature flags are explicitly **not** required for the first usable template. They are added as real projects prove the need.

---

# 9. Summary

The architecture blueprint is the long-term design standard.

This guide is the build plan for the first practical, usable template.

The first template implements only the proven common core, built as a sequence of small testable releases, assembled from mature libraries, held together by our own conventions.

Once that core is stable, cloning the repository really can get a new bespoke application running within minutes.
