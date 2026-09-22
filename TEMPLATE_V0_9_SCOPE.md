# Template v0.9 — PostgreSQL Row-Level Security Tenant-Isolation Backstop — Scope & Progress Log

## Relationship to other documents

- `Internal_Custom_Application_Starter_Architecture_v2.md` remains the long-term design standard. v0.9 implements its tenant-isolation, database, security and operations rules (BP §§8–13, §§28–31, §§37–39) without replacing the storage, jobs, tenancy, security or adapter rules.
- `TEMPLATE_V0_8_SCOPE.md` is the completed foundation: identity, tenancy, platform administration, storage, durable jobs, notifications and AI already exist and enforce the organisation boundary in the application layer.
- `plans/2026-09-18-postgresql-row-level-security-plan.md` is the active execution contract that proposed this capability. It records the P1 design, the P2 prototype, the adoption gate and the P3/P4 rollout. This file is the versioned release contract authored at release time, exactly as the plan's "Review and delivery" and ADR-0022's release-bookkeeping note require; it does not replace the plan while the plan remains `Status: Active`.
- Design and evidence documents: `docs/decisions/0022-postgresql-row-level-security.md` (ADR-0022), `docs/rls-table-inventory.md`, `docs/rls-prototype-findings.md`, `docs/rls-rollout.md`, `docs/operations.md`, `docs/backup-and-recovery.md`, `SECURITY.md` and `ARCHITECTURE.md`.
- `IMPLEMENTATION_GUIDE.md` defines the original v0.1–v0.6 foundation. Like v0.7 and v0.8, v0.9 is a supplementary post-foundation release.

---

# 1. Goal of v0.9

Add PostgreSQL Row-Level Security (RLS) as a **defence-in-depth backstop** for
organisation isolation. Application-level scoping, permissions and
foreign-resource `404` behaviour remain the first enforcement layer. RLS makes
the database default-deny so that a missed or wrong `organisation_id` predicate
in a service, refactor, raw/bulk statement or relationship load returns no rows
instead of foreign rows. It supplements, and never replaces, the existing
application controls.

---

# 2. In Scope

## 2.1 Fixed release decisions

The open decisions in the source plan are resolved for this release:

1. **Defence in depth, not replacement.** Application predicates, permission
   checks and `404` semantics stay in place and are never relaxed when a policy
   is enabled. The target invariant is that an ordinary application connection
   cannot read or change an organisation-owned row without trusted
   transaction-local context authorising that row.
2. **Separate database roles.** `app_owner` owns the schema and runs
   Alembic/DDL only; `app_runtime` is the ordinary API/worker path (non-owner,
   non-superuser, `NOBYPASSRLS`, `NOINHERIT`); `app_coordinator` is a second
   non-bypass role scoped to dispatch state rather than a tenant; `app_metrics`
   is a `NOLOGIN` `SECURITY DEFINER` aggregate role; and `app_operator` is the
   isolated, audited operational credential (the only application role allowed
   to carry `BYPASSRLS`).
3. **Transaction-local context.** Context is set with a parameterised
   `set_config('app.organisation_id', $1, true)` after the active membership or
   durable row has been validated. It is transaction-local, clears on commit and
   rollback, and cannot survive pool reuse. Tenant context never comes from an
   `X-Org-Id` header alone or a broker message.
4. **No universal bypass.** There is no implicit or request-selectable bypass.
   Cross-tenant platform access goes through explicit policies keyed to a
   validated transaction-local platform context; the only general cross-tenant
   read is the isolated `app_operator` credential, loaded only by audited
   CLI/ops tooling and never by an HTTP process or worker.
5. **Indirect tables use a denormalised key or the parent strategy.**
   `job_attempts` carries a denormalised non-null `organisation_id` tied to its
   parent by a composite `(job_id, organisation_id)` foreign key;
   `notification_deliveries` and `membership_roles` use tested parent-existence
   policies, with `membership_roles` splitting read visibility from
   tenant-checked write authority.
6. **Nullable tenant never means all rows.** A `NULL organisation_id` on
   `audit_events`/`outbox_events` is never read as "all rows"; global and
   cross-tenant history is reachable only under the validated platform context
   or the operator credential.
7. **User-private rows need a user predicate.** `notifications` (and any future
   recipient-scoped table) require both organisation context and a
   transaction-local user id.

