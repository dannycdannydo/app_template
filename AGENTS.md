# AGENTS.md

Instructions for human and AI contributors working in this repository. The canonical design standard is `Internal_Custom_Application_Starter_Architecture_v2.md`; the release contract is `TEMPLATE_V0_3_SCOPE.md` (see its §7 reference map for which blueprint sections apply to each work unit).

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
- Keep the mandatory security suite green: new protected `/api/v1` endpoints must be added to `PROTECTED_ROUTES` in `backend/tests/test_security_suite.py` (blueprint §31, v0.2 Scope §6.6), which then checks unauthenticated/invalid-session/disabled-user rejection, cross-organisation denial, viewer-write denial and stack-trace non-exposure for the new route.
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
