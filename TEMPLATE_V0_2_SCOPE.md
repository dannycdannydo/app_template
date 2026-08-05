# Template v0.2 — Scope & Progress Log

## Relationship to other documents

- `Internal_Custom_Application_Starter_Architecture_v2.md` is the long-term **design standard**.
- `IMPLEMENTATION_GUIDE.md` is the **build plan** and the incremental release sequence.
- This file is the **scoped contract for the v0.2 release**. It defines exact deliverables, exclusions, acceptance tests, and the commands that must work. It also serves as a progress log: check items off as they are completed.

---

# 1. Goal of v0.2

A **backend identity and tenancy core**. After v0.2, a fresh clone can authenticate through WorkOS, map the verified identity to an internal user, operate inside a validated organisation context with default-deny roles and permissions, and use a tenant-scoped example module that proves the isolation conventions. This is the highest-value release of the template and where most security risk lives; frontend login flows arrive in v0.3.

v0.2 establishes every convention later releases inherit for identity: the centralised session-validation module, the users/organisations/membership data model, organisation context resolution, the permission model, and the mandatory cross-organisation security tests.

---

# 2. In Scope

```text
WorkOS login (backend validation side)
internal users
organisations
organisation memberships
basic roles
request context
tenant-scoped example module
cross-organisation security tests
```

WorkOS owns login, sessions, SSO, MFA. The application owns the internal user record, organisation membership, roles, and permissions (audit history arrives in v0.5).

The application stores the WorkOS user identifier, not passwords.

Backend rules from `IMPLEMENTATION_GUIDE.md` §Template v0.2, all in scope:

- validate token signature, issuer, audience and expiry;
- never trust identity fields submitted by the frontend;
- disabled users are blocked even with a valid session;
- session and webhook validation are centralised;
- authentication is not authorisation.

Explicit deliverables:

- Centralised auth module (`app/core/security.py` per blueprint §5) with session validation and a webhook-signature validation helper (no webhook consumers until v0.5).
- `GET /api/v1/me` — current user, memberships, roles.
- Organisation creation endpoint; creator becomes `owner`.
- Default roles (`owner`, `administrator`, `manager`, `member`, `viewer`) and example permissions seeded via an Alembic data migration.
- Tenant-scoped example module (`records`) demonstrating model, schemas, service, router, pagination, permission enforcement and integration tests — backend only; Vue screens are v0.3.
- Mandatory reusable security tests (blueprint §31) applicable to v0.2.

---

# 3. Out of Scope (Explicitly Deferred)

These are **not** part of v0.2. They appear in later releases per `IMPLEMENTATION_GUIDE.md`.

| Capability | Deferred to |
| --- | --- |
| Frontend login flow, protected routes, organisation selector | v0.3 |
| Storage interface, S3-compatible adapter, MinIO, signed uploads | v0.4 |
| Dramatiq, Redis queue wiring, durable job records | v0.4 |
| Audit log and audit events | v0.5 |
| WorkOS webhook consumers (event processing) | v0.5 |
| Sentry, email provider, notifications | v0.5 |
| Hybrid VPS deployment | v0.5 |
| Teams (`teams`, `team_memberships`) and team-specific permissions | post-v1 (no planned release slot; blueprint §9 adds them only when required) |
| Managed Azure reference deployment | post-v1 |
| Transactional outbox, import/export framework, DB feature flags | post-v1 |

---

# 4. Commands That Must Work

All v0.1 commands remain part of the quality gate. `make migrate` now also applies the seed data migration (default roles and example permissions). No new Makefile target is required by these capabilities.

```bash
make dev              # Postgres + Redis in Docker; API + frontend native with live reload (ADR-0008)
make dev-docker       # entire stack in containers (CI parity, onboarding, Dockerfile validation)
make migrate          # run Alembic migrations, including default-role seed data
make lint             # Ruff (backend) + ESLint (frontend)
make typecheck        # Pyright (backend) + vue-tsc (frontend)
make test             # pytest (backend) + Vitest (frontend)
make format           # Ruff format + Prettier
make generate-client  # export OpenAPI from FastAPI -> generate TS client
make check            # full local quality gate (lint + typecheck + test + drift)
```

---

# 5. Acceptance Criteria

v0.2 is done when **all** of the following are true:

