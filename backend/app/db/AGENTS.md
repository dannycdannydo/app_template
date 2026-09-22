# Database & RLS — Agent Guide

Read this before any change that touches an ORM model, `queries.py`, an Alembic
migration, a database role, or tenant isolation. It is a short orientation and
procedure guide, not the design authority. The authority is
`docs/decisions/0022-postgresql-row-level-security.md` (ADR-0022) and
`docs/rls-rollout.md`; the full table register is
`docs/rls-table-inventory.md`. Update this guide in the same change that alters
a rule, invariant or gotcha below.

## The model in one paragraph

The organisation is the hard tenant boundary. The **application layer enforces
it first**: `X-Org-Id` → validated active membership → an explicit
`organisation_id` predicate in every query → a foreign row is a `404`. PostgreSQL
Row-Level Security is a second, database-side **default-deny backstop** so that
a missed or wrong predicate returns *no* rows instead of foreign rows. Never
weaken the application layer when adding RLS, and never use RLS to replace
permissions or `404` behaviour. The target invariant: an ordinary application
connection cannot read or change an organisation-owned row unless trusted
transaction-local context authorises that row.

## Roles — separate credentials per process

| Role | Credential | Used by | Attributes / scope |
| --- | --- | --- | --- |
| `app_owner` | `DATABASE_URL` | Alembic DDL/seed only | owns the schema; never the runtime path |
| `app_runtime` | `DATABASE_RUNTIME_URL` | API and Dramatiq workers | non-owner, `NOBYPASSRLS`, `NOINHERIT`; subject to every policy |
| `app_coordinator` | `DATABASE_COORDINATOR_URL` | outbox coordinator, reliability metrics, `reconcile_jobs` | second non-bypass role, policies scoped to dispatch state, not a tenant |
| `app_metrics` | none (NOLOGIN) | the `SECURITY DEFINER` aggregate delivery count | narrow policy; no tenant read |
| `app_operator` | `DATABASE_OPERATOR_URL` | backup/restore, support, emergency CLI only | the **only** application role that may carry `BYPASSRLS`; owns no table, member of nothing |

`resolve_database_url` / `resolve_coordinator_database_url` /
`resolve_operator_database_url` (`app/db/session.py`) select the credential and
refuse to fall back in production. The startup gates in
`app/db/role_checks.py` prove the runtime/coordinator credentials are non-owner,
non-`BYPASSRLS` and cannot assume `app_operator`; they run from the API lifespan,
the worker and the coordinator. `make verify-db-roles` runs the same check
before deployment. Never load `app_operator` from an HTTP process or a worker.

## Tenant context — transaction-local, fail-closed

- Context is bound with a **parameterised** `set_config(name, value, true)` via
  the helpers in `app/db/rls.py` (`bind_organisation_context`,
  `bind_user_context`, `bind_job_context`, `bind_invitation_provider_context`,
  `bind_platform_context`, `bind_platform_service_context`). Never interpolate a
  request value into SQL.
- The setting is **transaction-local**: it clears automatically on commit,
  rollback, exception, cancellation, timeout and pool reuse. It is deliberately
  **not** re-applied to a later transaction. **Any code that commits must rebind
  before its next protected read or write.**
- The application API binds `app.user_id` after authentication and
  `app.organisation_id` only after `get_current_membership` confirms an active
  membership (`app/api/dependencies.py`). The organisation always comes from the
  validated membership row, never the header body directly.
- `app_current_tenant_id()` returns `NULL` for absent/empty/malformed settings,
  so it matches no row. A `NULL` tenant key never means "all rows".
- Workers bootstrap with `app.job_id` (single-row `jobs` read by opaque id),
  clear it, then bind the durable row's own `organisation_id` / creator user.

## Adding a new table

1. Add the model (subclass `Base` from `app/db/base.py`) and register the model
   module in that file's import block so it lands on `Base.metadata`.
2. **Classify it** in `backend/tests/tenant_isolation_registry.py`. The
   structural suite fails on any unclassified model. Choose one:
   `organisation_owned` (carries `organisation_id`), `user_private`
   (`organisation_id` + recipient key), `indirect` (real parent FK),
   `global`, `platform` or `operational`.
3. Add an Alembic migration (every schema change) and, if the table holds tenant
   data, a **bounded, additive, reversible** group migration following the
   existing `*_rls_*_group_enablement.py` pattern:
   - grant **only the new table/sequence** to `app_runtime` (do not copy the
     historical `GRANT ... ON ALL TABLES IN SCHEMA public`, which over-grants
     every table present at that revision — see Gotchas);
   - install `CREATE POLICY <table>_organisation_isolation FOR ALL USING
     (organisation_id = app_current_tenant_id()) WITH CHECK (...)`; user-private
     adds `AND user_id = app_current_user_id()`; indirect uses a parent `EXISTS`
     or a denormalised key tied to the parent by a composite FK;
   - `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` **and** `FORCE`;
   - grant `app_coordinator`/`app_operator` only what the path truly needs, and
     at **column level** for UPDATE wherever possible;
   - write a downgrade that reverses exactly this migration.
