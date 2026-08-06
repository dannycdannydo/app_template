# Template v0.3 — Scope & Progress Log

## Relationship to other documents

- `Internal_Custom_Application_Starter_Architecture_v2.md` is the long-term **design standard**.
- `IMPLEMENTATION_GUIDE.md` is the **build plan** and the incremental release sequence.
- This file is the **scoped contract for the v0.3 release**. It defines exact deliverables, exclusions, acceptance tests, and the commands that must work. It also serves as a progress log: check items off as they are completed.

---

# 1. Goal of v0.3

A **frontend application shell** on top of the v0.2 identity core. After v0.3, a fresh clone can sign in through WorkOS from the browser, land in a protected application layout with a sidebar, user menu and organisation selector, and operate the tenant-scoped example module (records) through standard table and form components with toasts and consistent error handling. Per `IMPLEMENTATION_GUIDE.md`: "the starter should feel like a genuine application rather than a technical demo."

v0.3 establishes every convention later releases inherit for the frontend: the generated-client consumption pattern, the TanStack Query composable layer, Pinia's client-state boundary, the auth adapter seam, the standard table/form/toast building blocks, and the Playwright journey approach.

---

# 2. In Scope

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

WorkOS owns login and session management end-to-end (`IMPLEMENTATION_GUIDE.md` §Template v0.3; blueprint §8). The frontend obtains a WorkOS session token and presents it to the backend, which validates it exactly as v0.2 established. The frontend **never** submits identity fields (name, email, user id) to the backend — only the token. UI permission awareness (hiding actions) is cosmetic; the backend remains the enforcement point ("authentication is not authorisation").

The v0.1 foundation already ships the generated-client pipeline (`scripts/generate-client.mjs`, `src/api/client.ts`, `src/api/generated/openapi.d.ts`), TanStack Vue Query, Pinia, Vue Router, shadcn-vue primitives, Vitest and Playwright. v0.3 builds the application shell on that foundation; it is not a greenfield frontend build.

Explicit deliverables:

- Auth adapter module behind which the WorkOS browser SDK is confined (`src/features/auth/`), exposing only `startLogin`, `completeLogin`, `signOut`, `getSession` — per "provider SDKs stay behind adapters".
- Login view + auth-callback route that completes the WorkOS authorization-code flow and stores the session; logout.
- Router guard: every route except login/callback requires a session; session expiry (`401`) clears state and returns to login.
- Application shell: collapsible sidebar, header, user menu (identity from `GET /api/v1/me`, logout), and an organisation selector driven by the user's memberships, persisted client-side, that sets `X-Org-Id` on subsequent requests.
- API client hardening in `src/api/client.ts`: Bearer-token injection and central `401`/error handling using the standard API error envelope (`code`, `message`, `details`, `request_id`).
- Server-state composables in `src/queries/` for `me`, organisations, memberships and records (queries + mutations with invalidation), built on the generated client.
- `DataTable` application component over TanStack Table + shadcn-vue table primitives, consuming the standard pagination envelope (`items`, `page`, `page_size`, `total`), with loading/empty/error states.
- Standard form primitives (shadcn-vue form with schema validation) and toast integration (vue-sonner per shadcn-vue toast docs), with API errors mapped to toasts.
- Records feature screens (list / create / edit / delete) proving the whole shell, with permission-aware UI derived from `/me` (a viewer sees no write actions).
- Vitest component tests for auth, router guard, selector, table and form; Playwright journeys for the authenticated shell and the records CRUD flow.
- New frontend dependencies (WorkOS browser SDK, `@tanstack/vue-table`, `vue-sonner`, form/validation packages) justified in an ADR — no dependency without documentation.
- `.env.example` additions: `VITE_WORKOS_CLIENT_ID` (public) and any auth-callback variables; `VITE_API_BASE_URL` already exists.

Backend surface for v0.3 is the v0.2 surface (`me`, `organisations`, `records`). No new backend endpoints are anticipated; if the frontend contract reveals a gap, it is raised in the daily loop for explicit approval rather than assumed.

---

# 3. Out of Scope (Explicitly Deferred)