## 2.2 Database roles, grants and deployment checks

- Production uses distinct credentials: `DATABASE_URL` (`app_owner`),
  `DATABASE_RUNTIME_URL` (`app_runtime`), `DATABASE_COORDINATOR_URL`
  (`app_coordinator`) and `DATABASE_OPERATOR_URL` (`app_operator`). Production
  refuses to start without the runtime credential, so the owner credential can
  never silently become the ordinary application path.
- `app.db.role_checks` proves from the server catalogue that each runtime
  credential owns no table in `public`, carries none of
  `SUPERUSER`/`BYPASSRLS`/`CREATEDB`/`CREATEROLE`, and inherits no privileged
  role. It runs at the start of every normal runtime process — the API
  (`create_app` lifespan), the Dramatiq worker (`configure_worker`) and the
  outbox coordinator (`_async_main`) — and aborts a misconfigured process before
  it serves work. It is also exposed as `make verify-db-roles`.
- The check asserts that `app_operator` is the only application role carrying
  `BYPASSRLS`, owns no table and has no membership in either direction, and that
  `app_runtime` cannot `SET ROLE` it. Role migration adoption is safe to re-run:
  a pre-existing role is normalised, refused if it owns a table, and has
  memberships revoked in both directions.

## 2.3 Policies, context propagation and query plans

- Every enabled table has RLS `ENABLE`d and `FORCE`d with default-deny policies
  whose `USING` and `WITH CHECK` match, so reads, inserts, updates and tenant-key
  moves are all constrained. Absent, empty or malformed context returns no rows
  and fails closed for writes.
- Context is bound only after the membership/permission path (or, for workers,
  the durable row) validates it, using the parameterised helper in
  `app/db/rls.py`, in the same transaction as the protected query.
- Each table group is enabled by its own bounded, additive, reversible Alembic
  migration that also grants the group's new tables/sequences to `app_runtime`.
  Representative `EXPLAIN` plans and latency were reviewed per group; no
  sequential-scan regression was accepted, and the group's cross-organisation
  real-PostgreSQL suite is green before enforcement.
- Rollback has two scopes: a policy-layer rollback (`NO FORCE`/`DISABLE`,
  retaining additive columns) for the mixed-version window and the emergency
  stop, and the migration downgrade (destructive schema change under the
  destructive-migration gate) for a full reversal.

## 2.4 Worker, coordinator, platform and identity paths

- Dramatiq workers share `app_runtime`. A worker binds `app.job_id` for a
  single-row `FOR SELECT` bootstrap read of its durable `jobs` row, clears it,
  then binds the row's own `organisation_id` before any protected read or write.
  The tenant context therefore comes from the durable row, never the broker.
- The outbox coordinator, the reliability-metrics refresh and the
  `reconcile_jobs` CLI use `app_coordinator`, whose policies are scoped to
  dispatch lifecycle state and whose UPDATE authority is granted **per column**.
- Identity and control-plane tables resolve pre-tenant rows through narrow,
  user-keyed or provider-keyed policies (for example the SELECT-only
  `organisation_memberships_user_isolation` and the invitee email-keyed
  `invitations` select/update pair). The verified webhook bootstrap is a
  single-row, non-insert policy, never a bypass.
- The platform plane binds a validated transaction-local platform context after
  the permission dependency authorises the caller; a separate narrow
  `app.platform_service` context covers the one-time bootstrap, the
  signature-verified `user.deleted` webhook and operator recovery. Platform
  status alone grants no tenant-row access.
- Three formerly global cross-tenant AI sweeps iterate the unprotected
  `organisations` table and bind each tenant before touching its protected rows;
  the provider-file reconciliation sweep allocates its global budget fairly
  across organisations.

## 2.5 Operations, backup and recovery

- `docs/operations.md` documents role separation, the per-environment
  confirmation queries, the automated startup gate and the
  `app_operator` operational path; `docs/backup-and-recovery.md` takes
  `pg_dump`/`pg_restore` through `app_operator`, because a non-`BYPASSRLS` dump
  would silently omit every row an enabled policy hides.
- Every privileged operational path is audited with who/when/where and the
  operation performed, never row contents, secrets, tokens or provider
  responses.

---

# 3. Out of Scope (Explicitly Deferred)

