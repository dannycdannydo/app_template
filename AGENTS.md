# AGENTS.md

Instructions for human and AI contributors working in this repository. The canonical design standard is `Internal_Custom_Application_Starter_Architecture_v2.md`; the release contract is `TEMPLATE_V0_9_SCOPE.md` (see its reference map for which blueprint sections apply to each work unit).

## Mandatory agent rules

- Read the architecture documentation before structural changes.
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
- Frontend HTTP calls happen only in `src/queries/` composables: no Vue component or Pinia store imports `src/api/client.ts` directly; Pinia stores hold client state only (server state belongs to TanStack Query).
- Scope citations are version-prefixed (`v0.2 Scope §6.3`, `v0.3 Scope §6.7`). A bare `Scope §6.x` found in v0.2-era backend code or docstrings refers to `TEMPLATE_V0_2_SCOPE.md`; new code always prefixes the release version so the daily loop never greps the wrong subsection.
- Tests accompany behavioural changes.
- Keep the mandatory security suite green: new protected `/api/v1` endpoints must be added to `PROTECTED_ROUTES` in `backend/tests/test_security_suite.py` (blueprint §31, v0.2 Scope §6.6), which then checks unauthenticated/invalid-session/disabled-user rejection, cross-organisation denial, viewer-write denial and stack-trace non-exposure for the new route. Platform routes (v0.4 Scope §6.2) additionally get the non-platform-admin `403` case and the cross-plane denial checks; they take no `X-Org-Id`.
- Do not weaken linting, typing or tests.
- Do not refactor unrelated code without a clear reason.
- Do not add dependencies without documenting why (see ADRs).

## Human review required

The following changes require human review before they are applied:

- authentication changes;
- permission-model changes;
- tenant-isolation changes;
- destructive migrations;
- secret handling;
- public API breaks;
- infrastructure changes;
- backup and recovery changes;
- major dependency additions.

## Working procedure

Work proceeds in a three-step loop per work unit: **implement → review → apply-and-commit**. See `CONTRIBUTING.md` for the workflow, commit style, and review requirements. Never commit work that has not been reviewed.

## Area guides for AI agents

Each major area of the app ships a short, current `AGENTS.md` summary at the root of that area, so an agent that picks up work there can load the essentials without reading the full design docs. **Read the area guide before starting work in that area**, in addition to this file. Keep the guide current in the same change that alters the area's rules, invariants, procedures or gotchas — a stale guide is worse than none. The root file indexes the guides roughly by area:

| Area | Guide | Read it when working on |
| --- | --- | --- |
| Backend (services, API, auth & permissions) | `backend/AGENTS.md` | backend layout, routers/services/queries, errors, dependencies, authentication and the permission model, config, observability, testing |
| Backend — database and RLS | `backend/app/db/AGENTS.md` | ORM models, `queries.py`, Alembic migrations, tenant isolation, RLS policies, database roles and grants |
| Backend — AI subsystem | `backend/app/ai/AGENTS.md` | providers, models, prompts, tasks, AI execution, persistence, transfer/scratch |
| Backend — durable jobs, outbox & coordinator | `backend/app/job_coordinator/AGENTS.md` | jobs, outbox, retries/leases, reconciliation, maintenance runs, coordinator role |
| Frontend | `frontend/AGENTS.md` | views, queries, stores, generated client, routing, permissions and frontend tests |

Further area guides follow the same nested-`AGENTS.md` convention and are added here as they are written.