These are **not** part of v0.3. They appear in later releases per `IMPLEMENTATION_GUIDE.md`.

| Capability | Deferred to |
| --- | --- |
| Storage interface, S3-compatible adapter, MinIO, signed uploads | v0.4 |
| Dramatiq, Redis queue wiring, durable job records, job progress polling | v0.4 |
| Audit log and audit events | v0.5 |
| WorkOS webhook consumers (event processing) | v0.5 |
| Structured JSON logging, Sentry, email provider, notifications | v0.5 |
| Hybrid VPS deployment, backup and recovery documentation | v0.5 |
| Teams (`teams`, `team_memberships`) and team-specific permissions | post-v1 (no planned release slot; blueprint §9 adds them only when required) |
| Advanced data grids (AG Grid, Handsontable) | post-v1 (blueprint §16: dedicated grids wrapped behind internal components when a project genuinely needs them) |
| Server-side rendering, multi-language UI / i18n | post-v1 |
| Managed Azure reference deployment | post-v1 |
| Transactional outbox, import/export framework, DB feature flags | post-v1 |

---

# 4. Commands That Must Work

All v0.1/v0.2 commands remain part of the quality gate. `make generate-client` now also produces types for the v0.2 endpoints (`me`, `organisations`, `records`) and the drift check stays in `make check`. One new target is required: `make e2e` (Playwright journeys). The CI workflow gains a Playwright smoke job per blueprint §37.

```bash
make dev              # Postgres + Redis in Docker; API + frontend native with live reload (ADR-0008)
make dev-docker       # entire stack in containers (CI parity, onboarding, Dockerfile validation)
make migrate          # run Alembic migrations
make lint             # Ruff (backend) + ESLint/oxlint (frontend)
make typecheck        # Pyright (backend) + vue-tsc (frontend)
make test             # pytest (backend) + Vitest (frontend)
make format           # Ruff format + Prettier
make generate-client  # export OpenAPI from FastAPI -> generate TS client (openapi-typescript)
make e2e              # NEW: run Playwright journeys against the local stack
make check            # full local quality gate (lint + typecheck + test + drift)
```

`make dev` for v0.3 requires `VITE_WORKOS_CLIENT_ID` set in `.env` for the login flow to reach WorkOS; `.env.example` documents it.

---

# 5. Acceptance Criteria

v0.3 is done when **all** of the following are true:

1. **Generated client, no hand-written duplicates**: `make generate-client` regenerates `frontend/src/api/generated/openapi.d.ts` with no diff; the generated file contains the `me`, `organisations` and `records` endpoint types; a repo-wide search proves no hand-written TypeScript interfaces duplicate backend request/response schemas outside the generated file.
2. **API client and auth header**: every API call in `frontend/src` goes through `src/api/client.ts`; a Vitest test proves the client attaches the session token as a Bearer `Authorization` header and that a `401` response clears the session store and redirects to `/login`.
3. **Login flow**: with the WorkOS adapter stubbed, Vitest proves: starting login redirects to the WorkOS authorization URL with the configured client id; the callback route completes the flow and stores a session; a denied/failed flow shows an error and stores nothing; the frontend never submits identity fields to the backend (only the token).
4. **Protected routes**: with no session, every route except `/login` and the auth callback redirects to `/login` (router guard, proved by Vitest and a Playwright check); with a session, visiting `/login` redirects to the shell; logout clears the session and returns to `/login`.
5. **Main layout and sidebar**: the shell renders a collapsible sidebar, header and user menu; the user menu shows the current user's name/email from `GET /api/v1/me` and offers logout; the sidebar collapsed state persists across reloads via Pinia.
6. **Organisation selector**: the selector lists the memberships returned by `/me`; selecting one persists it in Pinia (client state only) and sets `X-Org-Id` on subsequent API requests (a test asserts the header); switching organisation invalidates org-scoped queries.
7. **Standard table**: `DataTable` renders the standard pagination envelope (`items`, `page`, `page_size`, `total`), supports page navigation via `?page`/`page_size`, and renders loading, empty and error states; covered by Vitest with a mocked query result.
8. **Standard form and toast/error handling**: the form surfaces inline field-validation errors; API errors from the envelope (`code`, `message`, `details`) appear as toasts; a successful create/update navigates to the updated list; covered by Vitest component tests.
9. **Records feature module**: an authenticated Playwright journey (test-profile session) lists, creates, edits and deletes records in the selected organisation through the generated client; a user without write permissions (e.g. `viewer`) sees no write actions, while the backend still rejects direct writes with `403` (enforcement is server-side).
10. **Query-layer architecture**: a repo-wide search proves no Vue component or Pinia store imports `src/api/client.ts` directly — every HTTP call lives in a `src/queries/` composable; Pinia stores hold client state only (no server data, per blueprint §14); each shell data read flows through a query composable built on the generated client.
11. **Governance and audit**: `make check` passes from a clean checkout with zero lint errors, zero type errors, green tests and a diff-free generated client; `make e2e` passes against the local stack; new frontend dependencies are justified in an ADR; `.env.example` documents every new `VITE_*` variable; `ARCHITECTURE.md` and `README.md` describe the frontend shell; auth-flow and any public-API changes were human-reviewed per blueprint §33; the architecture audit (`prompts/04-architecture-audit.md`) reports no CRITICAL or MAJOR findings.