| Capability | Deferred to / decision |
| --- | --- |
| Replacing application scoping, permissions or `404` semantics with RLS | Prohibited; the application layer stays first (ADR-0022 decision 1) |
| Intra-organisation teams or sub-organisation data partitions | The organisation remains the tenant boundary |
| Custom per-organisation role definitions | The global role catalogue is unchanged |
| A production-wide universal or request-selectable runtime bypass | Prohibited |
| Platform impersonation or unbounded support read access | Only the explicitly reviewed operational path exists |
| RLS for provider-specific storage or AI behaviour | RLS changes database access control, not adapters |
| Per-tenant database/shard isolation | A later capacity decision, not this release |

---

# 4. Commands That Must Work

All v0.8 commands remain part of the gate:

```bash
make dev
make migrate
make lint
make typecheck
make test
make format
make generate-client
make validate-ai-registries
make validate-execution-contracts
make test-ai-contracts
make e2e
make verify-db-roles
make check
```

The RLS-specific real-PostgreSQL and structural checks are:

```bash
cd backend && uv run pytest \
  tests/test_org_isolation_matrix_db.py \
  tests/test_tenant_isolation_registry.py \
  tests/test_security_suite.py \
  tests/test_rls_records_db.py \
  tests/test_rls_records_enablement_db.py \
  tests/test_rls_files_enablement_db.py \
  tests/test_rls_notifications_enablement_db.py \
  tests/test_rls_ai_data_enablement_db.py \
  tests/test_rls_organisation_settings_enablement_db.py \
  tests/test_rls_jobs_enablement_db.py \
  tests/test_rls_identity_enablement_db.py \
  tests/test_rls_operational_ledgers_enablement_db.py \
  tests/test_rls_platform_plane_enablement_db.py \
  tests/test_rls_indirect_rows_db.py \
  tests/test_rls_operator_credential_boundary.py
```

`make check` (lint, typecheck, tests, `validate-ai-registries`,
`validate-execution-contracts`, generated-client drift) is the complete local
gate. The migration upgrade/downgrade/re-upgrade cycle and `alembic check` are
part of it for every group.

---

# 5. Acceptance Criteria

1. **Fail-closed context:** missing, empty or malformed tenant context returns
   no tenant rows and fails closed for writes.
2. **Cross-organisation denial:** a normal runtime connection cannot read or
   mutate another organisation's protected rows, including through an unscoped
   query.
3. **Write safety:** inserts and updates cannot assign a protected row to an
   unauthorised organisation, and a tenant-key move is rejected.
4. **No context leakage:** transaction-local context cannot survive commit,
   rollback, exception, cancellation, timeout or pooled-connection reuse.
5. **Explicit control-plane paths:** workers, platform operations and
   authentication flows use explicit, tested access paths rather than a
   universal runtime bypass; platform status alone grants no tenant-row access.
6. **Application layer unchanged:** application-level scoping, permissions and
   `404` behaviour remain in place as the first enforcement layer.
7. **Complete classification:** every table has a recorded classification and
   either a tested policy or an explicit reviewed exclusion.
8. **Role least privilege:** runtime roles cannot disable policies, alter
   protected schema or bypass RLS, and no ordinary API/worker path uses owner,
   superuser or `BYPASSRLS` credentials.
9. **Tested operations:** deployment, migration, rollback and recovery
   procedures are tested and documented, including the restricted-role lease,
   retry, reconciliation and outbox-dispatch proof.
10. **Human review:** all tenant-isolation, migration, database-role, identity,
    control-plane and infrastructure changes received human review before
    apply-and-commit.

## 5.1 Capability traceability

| Source requirement | Acceptance | Owning checkpoint | API/frontend surface | Required test evidence |
| --- | --- | --- | --- | --- |
| RLS design, table classification and access-path register | §5.1, §5.7 | Scope §6.1 | None; documentation only | `tenant_isolation_registry.py`, `test_tenant_isolation_registry.py`, `docs/rls-table-inventory.md` |
| `records` prototype, adoption gate and plan evidence | §5.1–§5.4, §5.8 | Scope §6.2 | None; internal prototype | `test_rls_records_db.py`, `docs/rls-prototype-findings.md` |
| Direct tenant-data rollout (`records`, files, notifications, AI data, organisation settings, jobs) | §5.1–§5.8 | Scope §6.3 | No new route; unchanged org-scoped APIs and `404` contract | `test_rls_*_enablement_db.py`, `test_org_isolation_matrix_db.py`, `test_security_suite.py` |
| Identity, control-plane, operational ledgers and platform plane | §5.2, §5.5, §5.7 | Scope §6.4 | No new route; unchanged platform and webhook contracts | `test_rls_identity_enablement_db.py`, `test_rls_operational_ledgers_enablement_db.py`, `test_rls_platform_plane_enablement_db.py`, `test_rls_indirect_rows_db.py` |
| Roles, deployment checks, docs and release verification | §5.8, §5.9 | Scope §6.5 | `make verify-db-roles`; docs only | `test_rls_operator_credential_boundary.py`, role-check tests, `make verify-db-roles`, `docs/operations.md`, `docs/backup-and-recovery.md` |

