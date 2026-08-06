# Template v0.4 — Scope & Progress Log

## Relationship to other documents

- `Internal_Custom_Application_Starter_Architecture_v2.md` is the long-term **design standard**.
- `IMPLEMENTATION_GUIDE.md` is the **build plan** and the incremental release sequence. v0.4 in this guide is *Platform Administration* (Files and Jobs moved to v0.5, Operations to v0.6).
- `PLATFORM_ADMIN_WORKFLOW_PLAN.md` is the **design source** for this release: the decisions behind the platform authorisation plane, bootstrap, invitations, org mapping, audit and feature flags.
- This file is the **scoped contract for the v0.4 release**. It defines exact deliverables, exclusions, acceptance tests, and the commands that must work. It also serves as a progress log: check items off as they are completed.

---

# 1. Goal of v0.4

A **bootstrap Platform Admin and Platform Admin Centre** on top of the v0.2/v0.3 identity core. After v0.4, administrators bootstrap one Platform Admin from a configured WorkOS email, then manage the whole application from inside it: create and edit organisations, invite users through the WorkOS Invitation API (the standard onboarding flow), assign roles, suspend/reactivate/remove memberships, control platform feature flags, and read the audit history — with no user or organisation administration in the WorkOS dashboard. WorkOS remains the identity provider; the application remains the source of truth for organisations, memberships, roles, permissions, feature flags and audit history. The platform plane is a dedicated authorisation layer, never a global bypass of the organisation permission system.

Per `IMPLEMENTATION_GUIDE.md`: WorkOS invitations become the standard onboarding flow and user and organisation administration moves into the application.

---

# 2. In Scope

```text
audit events (append-only, blueprint §29)
bootstrap platform admin (BOOTSTRAP_PLATFORM_ADMIN_EMAIL, one-time, audited)
platform authorisation plane (platform_roles, platform.admin permission)
WorkOS organisation mapping (organisations.workos_organisation_id)
WorkOS Invitation API onboarding (invitations table, login-time linking)
membership administration (assign roles, suspend / reactivate, remove)
platform-controlled organisation feature flags (blueprint §27)
WorkOS webhook consumer (signature-verified, best-effort)
platform admin centre UI (Vue admin pages)
```

WorkOS owns identities, authentication, sessions and invitation delivery; the backend Management API key and webhook secret exist server-side only and never reach the frontend. The frontend never submits identity fields — only the session token. UI permission awareness (showing the Platform Admin Centre only to platform admins) is cosmetic; the backend remains the enforcement point ("authentication is not authorisation", blueprint §9).

The v0.1–v0.3 foundation already ships the generated-client pipeline, TanStack Query, Pinia, Vue Router, shadcn-vue primitives, Vitest and Playwright, and the backend identity/permission surface (`me`, `organisations`, `records`, `roles`, `permissions`, `membership_roles`). v0.4 builds the platform plane and admin centre on that foundation; it is not a greenfield build.

Explicit deliverables:

- **Audit**: `audit_events` table (blueprint §29) with an append-only recording service and a platform-gated listing endpoint. Every lifecycle action below writes an audit event.
- **Platform authorisation plane**: `platform_roles`, `platform_role_permissions`, `platform_memberships` tables mirroring the organisation plane; a `platform_admin` role seeded against a new `platform.admin` permission code; a dedicated `require_platform_permission(code)` dependency that never consults organisation context; `GET /api/v1/me` returns `platform_roles` for UI gating.
- **Organisation mapping**: `organisations.workos_organisation_id` (nullable, unique) with a WorkOS adapter (`integrations/workos/organizations.py`) that creates the WorkOS organisation eagerly at platform org creation and lazily backfills pre-existing organisations at first invite. Internal id remains the primary key.
- **Bootstrap**: `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` setting; `bootstrap_state` single-row table; the grant runs inside the existing provisioning chain, only for the configured email, only when the WorkOS profile reports `email_verified`, once, atomically, audited.
- **Invitations**: `invitations` table (status, expiry, `workos_invitation_id`) and WorkOS adapter (`integrations/workos/invitations.py`); platform-gated invite/revoke/list endpoints; membership created at **login-time linking** (authoritative), invitation marked accepted, audit written. No membership row is created at invite time.
- **Membership administration**: assign/remove organisation roles, suspend/reactivate and remove memberships — all audited, all enforced through the existing active-membership check.
- **Feature flags**: `organisation_features` table (blueprint §27) with a backend enforcement helper (`core/feature_flags.py`, default off) and platform-gated management endpoints.
- **Webhooks**: `POST /api/v1/webhooks/workos` gated by the existing `verify_webhook_signature`, consuming invitation and user-lifecycle events to refresh local state. Best-effort only: login-time reconciliation stays authoritative.
- **Platform Admin Centre UI**: `/platform` route section (dashboard, organisations list/create/edit, org detail with memberships/invitations/feature flags, invite form, feature-flag catalogue, audit view) gated by `platform_roles` from `/me`; query composables in `src/queries/platform.ts`; generated-client refresh; Vitest + Playwright coverage.
- New settings (`BOOTSTRAP_PLATFORM_ADMIN_EMAIL`, `WORKOS_WEBHOOK_SECRET`) documented in `.env.example` (backend-only; no new `VITE_*` variables).

---

# 3. Out of Scope (Explicitly Deferred)

These are **not** part of v0.4. They appear in later releases per `IMPLEMENTATION_GUIDE.md`.

| Capability | Deferred to |
| --- | --- |
| Storage interface, S3-compatible adapter, MinIO, signed uploads, file metadata records | v0.5 |
| Dramatiq, Redis queue wiring, durable job records, job progress polling | v0.5 |
| Structured JSON logging, Sentry, email provider, notifications | v0.6 |
| Hybrid VPS deployment, backup and recovery documentation | v0.6 |
| Org-level invitations (a member with `users.invite` inviting into their own organisation through the same flow) | post-v1 (the shared invitation service is built; only the org-gated endpoints are deferred) |
| Gating or removing the unprivileged `POST /api/v1/organisations` (org-first bootstrap) | post-v1 (breaking change, human review required) |
| General (application-level) database-backed feature-flag framework | post-v1 (only platform-controlled organisation flags ship in v0.4) |
| Teams (`teams`, `team_memberships`) and team-specific permissions | post-v1 (blueprint §9 adds them only when required) |
| Self-service registration / public signup flows | post-v1 |
| Advanced data grids (AG Grid, Handsontable) | post-v1 (blueprint §16: dedicated grids wrapped behind internal components when a project genuinely needs them) |
| Server-side rendering, multi-language UI / i18n | post-v1 |
| Managed Azure reference deployment | post-v1 |
| Transactional outbox, import/export framework | post-v1 |

---

# 4. Commands That Must Work

All v0.1–v0.3 commands remain part of the quality gate. `make generate-client` now also produces types for the v0.4 endpoints and the drift check stays in `make check`. No new make targets are required by v0.4; the Alembic migration pipeline (`make migrate`) covers the new tables.

```bash
make dev              # Postgres + Redis in Docker; API + frontend native with live reload (ADR-0008)
make dev-docker       # entire stack in containers (CI parity, onboarding, Dockerfile validation)
make migrate          # run Alembic migrations
make lint             # Ruff (backend) + ESLint/oxlint (frontend)
make typecheck        # Pyright (backend) + vue-tsc (frontend)
make test             # pytest (backend) + Vitest (frontend)
make format           # Ruff format + Prettier
make generate-client  # export OpenAPI from FastAPI -> generate TS client (openapi-typescript)
make e2e              # Playwright journeys against the local stack
make check            # full local quality gate (lint + typecheck + test + drift)
```

`make dev` for v0.4 requires `WORKOS_API_KEY` (already documented) and, for bootstrap and invitation work, `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` and `WORKOS_WEBHOOK_SECRET` where set; `.env.example` documents them.

---

# 5. Acceptance Criteria

v0.4 is done when **all** of the following are true:

1. **Audit**: `audit_events` exists via Alembic migration with the blueprint §29 shape; every v0.4 lifecycle action (bootstrap grant, platform-admin grant/revoke, organisation create/update, invitation sent/accepted/revoked, membership role change/suspend/reactivate/remove, feature-flag change) writes an append-only row; there is no update or delete path for audit rows (service or API); the listing endpoint is platform-gated and paginated.
2. **Platform plane is a separate plane, not a bypass**: `require_platform_permission` rejects callers with no platform membership (403 `platform_admin_required`); the security suite proves an organisation `owner` cannot call platform routes and a platform admin with no organisation membership cannot call organisation routes; no `is_admin`/superuser boolean exists anywhere in the model or services.
3. **Protected-surface completeness**: every new `/api/v1` route is present in `PROTECTED_ROUTES` (`backend/tests/test_security_suite.py`) and its completeness guard test stays green; platform routes get the unauthenticated → 401, invalid-session → 401, disabled-user → 403, non-platform-admin → 403 and stack-trace non-exposure cases; org-context rows do not apply to platform routes (no `X-Org-Id`).
4. **Organisation mapping**: `organisations.workos_organisation_id` is nullable and unique; the adapter creates a WorkOS organisation and stores the mapping; pre-existing organisations are backfilled lazily at first invite; the mapping field is never client-writable (request schemas use `extra="forbid"`).
5. **Bootstrap**: with `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` set, the first login of that exact WorkOS email (WorkOS profile `email_verified` true) grants `platform_admin` exactly once — a second login is a no-op, a different or unverified email never grants, a concurrent double first-login cannot double-grant (constraint, proven by test), and every grant writes `platform.bootstrap_granted`.
6. **Invitation flow**: inviting writes a row to `invitations` and calls the WorkOS Invitation API through the adapter (stubbed in tests); accepting at login (WorkOS email == invitation email, invitation not revoked/expired) creates an active membership with the intended role, marks the invitation accepted and audits both events; revoked or expired invitations never grant; no membership row exists before acceptance.
7. **Membership administration**: role assignment/removal, suspend/reactivate and removal round-trip through the platform API; a suspended membership is rejected by organisation routes (403 `not_a_member` via the existing active-membership check); all changes are audited.
8. **Feature flags**: `organisation_features` exists and the backend helper enforces flags (default off) in services; platform management endpoints are platform-gated and audited.
9. **Webhooks**: the webhook endpoint rejects requests with a missing/invalid/expired signature (tests against `verify_webhook_signature`); webhook processing refreshes best-effort invitation state only; a login without any webhook delivery still links the invitation (authoritative login-time reconciliation, proven by test).
10. **Frontend**: the Platform Admin Centre renders only for users with `platform_roles` from `/me` (router guard + nav gating); organisations/invitations/memberships/audit screens round-trip through the generated client; Vitest covers the guard, the invite form and the audit table; a Playwright journey covers the platform-admin invite flow with the test-profile session; `make generate-client` produces no diff.
11. **Governance and audit**: `make check` passes from a clean checkout with zero lint errors, zero type errors, green tests and a diff-free generated client; `make e2e` passes against the local stack; no new dependencies are added without documentation; `.env.example` documents `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` and `WORKOS_WEBHOOK_SECRET`; blueprint amendments, the ADR, `ARCHITECTURE.md`, `API_CONVENTIONS.md` and `README.md` describe the platform plane and invitation flow; auth-flow, permission-model, tenant-isolation, secret-handling and public-API changes were human-reviewed per blueprint §33; the architecture audit (`prompts/04-architecture-audit.md`) reports no CRITICAL or MAJOR findings.

---

# 6. Progress Log

Check items off as they are completed. Keep one task `in_progress` at a time where practical.

Subsections are ordered so later work builds on earlier work: audit precedes every audited action, the platform plane precedes everything that uses it, the org mapping precedes invitations, the bootstrap precedes the admin centre, and the UI closes the release. Dependencies are noted per subsection.

## 6.1 Audit Foundation

Prerequisite for the whole release — the bootstrap and invitation flows must be "fully audited" (§1).