1. **Session validation**: `make test` passes integration tests where a fixture-signed token with an invalid signature, a wrong issuer, a wrong audience, or an expired expiry is rejected with `401` on a protected endpoint, and a correctly signed token is accepted and maps to an internal user.
2. **Internal users**: the first valid session for a given `workos_user_id` provisions exactly one internal user row; a later session for the same identifier reuses that row (no duplicates); the `users` table has no password column.
3. **Organisations and memberships**: `POST /api/v1/organisations` returns `201` and the creator's membership is assigned the `owner` role; `GET /api/v1/me` returns the current user with memberships and roles.
4. **Request context**: every `/api/v1` route except `/health` and `/ready` requires a Bearer token and an `X-Org-Id` header; missing token → `401`, missing/malformed `X-Org-Id` → `400`, an org the user does not belong to → `403`; a test proves identity fields submitted in a request body are never trusted.
5. **Roles and permissions**: `make migrate` against a fresh database seeds the five default roles and the example permission set; a `viewer` can list records but every write returns `403`; access to a permission not granted to any of the caller's roles is denied (default deny).
6. **Disabled users**: with an otherwise valid session, a user with `is_active = false` is rejected with `403` on every protected endpoint.
7. **Tenant-scoped example module**: records CRUD works inside the caller's organisation; reading or updating a record belonging to another organisation returns `404`; the list endpoint returns the pagination envelope documented in `API_CONVENTIONS.md`.
8. **Cross-org security tests**: the mandatory reusable security tests relevant to v0.2 (unauthenticated rejected, invalid session rejected, cross-organisation access denied, viewer writes denied, disabled users denied, stack traces not exposed) run in CI and are green.
9. **Governance and audit**: `make check` passes with zero lint errors, zero type errors, green tests and a diff-free generated client; all authentication, permission-model and tenant-isolation changes were human-reviewed per blueprint §33; `.env.example` documents every new variable the app reads; no secrets are committed; the architecture audit (`prompts/04-architecture-audit.md`) reports no CRITICAL or MAJOR findings.

---

# 6. Progress Log

Check items off as they are completed. Keep one task `in_progress` at a time where practical.

Subsections are ordered so later work builds on earlier work: the data model precedes the auth foundation that maps identities to it, request context precedes the example module that consumes it, and the security tests close the loop over everything. Dependencies are noted per subsection.

## 6.1 Identity & Tenancy Data Model

Foundation for everything else; §6.2 and §6.4 depend on these tables existing.

- [x] `User` model — `workos_user_id` (unique), email, name, `is_active`, UUIDv7 PK, timestamps
- [x] `Organisation` model — name, UUIDv7 PK, timestamps
- [x] `OrganisationMembership` model — user_id + organisation_id with unique constraint, status
- [x] Alembic migration creating the three tables with naming conventions and constraints
- [x] Pydantic schemas (`UserListItem`, `OrganisationCreate`, `OrganisationResponse`, membership schemas) — ORM models are never API request models
- [x] `base.py` registration of the new models for Alembic autogenerate

## 6.2 WorkOS Authentication Foundation

Depends on §6.1 (users table). Centralises session validation per the guide's backend rules.

- [x] Settings added to `app/core/config.py`: `WORKOS_API_KEY`, `WORKOS_CLIENT_ID`, WorkOS environment/API base, and any session-validation inputs the SDK requires; fail-fast validation for production
- [x] `app/core/security.py` — centralised session validation: token signature, issuer, audience, expiry (via WorkOS SDK)
- [x] Webhook-signature validation helper in `app/core/security.py` (centralised; no consumer until v0.5)
- [x] Auth dependency `get_current_user` — Bearer token → validated session → internal user; provisions the user on first login; rejects disabled users
- [x] `GET /api/v1/me` route — current user with memberships and roles

## 6.3 Request Context & Organisation Selection

Depends on §6.2. Resolves the validated identity to an organisation context for every protected request.

- [x] `get_current_membership` dependency — resolves `X-Org-Id` against the current user's memberships
- [x] Standard errors for context failures: missing token (`401`), missing/malformed `X-Org-Id` (`400`), not a member (`403`)
- [x] `POST /api/v1/organisations` — creates an organisation and assigns the creator the `owner` role (transactional)
- [x] Organisation ID derived from validated context, never from request bodies
- [x] Update `API_CONVENTIONS.md` authn/authz section with the real conventions (headers, codes, default deny)

## 6.4 Roles & Permissions

Depends on §6.1 and §6.3. Default-deny permission model over memberships.

- [x] Models: `Role`, `Permission`, `RolePermission`, `MembershipRole` + Alembic migration
- [x] Data migration seeding the five default roles (`owner`, `administrator`, `manager`, `member`, `viewer`) and the example permission set from blueprint §9
- [x] Permission codes and a `require_permission(...)` dependency/helper enforcing default deny
- [x] Role-assignment service — `owner`/`administrator` manages member roles; enforced via permissions such as `users.manage_roles`
- [x] Integration tests: seeded roles exist, permission checks deny by default, role assignment works

## 6.5 Tenant-Scoped Example Module (`records`)

Depends on §6.3 and §6.4. Demonstrates the full module pattern from blueprint §5 with tenancy.