---

# 6. Progress Log

Check items off only after the implement → review → apply-and-commit loop. Each
subsection is one checkpoint and ran on its own `feature/*` branch. A human
review gate named in a subsection is recorded before apply-and-commit. The
per-group migration IDs, tests and recorded human reviews are the plan's P1–P4
evidence; this log restates them as the release contract.

## 6.1 ADR, inventory and access-path design

Dependencies: completed v0.8 release.

- [x] Create a checked-in table inventory classifying every table as global,
      organisation-owned, user-private, indirectly organisation-owned,
      platform-only or operational, and record the tenant key, parent ownership
      path, readers/writers and application access paths for each non-global
      table (`docs/rls-table-inventory.md`)
- [x] Identify raw SQL, bulk mutations, relationship loads, row locks and every
      connection/access path (`docs/rls-table-inventory.md` §3–§4)
- [x] Record ADR-0022: enforcement model, separate owner/runtime/coordinator/
      operator roles, worker bootstrap, no universal bypass, user-private
      context, indirect-table strategy, nullable-tenant treatment, pre-tenant
      lookup, context propagation, threat model and prototype success criteria
- [x] Machine-check the classification in `backend/tests/tenant_isolation_registry.py`
      so a new model with no declared isolation strategy fails
      `test_tenant_isolation_registry.py`

Human review required before application: tenant isolation and database-role
design. Recorded approval precedes the prototype (plan P1 completion evidence).

## 6.2 `records` prototype and adoption gate

Dependencies: Scope §6.1.

- [x] Add a dedicated non-owner, non-superuser, non-`BYPASSRLS` prototype runtime
      role and ensure the migration/schema-owner credential is not the normal
      application path (migration `c1d2e3f4a5b6`)
- [x] Add reversible migrations enabling and forcing RLS on `records` and
      `record_revisions` with default-deny `USING`/`WITH CHECK` policies driven
      by transaction-local organisation context
- [x] Bind context with a parameterised transaction-local operation after the
      active membership is validated; prove it clears on commit, rollback,
      exception, cancellation, timeout and pool reuse
- [x] Prove organisation A cannot select/insert/update/delete organisation B
      rows, an unscoped query returns only A rows, missing context fails closed,
      mismatched writes are rejected, and runtime credentials cannot disable
      policies or assume the owner role (`test_rls_records_db.py`)
- [x] Measure representative plans and latency and record the findings
      (`docs/rls-prototype-findings.md`)
- [x] Record the adoption gate: RLS **adopted** (2026-09-18) after the prototype
      met every success criterion; approve the rollout order and rollback
      procedure (`docs/rls-rollout.md`) and confirm environments can provide
      separate migration and runtime roles

Human review required before application: tenant isolation, database roles and
migrations. Recorded at the adoption gate (ADR-0022 status, 2026-09-18).

## 6.3 Direct tenant-data rollout

Dependencies: Scope §6.2 and the adopted decision.