---

# 6. Progress Log

Check items off as they are completed. Keep one task `in_progress` at a time where practical.

Subsections are ordered so later work builds on earlier work: the generated client and error surface precede the auth flow that feeds it, the auth flow precedes the protected shell that consumes it, the shell precedes the query layer and building blocks, and the records feature module proves the whole stack. Dependencies are noted per subsection.

## 6.1 Generated Client & API Client Hardening

Foundation for everything else; §6.2–§6.7 depend on the typed client and the error/`401` surface.

- [x] `make generate-client` regenerates `openapi.d.ts` covering `me`, `organisations`, `records`; drift gate stays in `make check` (blueprint §15)
- [x] `src/api/client.ts` gains Bearer-token injection from the session store and a central `401` handler (clear session, redirect to login)
- [x] Error normalization: the standard envelope (`code`, `message`, `details`, `request_id`) is parsed into a typed client error (blueprint §13) used by toasts and forms
- [x] Audit pass: no hand-written API interfaces in `frontend/src`; every backend call goes through `client.ts` (blueprint §15 rules)

## 6.2 Auth Flow & Session Store

Depends on §6.1 (client + error surface). Implements the WorkOS authorization-code flow behind an adapter.

- [x] `src/features/auth/workos.ts` adapter — the only module importing the WorkOS browser SDK; exposes `startLogin`, `completeLogin`, `signOut`, `getSession`
- [x] `VITE_WORKOS_CLIENT_ID` (+ callback URL config) added to settings handling and `.env.example`
- [x] Login view (`/login`) — "Continue with WorkOS" starts the flow; no identity fields are ever collected or submitted
- [x] Auth callback route — completes the code flow, stores the session, redirects to the shell; failure shows an error and returns to login
- [x] Session store (Pinia) — token/session state, `isAuthenticated`, logout; client state only, no server-state caching
- [x] Vitest coverage: start/completed/denied flow with stubbed adapter; no identity fields sent to the backend

## 6.3 Protected Routes & Application Shell

Depends on §6.2 (session). Builds the navigable shell with organisation context.

- [x] Router guard `requiresAuth` — redirects to `/login` without a session, and away from `/login` with one; callback route is always public
- [x] Main layout — collapsible sidebar (shadcn-vue sheet/drawer primitives), header, mobile handling; sidebar state in Pinia (`stores/ui.ts`) and persisted
- [x] User menu — current user from `GET /api/v1/me` (name, email), logout action
- [x] Organisation selector — memberships from `/me`, persisted selected organisation in Pinia, sets `X-Org-Id` on the client (blueprint §14 client-state boundary)
- [x] Vitest/component tests for the guard, layout, user menu and selector

## 6.4 Server-State Query Layer

Depends on §6.1 and §6.3. TanStack Vue Query owns all server state; components never touch the HTTP client (blueprint §14, §15).