- [x] `audit_events` table (blueprint §29 shape: id, organisation_id nullable, actor_user_id nullable, action, resource_type, resource_id, metadata JSONB, created_at) via Alembic migration; append-only by construction (no update column, no delete endpoint)
- [x] `record_event(...)` service in a new `modules/audit/` module (insert-only, request-id in metadata); existing mutating services gain audit calls
- [x] Platform-gated listing endpoint `GET /api/v1/platform/audit-events` (filter by org/actor/action, standard pagination envelope)
- [x] Tests: append-only enforcement, filtering, platform gating, audit rows for representative mutations

## 6.2 Platform Authorisation Plane

Depends on §6.1 (audit). Establishes the second, orthogonal plane.

- [x] Tables `platform_roles`, `platform_role_permissions`, `platform_memberships` (mirroring the org plane) + data migration seeding `platform_admin` role and `platform.admin` permission code in `permissions/constants.py`
- [x] `require_platform_permission(code)` dependency in `api/dependencies.py` — Bearer token → enabled user → platform membership → role bundles; default deny; never consults `X-Org-Id`
- [x] `GET /api/v1/me` response gains `platform_roles` (empty for non-admins)
- [x] Security suite: platform routes added to `PROTECTED_ROUTES`; cross-plane denial cases (org owner → 403 on platform routes; platform admin without org membership → 403 on org routes); no superuser boolean anywhere

## 6.3 WorkOS Organisation Mapping

Depends on §6.2 (platform plane gates the org-management endpoints). Satisfies ADR-0001's mapping requirement.

- [x] `organisations.workos_organisation_id` (nullable, unique) via Alembic migration
- [x] `integrations/workos/organizations.py` adapter — create/get WorkOS organisation; name from the internal org; `WORKOS_API_KEY` stays inside the adapter
- [x] Platform org create (`POST /api/v1/platform/organisations`) creates internal org + WorkOS org + mapping transactionally; lazy backfill for pre-existing organisations at first invite; orphan-org reconciliation documented
- [x] Tests: mapping round-trip, uniqueness, lazy backfill, no client-writable mapping field

## 6.4 Bootstrap Platform Admin

Depends on §6.2 (platform membership machinery) and §6.1 (audit). The one-time bootstrap.

- [x] `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` in `core/config.py` (fail-fast production validation) and `.env.example`
- [x] `bootstrap_state` single-row table (email, consumed_by_user_id, consumed_at); `UserProfile` gains `email_verified` from the WorkOS profile
- [x] Grant hook inside the `get_current_user` provisioning chain: configured email + profile `email_verified` + bootstrap unconsumed → platform membership + `bootstrap_state` in one transaction (IntegrityError = already consumed), audit `platform.bootstrap_granted`
- [x] Tests: once-only (repeat logins no-op), wrong/unverified email never grants, concurrent first-login race, audit row written

## 6.5 Invitations

Depends on §6.3 (org mapping) and §6.2 (platform gate). WorkOS Invitation API becomes the standard onboarding flow.

- [x] `invitations` table (organisation_id, email, role_code, workos_invitation_id unique nullable, invited_by_user_id, status sent/accepted/revoked/expired, expires_at) via Alembic migration
- [x] `integrations/workos/invitations.py` adapter — send (returns id + expiry), revoke, get; SDK stays behind the adapter
- [x] Platform endpoints: `POST /organisations/{id}/invitations`, `GET .../invitations`, `DELETE .../invitations/{id}` — validate permission → ensure WorkOS org → WorkOS send → insert row → audit `invitation.sent`
- [x] Login-time linking service (`link_invitation_on_login`): after provisioning, match `sent` invitations by email (case-insensitive, not expired), verify authenticated WorkOS email and `email_verified`, create active membership + intended role, mark accepted, audit `invitation.accepted` + `membership.role_changed`; idempotent and race-safe
- [x] Tests: full invite→accept journey, revocation/expiry never grant, no membership before acceptance, email mismatch rejected, audit rows

## 6.6 Membership Administration

Depends on §6.5 (memberships created at acceptance). Completes the platform administration of organisations.