- [x] Group 0 (`records`, `record_revisions`): production enablement migration
      `d2e3f4a5b6c7`, canonical organisation-isolation policies, reversible
      downgrade, `test_rls_records_enablement_db.py` (merged in PR #96)
- [x] Group 1 (`files`): migration `e3f4a5b6c7d8`, including the queued
      AI/document-authority worker read closure, reversible downgrade and
      `test_rls_files_enablement_db.py` (human review recorded 2026-09-19)
- [x] Group 2 (`notifications`, `notification_deliveries`): migration
      `f5a6b7c8d9e0`, the user-private notification policy, the parent-existence
      delivery policy, the API/email-worker user-context propagation and the
      non-bypass `app_metrics` aggregate operational read
      (`app_attention_required_delivery_count()`); `test_rls_notifications_enablement_db.py`
      (human review recorded 2026-09-19)
- [x] Group 3 (AI data: `ai_requests`, `ai_outputs`, `ai_attachment_references`,
      `ai_scratch_uploads`): migration `b8c9d0e1f2a3`, worker context from the
      durable `jobs` row, and the three formerly global sweeps reworked to
      iterate `organisations` and bind each tenant with a fair per-organisation
      batch budget; `test_rls_ai_data_enablement_db.py` (human review recorded
      2026-09-19)
- [x] Group 4a (`organisation_features`, `organisation_ai_settings`): migration
      `c9d0e1f2a3b4`, the reviewed per-organisation platform binding and the
      organisation-creation bindings;
      `test_rls_organisation_settings_enablement_db.py` (human review recorded
      2026-09-19)
- [x] Group 4b (`jobs`, `job_attempts`): migration `d0e1f2a3b4c5`, the
      `app_coordinator` non-bypass prerequisite with dispatch-state policies and
      column-level UPDATE grants, the `FOR SELECT` worker bootstrap, the
      denormalised `job_attempts.organisation_id` with its composite parent FK,
      and the notification-exhaustion addendum; `test_rls_jobs_enablement_db.py`
      (human review recorded 2026-09-19)
- [x] Add read/write policies and cross-organisation tests for every group
      before enforcement; require organisation **and** user context for
      user-private rows; keep signed downloads and AI attachment resolution
      scoped by the protected row; check representative plans and indexes after
      each group
- [x] Prove retries, leases, reconciliation and outbox dispatch work without a
      bypass role on the restricted `app_runtime`/`app_coordinator` logins
      (`test_rls_jobs_enablement_db.py`)
- [x] Prove application errors do not disclose whether RLS hid a foreign row;
      stop the rollout if any table needs an unexplained bypass

Human review required before application: tenant isolation, migrations and
worker context, per group. Every group's review is recorded in the plan's P3
progress notes.

## 6.4 Identity, control-plane and indirect tables

Dependencies: Scope §6.3.

- [x] Group 5 (`organisation_memberships`, `membership_roles`, `invitations`):
      migration `f1a2b3c4d5e6` with the SELECT-only pre-tenant user-keyed
      policy, the parent read / tenant-checked write split for `membership_roles`,
      the invitee email-keyed select/update pair and the verified-webhook
      single-row bootstrap; `test_rls_identity_enablement_db.py` (human review
      recorded 2026-09-19)
- [x] Group 6 (operational ledgers: `audit_events`, `outbox_events`,
      `maintenance_runs`, `webhook_events`): migration `a2b3c4d5e6f7`, null-safe
      policies, the tenant-checked audit append, the validated platform context
      and the isolated `app_operator` credential;
      `test_rls_operational_ledgers_enablement_db.py` (human review recorded
      2026-09-20)
- [x] Group 7 (platform-only plane: `platform_roles`,
      `platform_role_permissions`, `platform_memberships`, `bootstrap_states`):
      migration `b4c5d6e7f8a9`, the pre-authorisation user-keyed self read, the
      validated platform context and the separate narrow `app.platform_service`
      context; `test_rls_platform_plane_enablement_db.py` (human review recorded
      2026-09-20)
- [x] Prove the indirect-row strategies together with real `app_runtime` tests
      for `job_attempts` (denormalised key), `notification_deliveries` (parent
      existence) and `membership_roles` (parent read split from tenant-checked
      write): `test_rls_indirect_rows_db.py`; record the final strategy in
      ADR-0022 decision 6 and `docs/rls-table-inventory.md` (human review
      recorded 2026-09-22)
- [x] Handle global versus tenant audit/outbox events without treating a null
      tenant key as unrestricted access
- [x] Give platform operations an explicit, narrowly scoped path and prove
      platform status alone cannot read ordinary tenant data
- [x] Document and test migration, support, backup, restore and emergency access
      roles; audit privileged operational use without placing secrets or row
      contents in the audit event
- [x] Add startup/deployment checks proving runtime roles do not own protected
      tables and lack `BYPASSRLS`, including the worker and coordinator gates
      (human review recorded 2026-09-22)

Human review required before application: identity, permission-model,
control-plane, worker-context and backup/recovery changes. Every group's review
is recorded in the plan's P4 progress notes.

## 6.5 Operations, documentation and release verification

Dependencies: Scope §6.1–§6.4.

- [x] Update `ARCHITECTURE.md` and `SECURITY.md` with the exact RLS guarantees,
      exclusions, role model and privileged-access procedure; `docs/operations.md`
      and `docs/backup-and-recovery.md` already carry the role/operator runbook
      and the operator-DSN backup path
- [x] Record every required human review (tenant isolation, database roles,
      migrations, identity/permission, control-plane platform/service contexts,
      worker/coordinator gates and the operational credential)
- [ ] Run the complete two-organisation/different-role contract matrix against
      real PostgreSQL with the production-like runtime role, plus the
      connection-pool stress tests with interleaved organisations
- [ ] Run migration, lint, type, backend/frontend, generated-client and the
      mandatory security gates on the supported toolchain (`make check`)
- [ ] Test a mixed-version deployment and rollback, and backup/restore into
      isolated infrastructure, using the intended role and policy definitions
- [ ] Verify the table inventory records a final policy or reviewed exclusion for
      every table and that no normal API/worker path uses owner, superuser or
      `BYPASSRLS` credentials
- [ ] Require final human review of the policies, grants, role ownership and
      deployment configuration, then mark this scope complete and cut the
      immutable `v0.9.0` tag

Human review required before application: tenant isolation, permission-model,
migration, database-role, control-plane and backup/recovery changes; final
release approval before tagging.

---

# 7. Blueprint Reference Map

Line ranges were verified against the current blueprint headings. The plan
`plans/2026-09-18-postgresql-row-level-security-plan.md` carries the full
reference map for the P1–P4 work; this table maps the release checkpoints to the
same governing sources.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Design/inventory | **BP §8–§10** (authentication, organisations/permissions, database conventions), **BP §11** (transactions), **BP §28–§31** (observability, audit, security baseline, testing) | Table classification, tenant keys and ownership paths, readers/writers and access paths, raw SQL and bulk/relationship access, connection types, role and control-plane design, threat model and prototype criteria |
| **Scope §6.2** Prototype/adoption | **BP §9–§11**, **BP §30–§31** | Separate runtime/owner roles, transaction-local context propagation, default-deny `USING`/`WITH CHECK` policies, pool-reuse safety, real-PostgreSQL tests, migration reversibility and performance measurement |
| **Scope §6.3** Direct tenant-data rollout | **BP §9**, **BP §11**, **BP §17–§20**, **BP §28–§31** | Bounded table-group rollout, user-private policies, worker context from durable rows, outbox/retry/reconciliation without bypass, index and plan review, error non-disclosure |
| **Scope §6.4** Identity/control-plane/indirect | **BP §8–§10**, **BP §28–§31**, **BP §39** | Pre-tenant membership lookup, identity/control-plane classification, indirect-table tenant keys, nullable-tenant event treatment, explicit platform/operational paths, role-ownership and `BYPASSRLS` deployment checks, backup/recovery |
| **Scope §6.5** Operations/governance/release | **BP §28**, **BP §30–§34**, **BP §37–§39**, **BP §41–§42** | Never-log/security controls, reviews/ADRs, CI, environment separation, backup and recovery, template lifecycle and validation |

---

# 8. Status

```text
Release:    v0.9.0 (PostgreSQL row-level security tenant-isolation backstop)
State:      planned
Started:    2026-09-18
Completed:  (pending)
```

The P1–P4 implementation and all per-group human reviews are delivered and
recorded in `plans/2026-09-18-postgresql-row-level-security-plan.md` and
`docs/rls-rollout.md`. This scope is **planned**: the release-time gates in
Scope §6.5 — the complete real-PostgreSQL contract matrix and pool stress, the
full `make check` gate, the mixed-version/rollback and backup/restore exercises,
and the final release review — are still open, and the plan remains the active
execution contract until they pass.

When every Scope §6 box above is checked after review, this file flips to
`State: complete`, the version is recorded in `backend/pyproject.toml`,
`frontend/package.json` and `[tool.project-template].version`, an upgrade guide
is added under `docs/upgrades/`, and the immutable `v0.9.0` tag is cut, per
blueprint §41 and `CONTRIBUTING.md`.
