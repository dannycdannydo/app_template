# AGENTS.md

Instructions for human and AI contributors working in this repository. The canonical design standard is `Internal_Custom_Application_Starter_Architecture_v2.md`; the release contract is `TEMPLATE_V0_1_SCOPE.md` (see its §7 reference map for which blueprint sections apply to each work unit).

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
- Tests accompany behavioural changes.
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