- [x] `useMeQuery` — current user, memberships, roles (drives user menu and org selector)
- [x] Records query composables — list (paginated, org-scoped), detail; mutations (create, update, delete) with invalidation
- [x] Organisation-switch invalidation — changing the selected org refetches org-scoped queries
- [x] Query-key convention documented (per-org keys), matching the API envelope and filter/sort conventions (blueprint §12)

## 6.5 Standard Table (`DataTable`)

Depends on §6.4 (query state). The reusable table every list screen uses.

- [x] shadcn-vue table primitives added; `DataTable` application component over TanStack Table (blueprint §16 data grids)
- [x] Pagination bound to the standard envelope (`items`, `page`, `page_size`, `total`) with page controls wired to query state
- [x] Loading, empty and error states (error state consumes the typed client error)
- [x] Vitest coverage with a mocked query result

## 6.6 Standard Form & Toast / Error Handling

Depends on §6.1 (error envelope) and §6.4. The reusable form + feedback pattern every edit screen uses.

- [x] Form primitives (shadcn-vue form) with schema validation; reusable field error presentation
- [x] Toast integration (vue-sonner per shadcn-vue docs) wired to the error envelope and to success messages
- [x] Submission flow — validation errors inline, API errors as toasts, success navigates to the list (blueprint §13 mappings)
- [x] Vitest coverage: inline errors, API-error toast, success navigation

## 6.7 Records Feature Module (Shell Proof)

Depends on §6.3–§6.6. Proves the shell end-to-end on the v0.2 tenant-scoped module.

- [x] Records list view — `DataTable` + `useRecordsQuery` + org selector context; viewer sees read-only UI (no write actions)
- [x] Record create form — standard form + toast; round-trips through the generated client
- [x] Record edit form and delete action with confirmation; permission-aware visibility derived from `/me`
- [x] Playwright journeys — authenticated shell (injected test-profile session): navigate to records, create a record, see it in the list, edit and delete it; unauthenticated visit redirects to login; **the successful callback round-trip is explicitly covered** (`/auth/callback?code=…` → session stored → redirect to `/`), per §6.2 review feedback on the boot-restore × history-snapshot coupling

## 6.8 Tests, Docs & Release Governance

Depends on §6.7 (exercises the shell). Closes the release.

- [ ] Vitest component/unit tests consolidated; Playwright smoke journeys wired into CI (blueprint §31, §37)
- [ ] `make e2e` target added to the root Makefile and documented (blueprint §32 shared commands)
- [ ] ADR(s) documenting new frontend dependencies (WorkOS browser SDK, `@tanstack/vue-table`, `vue-sonner`, form/validation packages) — repo rule: no dependency without documentation
- [ ] `.env.example` documents `VITE_WORKOS_CLIENT_ID` and any auth-callback variables
- [ ] Docs updated: `ARCHITECTURE.md` (frontend shell, auth flow, state boundaries), `README.md` (login to try the demo), `AGENTS.md` if frontend rules change
- [ ] Stale `Scope §6.x` citations disambiguated under the v0.3 numbering (review 00b): `AGENTS.md` security-suite citation repointed to `v0.2 Scope §6.6`; `API_CONVENTIONS.md` (§6.4, §6.6) and `SECURITY.md` (§6.4) citations repointed; project convention recorded — bare `Scope §6.x` in v0.2-era backend code/docstrings refers to `TEMPLATE_V0_2_SCOPE.md`, and new code prefixes the version (e.g. `v0.2 Scope §6.3`) so the daily loop never greps the wrong subsection
- [ ] `make check` green from a clean checkout; generated-client drift clean; CI green including the Playwright job
- [ ] Human review recorded for auth-flow changes and any public-API breaks (blueprint §33)
- [ ] Architecture audit (`prompts/04-architecture-audit.md`) clean — no CRITICAL or MAJOR findings

---

# 7. Blueprint Reference Map

Each scope subsection (left column) maps to specific sections of `Internal_Custom_Application_Starter_Architecture_v2.md`. Implementers and reviewers read **only** the listed sections for a given task — not the whole blueprint. This keeps context lean and focused.

