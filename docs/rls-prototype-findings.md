# RLS P2 Prototype - Findings

Status: P2 evidence for
`plans/2026-09-18-postgresql-row-level-security-plan.md` (work unit P2 -
`records` prototype) and design detail for
`docs/decisions/0022-postgresql-row-level-security.md`.

This records the prototype's performance and operational findings against real
PostgreSQL with a restricted runtime role. It is evidence for the adoption gate
after P2; approval of the prototype is **not** an approval of a production
rollout (P3/P4).

## What the prototype installs

- Migration `c1d2e3f4a5b6_rls_records_prototype.py` creates the `app_runtime`
  role (`NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS`),
  grants it ordinary DML on the current schema, `ENABLE`s and `FORCE`s RLS on
  `records` and `record_revisions`, and installs one `FOR ALL` policy per table
  with matching `USING` and `WITH CHECK`.
- The role is provisioned safely over the documented two-stage flow. If
  `app_runtime` is absent the migration creates it and marks itself as the
  role's owner with a role comment. If the role already exists (a deployment
  pre-provisioned it with its own login credential) the migration adopts it:
  it forces `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS`,
  refuses to continue if the role owns a protected table, and revokes every
  membership that could let it `SET ROLE` to a superuser, a `BYPASSRLS` role or
  a protected-table owner. The downgrade drops the role **only** when it
  carries this migration's ownership marker, so an adopted, deployment-managed
  role and its out-of-band credential are left in place (with the prototype
  grants, policies and function removed).
- `app_current_tenant_id()` reads the transaction-local
  `app.organisation_id` setting with `current_setting(..., true)` and returns
  `NULL` for absent, empty or malformed values. A `NULL` predicate matches no
  row and authorises no write.
- The application binds context through `app/db/rls.py` using a parameterised
  `set_config('app.organisation_id', $1, true)` only after
  `get_current_membership` has validated an active membership. The setting is
  **transaction-local and never re-applied automatically**: after a commit,
  rollback or exception the context is absent and a protected read is
  default-denied until the caller explicitly rebinds a freshly validated
  organisation. The records service keeps its post-write `refresh` inside the
  write transaction, so it does not depend on a second, re-contextualised
  transaction.
- The normal application path uses `DATABASE_RUNTIME_URL`
  (`app/db/session.py` → `resolve_database_url`). A configured runtime URL
  always wins; a production process **refuses to start without one**, so the
  schema-owner/migration `DATABASE_URL` can never become the ordinary
  production application credential. Outside production (local development and
  the default test profile) an empty runtime URL uses `DATABASE_URL`, where the
  local `app` role is a superuser and no policy is enforced.

## Measured query plan and latency

Representative list and detail plans recorded with the **normal** planner
settings (no `enable_seqscan` hint), on a multi-tenant table of 40
organisations x 50 rows per organisation, runtime role, tenant context bound::

```text
Limit
  ->  Sort  (Sort Key: created_at DESC, id DESC)
        ->  Result
              One-Time Filter: (app_current_tenant_id() = '<uuid>'::uuid)
              ->  Bitmap Heap Scan on records
                    Recheck Cond: (organisation_id = '<uuid>'::uuid)
                    ->  Bitmap Index Scan on ix_records_organisation_id_created_at
                          Index Cond: (organisation_id = '<uuid>'::uuid)
```

```text
Index Scan using pk_records on records
  Index Cond: (id = '<uuid>'::uuid)
  Filter: (organisation_id = app_current_tenant_id())
```

Neither representative plan contains a sequential scan. The policy adds a
`One-Time Filter`; the existing `ix_records_organisation_id_created_at`
composite index serves the organisation filter and the newest-first sort, and
the detail path uses the primary-key index. A metadata-only table (two rows)
would legitimately prefer a sequential scan, so the suite additionally keeps a
separate forced-plan check (`enable_seqscan = off`, in its own rolled-back
transaction) proving the composite index is available.

Measured end-to-end statement latency under the normal planner settings
(300-500 row organisations, a single connection, including the RLS predicate; a
run with 40 x 50 rows and a run with 300 rows produced the same order of
magnitude):

| Operation | Latency |
| --- | --- |
| List page (50) | ~2.7 ms |
| Detail by id | ~2.4 ms |
| Insert | ~5.2 ms |

These are informational, not a benchmark: the bounded check in
`backend/tests/test_rls_records_db.py` is a regression signal, not a
machine-speed assertion. No sequential-scan regression was observed on the
representative list/detail operations.

## Operational findings

1. **No context survives a transaction boundary.** The context is a
   transaction-local setting, so it is gone after every commit, rollback,
   exception, cancellation, timeout and pooled-connection reuse. The same
   session must explicitly rebind a validated organisation before its next
   protected read; the lifetime tests assert the post-boundary denial directly,
   in the same session, in addition to the pool-reuse tests. The records
   service's post-write `refresh` runs before the commit so the write and the
   refresh share one context-bearing transaction.
2. **Role provisioning is two-stage and safe to re-run.** The migration creates
   the role `NOLOGIN` so no credential is checked in; a deployment grants its
   own login credential out of band (the focused test grants a throwaway one).
   A pre-existing `app_runtime` is adopted only after its attributes and
   memberships are corrected, and the downgrade never drops a role this
   migration did not create.
3. **Production requires the runtime credential.** `resolve_database_url`
   refuses to start a production process without `DATABASE_RUNTIME_URL`, so the
   ordinary production path cannot silently use the schema-owner credential
   (ADR-0022 decision 2). The role-attribute startup check itself remains P4.
4. **Grants are complete only at this head revision.** The prototype grants DML
   on every table that exists when it runs. A later table group (P3/P4) must
   grant its new tables to `app_runtime` in its own migration.
5. **A superuser/owner connection bypasses RLS.** The local development `app`
   role is the table owner and a superuser, so existing records tests and the
   real-PostgreSQL isolation matrix keep running with no context. The prototype
   behaviour is only observable through the restricted runtime login.
6. **Lightweight session doubles need no context.** Request-flow tests that use
   an in-memory session stand-in have no DBAPI connection; the binding helper
   detects that and skips, so the production contract and the test doubles can
   coexist without weakening the real path.
7. **Health, authentication, public and platform routes need no tenant
   context.** A focused test drives each path through the restricted runtime
   role with no `X-Org-Id` and no bound context, and none of them touches a
   protected `records`/`record_revisions` row.
8. **`alembic check` stays green.** Roles, policies, functions and the role
   comment are not part of `Base.metadata`, so the prototype migration
   introduces no autogenerate drift.

## Residual risks and follow-ups

- Error non-disclosure for RLS-hidden rows is P3; the P2 app test relies on the
  existing service `404` predicate as the first layer.
- Workers, the coordinator, platform access and identity/indirect tables are
  explicitly out of the P2 prototype (P3/P4).
- Production enforcement of role separation includes the startup/deployment
  check that the runtime role is non-owner and lacks `BYPASSRLS` (P4 evidence
  per ADR-0022); P2 establishes the credential separation itself and the
  in-app path.