- [ ] Platform endpoints: list memberships; assign/remove organisation role; suspend/reactivate (`PATCH .../status`); remove membership — all audited
- [ ] Enforcement: suspended memberships rejected by org routes through the existing active-membership check; removing a membership also revokes its pending invitations
- [ ] Tests: role round-trip, suspend → 403 on org routes, reactivate → access restored, removal cascades, audit rows

## 6.7 Feature Flags

Depends on §6.2 (platform gate). Platform-controlled organisation flags (blueprint §27).

- [ ] `organisation_features` table (organisation_id, feature_key, enabled, configuration_json, unique pair) via Alembic migration
- [ ] `core/feature_flags.py` enforcement helper (`is_feature_enabled`, default off, cache-friendly); used by services, not routers
- [ ] Platform endpoints: `GET /api/v1/platform/feature-flags` (catalogue + org overrides), `PUT /api/v1/platform/feature-flags/{feature_key}` — audited
- [ ] Tests: default-off enforcement, org isolation, platform gating

## 6.8 WorkOS Webhooks

Depends on §6.5 (invitation state to refresh). Pulled forward from the v0.5 backlog; best-effort only.

- [ ] `WORKOS_WEBHOOK_SECRET` setting; `POST /api/v1/webhooks/workos` gated by `verify_webhook_signature` (HMAC-SHA256, 300s tolerance)
- [ ] Consumer refreshes best-effort invitation status (revoked/expired) and user-lifecycle state; never authoritative for grants — login-time reconciliation (§6.5) decides
- [ ] Tests: bad signature rejected, unknown event tolerated, webhook-delivery failure does not break the login-time invite link

## 6.9 Platform Admin Centre UI

Depends on §6.2–§6.8 (the full platform API surface). The Vue admin pages.

- [ ] `make generate-client` regenerates types for all platform endpoints; drift gate stays in `make check`
- [ ] `src/queries/platform.ts` composables keyed `['platform', ...]` (cross-org server state); no component/store imports `src/api/client.ts` directly
- [ ] Router section `/platform` with a `requiresPlatformAdmin` guard; `SidebarNav` entry only when `useMeQuery` reports `platform_roles` (UI-only; backend enforces)
- [ ] Views: dashboard, organisations list/create/edit, org detail (memberships table with role select + suspend/reactivate/remove, invitations list, feature-flag toggles, org audit events), invite form (email + role), feature-flag catalogue, audit view — standard `DataTable`/form/toast building blocks
- [ ] Vitest: guard, nav gating, invite form, audit table; Playwright journey: platform-admin invites a user who then appears in memberships (test-profile session)

## 6.10 Docs, ADR & Release Governance

Depends on §6.9 (exercises the platform). Closes the release.

- [ ] Blueprint amendments applied (`Internal_Custom_Application_Starter_Architecture_v2.md` §8, §9, §27, §29, §31, §33 — see §7 of this file); ADR recording the WorkOS Organization adoption decision and the platform-plane decision
- [ ] `ARCHITECTURE.md` (platform plane, request flow for `/api/v1/platform/*`, invitation flow), `API_CONVENTIONS.md`, `SECURITY.md` and `README.md` updated; `.env.example` documents new settings
- [ ] `make check` green from a clean checkout; generated-client drift clean; CI green including the Playwright job
- [ ] Human review recorded for auth-flow, permission-model, tenant-isolation, secret-handling and public-API changes (blueprint §33); architecture audit clean (no CRITICAL/MAJOR)

---

# 7. Blueprint Reference Map

Each scope subsection (left column) maps to specific sections of `Internal_Custom_Application_Starter_Architecture_v2.md`. Implementers and reviewers read **only** the listed sections for a given task — not the whole blueprint. This keeps context lean and focused.

## Disambiguating section numbers

Two documents use the `§` symbol. Do not confuse them:

- **Scope §6.x** — a subsection of *this file's* checklist (e.g. Scope §6.2 = "Platform Authorisation Plane").
- **BP §N** — a section of the *blueprint* (e.g. BP §9 = "Organisations, Teams and Permissions", starting at line 379).