## Disambiguating section numbers

Two documents use the `§` symbol. Do not confuse them:

- **Scope §6.x** — a subsection of *this file's* checklist (e.g. Scope §6.3 = "Protected Routes & Application Shell").
- **BP §N** — a section of the *blueprint* (e.g. BP §8 = "Authentication with WorkOS", starting at line 325).

The map below always uses `BP §N` for blueprint sections and `Scope §6.x` for this file's checklist. **Never** write a bare `§6` or `§8` — always prefix with `Scope` or `BP` so the next reader knows which document you mean.

## Map

Line ranges were verified against the blueprint's table of contents and by reading each section's start and end. Each range covers the section up to the next `#` heading.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Generated Client & API Client Hardening | **BP §15** (lines 743–778), **BP §13** (lines 636–685), **BP §12** (lines 564–635) | Generated-client flow and rules (never hand-write duplicates, drift in CI), structured error envelope and exception mappings, API style, pagination and filtering conventions |
| **Scope §6.2** Auth Flow & Session Store | **BP §8** (lines 325–378), **BP §14** state management (lines 719–739), **BP §30** (lines 1459–1522) | Responsibility split and identity flow (WorkOS owns login/session; app never trusts identity fields), Pinia client-state boundary vs server state, security controls relevant to the frontend token path |
| **Scope §6.3** Protected Routes & Application Shell | **BP §14** (lines 686–742), **BP §16** (lines 779–817), **BP §5** (lines 156–216) | Frontend folder structure and Vue conventions, design-system rules (semantic tokens, reusable application components above primitives), the backend module surface the shell consumes (`me`, organisations) |
| **Scope §6.4** Server-State Query Layer | **BP §14** state management (lines 719–739), **BP §15** (lines 743–778), **BP §12** (lines 564–635) | TanStack Query ownership of fetching/caching/pagination/invalidation, composable flow (generated client → query composables → components), pagination envelope and filter/sort conventions |
| **Scope §6.5** Standard Table (`DataTable`) | **BP §16** data grids (lines 800–814), **BP §12** pagination (lines 589–624), **BP §14** (lines 686–742) | Table default (shadcn-vue + TanStack Table; advanced grids deferred), envelope-driven pagination, component conventions and file layout |
| **Scope §6.6** Standard Form & Toast / Error Handling | **BP §13** (lines 636–685), **BP §16** (lines 779–817) | Error envelope and exception-to-HTTP mappings consumed by the client, design-system rules for form/toast primitives |
| **Scope §6.7** Records Feature Module (Shell Proof) | **BP §14** (lines 686–742), **BP §15** (lines 743–778), **BP §12** (lines 564–635) | Feature-oriented module layout, generated-client consumption, pagination/filtering surface of the records API |
| **Scope §6.8** Tests, Docs & Release Governance | **BP §31** (lines 1523–1575), **BP §32** (lines 1576–1626), **BP §33** (lines 1627–1672), **BP §37** (lines 1865–1907), **BP §38** (lines 1908–1931), **BP §42** (lines 2042–2063), **BP §44** (lines 2086–2116), **BP §45** (lines 2117–2138) | E2E strategy (Playwright for critical journeys), frontend tooling and shared Makefile commands, coding-agent governance and human-review list, CI checks (Playwright smoke, client drift), environment separation (frontend URL / API URL / WorkOS environment), template validation, implementation order steps 8–10 (Vue shell, generated client, design system), v0.3-relevant readiness items |

If a task touches a concern not listed here (e.g. the security baseline details for a specific control), consult the blueprint's table of contents and read only the relevant section. When in doubt, read less rather than more — this file's §2–§5 already encodes the v0.3 contract.

---

# 8. Status

```text
Release:    v0.3.0 (frontend application shell)
State:      in progress
Started:    —
Completed:  —
```

When every acceptance criterion in §5 is met and every box in §6 is checked, update the version recording in `pyproject.toml` and `frontend/package.json`, and tag `v0.3.0`. Then open `TEMPLATE_V0_4_SCOPE.md`.