4. Update `docs/rls-table-inventory.md` (classification, readers/writers, access
   paths) and `docs/rls-rollout.md` §3.
5. Add a real-PostgreSQL group suite (below). `backend/app/db/AGENTS.md` is
   registered in the root `AGENTS.md` area index.
6. Human review before apply-and-commit — tenant-isolation, migration and
   database-role changes are all review-gated (repo `AGENTS.md`).

## Adding a query or endpoint that touches tenant data

- Keep the router thin; put the `organisation_id` predicate in the service /
  `queries.py`, and return `404` for a foreign row.
- Prefer an explicitly org-scoped `select()`. Raw SQL, bulk updates/deletes and
  relationship/lazy loads are the high-risk paths RLS exists to catch — review
  them against `docs/rls-table-inventory.md` §3.
- API org routes get context for free from the membership dependency; a service
  that commits must rebind (see Tenant context). Cross-tenant sweeps must not use
  a bypass: iterate the global `organisations` table and bind each tenant, or
  use a narrow non-bypass aggregate like `app_metrics`.
- Platform routes run on `app_runtime` under RLS; cross-tenant platform access
  uses the validated `app.platform_admin` flag (audit) or the narrow
  `app.platform_service` flag (bootstrap/webhook/recovery) — never a bypass.
- New protected `/api/v1` endpoints **must** be added to `PROTECTED_ROUTES` in
  `backend/tests/test_security_suite.py`; the completeness test fails otherwise,
  and the table-driven suite then checks auth, cross-organisation denial,
  viewer-write denial and non-disclosure. Add cross-org coverage in
  `backend/tests/test_org_isolation_matrix_db.py`.

## Gotchas

- **Classification is not enforcement.** `test_tenant_isolation_registry.py`
  proves every model is classified, not that a tenant table has a policy. There
  is currently no automated registry→database check, so shipping the group
  migration and its real-PostgreSQL suite is on you.
- **`GRANT ... ON ALL TABLES`** appears in the historical group migrations and
  grants DML on *every* table present at that revision; group 7 had to revoke the
  inherited table-wide DML on the platform tables. Grant per-table for new work.
- **`SELECT ... FOR UPDATE` is governed by the UPDATE policy as well as SELECT.**
  A permissive job-keyed bootstrap UPDATE policy would authorise a real update,
  which is why the worker bootstrap is `SELECT`-only and takes the row lock under
  tenant context.
- **`INSERT ... RETURNING` applies SELECT policies.** Append-only ledgers carry
  Python-side timestamp defaults so a writer never needs a `RETURNING` read it
  may not be permitted.
- **Commits clear context.** The most common RLS bug in new code is a
  self-committing service that then reads a protected row without rebinding.
- **Indirect tables:** prefer a denormalised non-null `organisation_id` tied to
  the parent by a composite FK; otherwise use a parent `EXISTS` policy. Keep a
  read policy separate from a tenant-checked write policy where a pre-tenant
  user context must read but never write (see `membership_roles`).
- **Platform-managed, organisation-owned tables** need the platform service to
  bind exactly the targeted organisation after the permission check (the
  reviewed group-4a exception). Re-review this for every new such table.
- **Provider/control-plane single-row bootstraps** (job id, webhook provider id)
  bind one opaque id to read/lock exactly that row; they are never a tenant
  authority and must not be `FOR ALL`.

## Testing — real PostgreSQL, not mocks

- `backend/tests/tenant_isolation_registry.py` + `test_tenant_isolation_registry.py`
  — every model classified, real parent FKs.
- `backend/tests/test_rls_*_enablement_db.py` — per-group own vs foreign
  select/insert/update/delete, tenant-key move, missing-context fail-closed,
  pool reuse, representative `EXPLAIN` plans, and migration
  downgrade+re-upgrade.
- `backend/tests/test_rls_indirect_rows_db.py` — cross-cutting indirect-row
  conformance.
- `backend/tests/test_rls_records_db.py`, `test_rls_operator_credential_boundary.py`
  — prototype and role-boundary proof.
- `backend/tests/test_org_isolation_matrix_db.py` — the two-organisation
  application-layer matrix.
- Shared fixtures/seeds live in `backend/tests/rls_helpers.py` and
  `org_isolation_helpers.py`. These suites **skip when no PostgreSQL is
  reachable**, so a green DB-less local run does not prove them — CI provisions
  `postgres:17`.

## Files worth knowing

- Context + policy setting names: `app/db/rls.py`
- Engine/credential resolution: `app/db/session.py`
- Startup/deployment role gates: `app/db/role_checks.py`
- Membership → context binding: `app/api/dependencies.py`
- Classification registry: `backend/tests/tenant_isolation_registry.py`
- Migration examples: `backend/alembic/versions/*_rls_*_group_enablement.py`
- Design/rollback/register: `docs/decisions/0022-postgresql-row-level-security.md`,
  `docs/rls-rollout.md`, `docs/rls-table-inventory.md`
- Operations & recovery: `docs/operations.md`, `docs/backup-and-recovery.md`