- [ ] `Record` model with `organisation_id` FK + migration
- [ ] Schemas: `RecordCreate`, `RecordUpdate`, `RecordListItem`, `RecordDetail`
- [ ] Service with tenant-scoped queries, transaction boundaries, domain exceptions
- [ ] `queries.py` with the reusable org-scoped query
- [ ] Router: list (paginated envelope), create, get, update, delete — all org-scoped and permission-gated (`records.read`, `records.create`, `records.update`, `records.delete`)
- [ ] Integration tests: CRUD within org, cross-org access returns `404`, viewer writes return `403`, pagination envelope correct

## 6.6 Cross-Org Security Tests & Release Governance

Depends on §6.5 (exercises the module). Closes the release.

- [ ] Mandatory security tests (blueprint §31): unauthenticated rejected; invalid session rejected; cross-organisation access denied; viewer writes denied; disabled users denied; stack traces not exposed
- [ ] Docs updated: `SECURITY.md`, `API_CONVENTIONS.md`, `AGENTS.md` (auth/permission/tenant rules), `ARCHITECTURE.md` (identity flow, request context)
- [ ] `.env.example` documents every new variable (WorkOS config)
- [ ] `make check` green from a clean checkout; generated-client drift clean; CI green
- [ ] Human review recorded for authentication, permission-model and tenant-isolation changes (blueprint §33)
- [ ] Architecture audit (`prompts/04-architecture-audit.md`) clean — no CRITICAL or MAJOR findings

---

# 7. Blueprint Reference Map

Each scope subsection (left column) maps to specific sections of `Internal_Custom_Application_Starter_Architecture_v2.md`. Implementers and reviewers read **only** the listed sections for a given task — not the whole blueprint. This keeps context lean and focused.

## Disambiguating section numbers

Two documents use the `§` symbol. Do not confuse them:

- **Scope §6.x** — a subsection of *this file's* checklist (e.g. Scope §6.3 = "Request Context & Organisation Selection").
- **BP §N** — a section of the *blueprint* (e.g. BP §8 = "Authentication with WorkOS", starting at line 325).

The map below always uses `BP §N` for blueprint sections and `Scope §6.x` for this file's checklist. **Never** write a bare `§6` or `§8` — always prefix with `Scope` or `BP` so the next reader knows which document you mean.

## Map

Line ranges were verified against the blueprint's table of contents and by reading each section's start and end. Each range covers the section up to the next `#` heading.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Identity & Tenancy Data Model | **BP §7** (lines 270–324), **BP §9** core tables (lines 408–421 within 379–462), **BP §10** (lines 463–543) | ORM/Pydantic separation, users/organisations/memberships tables, identifiers, timestamps, constraints, naming |
| **Scope §6.2** WorkOS Authentication Foundation | **BP §8** (lines 325–378), **BP §27** (lines 1340–1384), **BP §30** (lines 1459–1522) | Responsibility split, identity flow, backend rules, typed settings (`WORKOS_API_KEY` etc.), session validation and CORS controls |
| **Scope §6.3** Request Context & Organisation Selection | **BP §5** (lines 156–216), **BP §6** (lines 217–269), **BP §12** (lines 564–634), **BP §13** (lines 636–685) | Module layout (`security.py`, dependencies), router/service responsibilities, auth context on requests, error mappings |
| **Scope §6.4** Roles & Permissions | **BP §9** (lines 379–462), **BP §10** (lines 463–543) | Default roles, example permissions, rules (default deny, org IDs from validated context), table constraints |
| **Scope §6.5** Tenant-Scoped Example Module | **BP §7** (lines 270–324), **BP §11** (lines 544–563), **BP §12** (lines 564–634) | Explicit response schemas, transaction ownership in services, pagination and filtering conventions |
| **Scope §6.6** Cross-Org Security Tests & Release Governance | **BP §2** (lines 40–61), **BP §30** (lines 1459–1522), **BP §31** (lines 1523–1575), **BP §33** (lines 1627–1672), **BP §42** (lines 2042–2063), **BP §44** (lines 2086–2116), **BP §45** (lines 2117–2138) | Tenant-isolation principle, security controls, mandatory reusable security tests, human-review list, template validation, implementation order (steps 5–7), v0.2-relevant readiness items |

If a task touches a concern not listed here (e.g. the security baseline details for a specific control), consult the blueprint's table of contents and read only the relevant section. When in doubt, read less rather than more — this file's §2–§5 already encodes the v0.2 contract.

---

# 8. Status

```text
Release:    v0.2.0 (identity and tenancy)
State:      in progress
Started:    —
Completed:  —
```

When every acceptance criterion in §5 is met and every box in §6 is checked, update the version recording in `pyproject.toml` and tag `v0.2.0`. Then open `TEMPLATE_V0_3_SCOPE.md`.