The map below always uses `BP §N` for blueprint sections and `Scope §6.x` for this file's checklist. **Never** write a bare `§6` or `§8` — always prefix with `Scope` or `BP` so the next reader knows which document you mean.

## Map

Line ranges were verified against the blueprint's table of contents and by reading each section's start and end. Each range covers the section up to the next `#` heading.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Audit Foundation | **BP §29** (lines 1428–1457), **BP §10** (lines 463–543), **BP §12** (lines 564–635), **BP §13** (lines 636–685) | Append-only audit-event shape and examples, database conventions (UUIDv7, timestamps, JSONB), API style and pagination envelope, structured error envelope the listing endpoint returns |
| **Scope §6.2** Platform Authorisation Plane | **BP §9** (lines 379–461), **BP §8** (lines 325–378), **BP §31** (lines 1523–1575) | Roles-as-permission-bundles model and default-deny rules (the platform plane mirrors it), responsibility split and "authentication is not authorisation", mandatory reusable security tests the platform routes must join |
| **Scope §6.3** WorkOS Organisation Mapping | **BP §9** (lines 379–461), **BP §10** (lines 463–543), **BP §30** (lines 1459–1522) | Organisation as data-isolation boundary and org rules (ids from validated context), column/constraint conventions for the mapping field, security controls for provider credentials behind adapters |
| **Scope §6.4** Bootstrap Platform Admin | **BP §8** (lines 325–378), **BP §29** (lines 1428–1457), **BP §30** (lines 1459–1522) | Identity flow and backend rules (never trust client identity fields; centralised session/webhook validation), audit requirements, security baseline controls relevant to a privileged grant |
| **Scope §6.5** Invitations | **BP §8** (lines 325–378), **BP §9** (lines 379–461), **BP §11** (lines 544–563), **BP §29** (lines 1428–1457) | WorkOS ownership of invitation delivery vs app ownership of memberships/roles, membership and role rules, transaction boundaries for the accept-and-link step, audit examples (`user.invited`) |
| **Scope §6.6** Membership Administration | **BP §9** (lines 379–461), **BP §29** (lines 1428–1457) | Membership rules and role bundles, audit examples (`membership.role_changed`) |
| **Scope §6.7** Feature Flags | **BP §27** (lines 1340–1383) | `organisation_features` table shape and backend enforcement rule |
| **Scope §6.8** WorkOS Webhooks | **BP §8** (lines 325–378), **BP §30** (lines 1459–1522) | Centralised session and webhook validation rule, security baseline (webhook signature verification, input limits) |
| **Scope §6.9** Platform Admin Centre UI | **BP §14** (lines 686–742), **BP §15** (lines 743–778), **BP §16** (lines 779–817), **BP §12** (lines 564–635) | Frontend folder structure and state boundaries (server state in queries, client state in Pinia), generated-client rules (never hand-write duplicates, drift in CI), design-system rules (reusable application components above primitives), pagination/filter conventions for the tables |
| **Scope §6.10** Docs, ADR & Release Governance | **BP §31** (lines 1523–1575), **BP §32** (lines 1576–1626), **BP §33** (lines 1627–1672), **BP §37** (lines 1865–1907) | Integration-test priority and mandatory security tests, tooling and shared Makefile commands, coding-agent governance and the human-review list, CI checks (Playwright smoke, client drift) |

If a task touches a concern not listed here (e.g. the security baseline details for a specific control), consult the blueprint's table of contents and read only the relevant section. When in doubt, read less rather than more — this file's §2–§5 already encodes the v0.4 contract, and `PLATFORM_ADMIN_WORKFLOW_PLAN.md` carries the design rationale.

---

# 8. Status

```text
Release:    v0.4.0 (platform administration)
State:      in progress
Started:    —
Completed:  —
```

When every acceptance criterion in §5 is met and every box in §6 is checked, update the version recording in `pyproject.toml` and `frontend/package.json`, and tag `v0.4.0`. Then open `TEMPLATE_V0_5_SCOPE.md`.
