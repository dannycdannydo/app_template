# PostgreSQL Row-Level Security Evaluation and Rollout Plan

Status: Active

Relates to: `Internal_Custom_Application_Starter_Architecture_v2.md` BP
§§8–13, §§28–31 and §§37–39; `SECURITY.md`; `docs/operations.md`;
`docs/backup-and-recovery.md`; and `AGENTS.md`. Design evidence:
`docs/rls-table-inventory.md` and
`docs/decisions/0022-postgresql-row-level-security.md`.

## Goal

Determine whether PostgreSQL Row-Level Security (RLS) is a practical final
backstop for organisation isolation, prove the design on the representative
`records` module, and—only if the evidence supports adoption—roll it out in
reviewed table groups.

RLS will supplement, not replace:

- validated organisation membership and permissions;
- explicit organisation predicates in application queries;
- foreign-resource `404` behaviour; and
- cross-organisation contract tests.

The target invariant is that an ordinary application connection cannot read or
change an organisation-owned row without trusted transaction-local context
authorising that row.

## Agreed scope

The current schema contains directly organisation-scoped data including:

- memberships and invitations;
- records and record revisions;
- files, notifications and jobs;
- organisation feature and AI settings;
- AI requests, outputs, attachment references and scratch uploads; and
- audit and outbox rows, whose organisation ID may be nullable.

It also contains indirectly owned rows, including job attempts, notification
deliveries and membership-role assignments. These cannot safely receive a
generic `organisation_id = current_context` policy without first deciding
whether to add a denormalised tenant key or enforce ownership through a parent
relationship.

Identity lookup, platform administration, migrations, maintenance and recovery
are distinct control-plane paths. They must be designed explicitly rather than
handled by a universal application bypass.

The work covers the design of roles, transaction-local context, policies and
control-plane paths (P1), a bounded prototype on `records`/`record_revisions`
(P2), an explicit adoption gate, and—only if adopted—staged rollout in reviewed
table groups (P3/P4) followed by release verification.

## Out of scope

- Replacing application-level scoping, permissions or `404` semantics with RLS.
  The application layer remains the first enforcement layer.
- A production-wide policy rollout as part of approving the prototype.
- Intra-organisation teams or sub-organisation data partitions; the
  organisation remains the tenant boundary.
- Custom per-organisation role definitions; the global role catalogue is
  unchanged.
- Provider-specific storage or AI behaviour; RLS changes database access
  control, not adapters.
- An implicit or request-selectable universal database bypass for the ordinary
  runtime role.
- Platform impersonation or support read access beyond the explicitly reviewed
  operational path.

## Decisions and assumptions

- [x] An organisation is the hard tenant/subscriber boundary.
- [x] Roles are organisation-wide user types, not data partitions.
- [x] A user may have separate memberships and roles in multiple
      organisations.
- [x] Tenant context must come from a validated active membership, never
      directly from an `X-Org-Id` header or broker message.
- [x] Application queries remain explicitly scoped even after RLS is enabled.
- [x] Platform status does not silently grant tenant-row access.
- [x] The normal runtime role must not own protected tables and must not have
      `BYPASSRLS`.
- [x] A production-wide rollout is conditional on the prototype and a recorded
      adoption decision.

## Commands that must work

Focused real-PostgreSQL and structural checks for this plan:

```bash
cd backend && uv run pytest \
  tests/test_org_isolation_matrix_db.py \
  tests/test_tenant_isolation_registry.py \
  tests/test_security_suite.py \
  tests/test_records_db.py
```

The complete local gate:

```bash
make check
```

P2 adds its own real-PostgreSQL policy, context-propagation and pool-reuse
suite; P3 and P4 add a policy test for every table group they enable. Those
suites are created by the checkpoint that enables them.

## Acceptance criteria

1. [ ] Missing or malformed tenant context fails closed.
2. [ ] A normal runtime connection cannot read or mutate another
       organisation's protected rows, including through an unscoped query.
3. [ ] Inserts and updates cannot assign a protected row to an unauthorised
       organisation.
4. [ ] Transaction-local context cannot leak through the connection pool.
5. [ ] Workers, platform operations and authentication flows use explicit,
       tested access paths rather than a universal runtime bypass.
6. [ ] Application-level scoping, permissions and `404` behaviour remain in
       place as the first enforcement layer.
7. [ ] Every table has a recorded classification and either a tested policy or
       an explicit reviewed exclusion.
8. [ ] Runtime roles cannot disable policies, alter protected schema or bypass
       RLS.
9. [ ] Deployment, migration, rollback and recovery procedures are tested and
       documented.
10. [ ] All tenant-isolation, migration and infrastructure changes receive
        human review before apply-and-commit.

## Implementation checkpoints

### P1 — ADR, inventory and access-path design

Dependencies: none.

Human review required before application: tenant isolation, database-role and
operational-design changes.

Inventory:

- [x] Create a checked-in table inventory classifying every table as global,
      organisation-owned, user-private, indirectly organisation-owned,
      platform-only or operational.
- [x] Record the tenant key, parent ownership path, legitimate readers/writers
      and current application access paths for each non-global table.
- [x] Identify raw SQL, bulk updates/deletes and relationship loads that touch
      classified tables.
- [x] Identify API, Dramatiq, scheduler, webhook, platform, migration, support,
      backup and recovery connections.

ADR decisions:

- [x] Compare continued application-only enforcement with RLS defence in depth.
- [x] Define separate database roles for schema ownership/migrations and normal
      application runtime.
- [x] Decide whether workers share the restricted runtime role or use a second
      non-bypass role with equally explicit context.
- [x] Define the narrow platform/maintenance access mechanism. Reject an
      implicit or request-selectable universal bypass.
- [x] Decide whether user-private policies also require a transaction-local
      user ID.
- [x] Decide how indirectly owned tables are protected: copied tenant key,
      parent-existence policy, restricted parent-only access, or documented
      exclusion.
- [x] Define treatment for nullable-tenant audit/outbox rows and global events.
- [x] Define how authentication and membership lookup occur before trusted
      tenant context exists.
- [x] Record the threat model, operational cost, residual risks and objective
      prototype success criteria.

P1 completion evidence:

- [x] Every current table and connection type appears in the inventory.
- [x] The ADR contains no unresolved universal-bypass or pre-authentication
      context assumption.
- [x] Human review approves proceeding to the prototype.

### P2 — `records` prototype

Dependencies: P1.

Human review required before application: tenant isolation, database roles and
migrations.

Expected engineering effort: approximately 2–4 days, excluding review.

Database roles and policy:

- [x] Add a dedicated non-owner, non-superuser, non-`BYPASSRLS` prototype
      runtime role.
- [x] Ensure migration/schema-owner credentials are not used by the normal
      application path.
- [x] Add reversible migrations enabling and forcing RLS on `records` and
      `record_revisions`.
- [x] Use default-deny policies based on transaction-local organisation
      context.
- [x] Make absent, empty or malformed context return no tenant rows or fail
      safely; it must never mean unrestricted access.
- [x] Apply equivalent `USING` and `WITH CHECK` constraints so inserts and
      updates cannot create or move rows into another organisation.

Context propagation:

- [x] Set context only after the active membership and permission path has
      validated the selected organisation.
- [x] Set context with a parameterised transaction-local operation; do not
      interpolate request values into SQL.
- [x] Ensure the context and protected query execute in the same transaction.
- [x] Clear context automatically on commit and rollback.
- [x] Keep platform, health, authentication and public routes functional
      without fabricating tenant context.
- [x] Keep existing application-level `organisation_id` predicates and
      foreign-resource `404` behaviour.

Prototype tests:

- [x] Prove organisation A can access its rows and cannot select, insert,
      update or delete organisation B rows.
- [x] Prove an unscoped query still returns only the authorised organisation's
      rows.
- [x] Prove missing context cannot access either organisation.
- [x] Prove `WITH CHECK` rejects a mismatched insert and tenant-key update.
- [x] Prove context cannot survive commit, rollback, exception, cancellation,
      timeout or pooled-connection reuse.
- [x] Prove a user who is owner in A and viewer in B receives the correct
      context and application permissions in each request.
- [x] Prove ordinary runtime credentials cannot disable policies, alter the
      schema or assume the owner role.
- [x] Measure query plans and latency for representative list/detail/write
      operations.

P2 completion evidence:

- [x] Real-PostgreSQL tests demonstrate default denial and safe pool reuse.
- [x] Migration upgrade, downgrade and re-upgrade pass.
- [x] Existing records API behaviour and security tests remain green.
- [x] The prototype records its performance and operational findings.

Adoption gate (one reviewed decision after P2):

If RLS is deferred or rejected:

- [ ] Record the specific complexity or risk that prevents adoption.
- [ ] Record the residual risk of a missed application query predicate.
- [ ] Remove or disable prototype-only production configuration cleanly.
- [ ] Retain the real-database cross-organisation test suite as a release gate.
- [ ] Close this plan without starting P3 or P4.

If RLS is adopted:

- [x] Approve a table-group rollout order and rollback procedure.
- [x] Confirm deployment environments can provide separate migration and
      runtime roles.
- [x] Proceed to P3 and P4 as separately reviewed work units.

### P3 — Direct tenant-data rollout

Dependencies: P2 and an adopted decision at the gate.

Human review required before application: tenant isolation, migrations and
worker context.

Expected engineering effort after a successful prototype: approximately 5–8
days for core user-facing data, excluding review.

Progress: **groups 0, 1, 2 and 3 are delivered.** Group 0 (`records`,
`record_revisions`): production enablement migration `d2e3f4a5b6c7`, merged in
PR #96. Group 1 (`files`): production enablement migration `e3f4a5b6c7d8`,
including the queued AI/document-authority worker read closure, with the
required human review of the tenant-isolation, migration/database-role and
worker-context changes recorded (2026-09-19). Group 2 (`notifications`,
`notification_deliveries`): production enablement migration `f5a6b7c8d9e0`,
installing the user-private policy (organisation + transaction-local user) and
the parent-existence delivery policy, with the API dependency and email-worker
user-context propagation and a reversible downgrade. Group 2 also installs the
non-bypass aggregate operational read the enforced user-private policy requires
(the `app_metrics` role and `app_attention_required_delivery_count()`), so the
`attention_required_email_deliveries` metric stays truthful without a bypass.
The required human review of the group-2 tenant-isolation,
migration/database-role and worker-context changes, including that operational
read, was recorded 2026-09-19. Group 3 (AI data: `ai_requests`, `ai_outputs`,
`ai_attachment_references`, `ai_scratch_uploads`): production enablement
migration `b8c9d0e1f2a3`, installing the canonical organisation-isolation
policies with a reversible downgrade. The group also binds the organisation
context for the durable `ai.execute` worker and, because the plan forbids a
universal bypass, reworks the three formerly global cross-tenant AI sweeps
(retention/stale reservation, scratch expiry and provider-file reconciliation)
to iterate the global, unprotected `organisations` table and bind each tenant
before touching its protected AI rows. The required human review of the group-3
tenant-isolation, migration/database-role and worker-context changes was
recorded (2026-09-19), and the review's mandatory fixes were applied: the
provider-file reconciliation sweep now allocates its global batch budget fairly
across organisations, so a single high-volume tenant can no longer consume the
whole run and starve every later tenant, and the group-3 real-PostgreSQL suite
now exercises own-tenant and cross-tenant insert/update/delete and
tenant-key-move on every enabled table. Group 4 (jobs and organisation settings)
was split during implementation review into 4a and 4b (`docs/rls-rollout.md`
§3.1). Group 4a (`organisation_features`, `organisation_ai_settings`) is
delivered by production enablement migration `c9d0e1f2a3b4`: the two tables are
organisation-owned but platform-managed, so the platform-plane services bind
exactly the organisation they target (an explicit per-organisation platform
path, never a bypass) and the organisation-creation paths bind the new
organisation before writing its default settings row. The required human review
of the group-4a tenant-isolation, migration/database-role and per-organisation
platform-binding changes was recorded (2026-09-19), and the review's mandatory
fixes were applied: the suite now proves representative, realistically sized
`EXPLAIN` plans for both settings lookups, exercises the missing-row
create/update and every organisation-creation path under the restricted role,
and the ADR-0022 decision 4/9 and RLS comments record the approved narrow
platform-binding exception. Group 4b (`jobs`, `job_attempts`) is delivered by
production enablement migration `d0e1f2a3b4c5`, which brings the `app_coordinator`
non-bypass prerequisite forward with it: the outbox coordinator, the in-process
reliability-metrics refresh and the `reconcile_jobs` CLI now connect as
`app_coordinator` with dispatch-state-scoped policies, workers bind `app.job_id`
for the single-row bootstrap and then the durable row's organisation, and
`job_attempts` takes the denormalised non-null `organisation_id` (ADR-0022
decision 6). The required human review of the group-4b tenant-isolation,
database-role/grant/policy, worker-context, notification-exhaustion and
destructive-downgrade changes was recorded (2026-09-19), and the review's
mandatory fixes were applied: the worker bootstrap is now `FOR SELECT` only
(the row lock runs under tenant authority after `app.job_id` is cleared), the
coordinator's `jobs`/`job_attempts` UPDATE authority is granted per
settlement/reconciliation column with state-scoped `WITH CHECK`, and
`job_attempts.organisation_id` is tied to its parent job by a composite
`(job_id, organisation_id)` foreign key.

**Aggregate P3 evidence closed 2026-09-22.** All P3 table groups (0–4b) have
landed, so the aggregate checkboxes and completion evidence below are satisfied
and are ticked. The restricted-role proof — worker claim, expired-lease takeover
and transient-failure retry on `app_runtime`, plus queued/expired-running
reconciliation and the real `run_cycle` outbox publish cycle (claim, durable
job-aggregate read, registry publication and owner-checked settlement) on the
non-bypass `app_coordinator` — is `test_rls_jobs_enablement_db.py`; every group's
cross-organisation read/write, representative-plan and reversibility evidence is
recorded in `docs/rls-rollout.md` §3; and application-error non-disclosure is
proven by the group 0–3 suites together with the unchanged jobs/API cross-org
`404` contract. The completion-evidence phrase "API, worker and generated-
capability isolation tests remain green" is explicitly mapped to the generated
OpenAPI/client drift gate plus the API and worker isolation suites (all green in
`make check`), because no artifact named "generated-capability" exists in the
repo. **Human review recorded 2026-09-22:** the tenant-isolation evidence for the
restricted-role lease/retry/reconciliation/outbox-dispatch proof was reviewed and
approved before these boxes were ticked.

- [x] Roll out policies in bounded migrations, beginning with the `records`
      group (group 0: `records`, `record_revisions`) — a production
      enablement migration separate from the P2 prototype, with its own
      cross-organisation tests and reversible downgrade — then files,
      notifications and AI data, then jobs and organisation settings.
- [x] Add both read/write policies and cross-organisation tests for every table
      group before enabling enforcement.
- [x] Require user context as well as organisation context for user-private
      notification rows.
- [x] Ensure signed downloads and AI attachment resolution remain scoped by
      the protected database row.
- [x] Ensure workers derive organisation context from validated durable rows,
      not broker arguments alone.
- [x] Prove retries, leases, reconciliation and outbox dispatch work without a
      bypass role.
- [x] Verify application errors do not disclose whether RLS hid a foreign row.
- [x] Check representative query plans and indexes after each table group.
- [x] Stop the rollout if a table requires an unexplained bypass; return it to
      design review instead.

P3 completion evidence:

- [x] Every enabled table has real select/insert/update/delete policy tests.
- [x] API, worker and generated-capability isolation tests remain green.
- [x] Rollback is demonstrated for every deployed table group.

### P4 — Identity, control-plane and indirect tables

Dependencies: P3.

Human review required before application: identity, permission-model,
control-plane and backup/recovery changes.

Expected engineering effort: approximately 3–7 days, depending on the P1
classification and whether schema changes are required.

Progress: **group 5 (identity and control plane) is delivered.** Production
enablement migration `f1a2b3c4d5e6` installs the canonical organisation
isolation policy on `organisation_memberships` and `invitations`, the
SELECT-only pre-tenant `organisation_memberships_user_isolation` policy, the
`membership_roles_parent_isolation` parent-existence policy, the invitee
email-keyed `invitations_invitee_select`/`invitations_invitee_update` pair and
the verified-webhook `invitations_webhook_provider_isolation` single-row
bootstrap, with a reversible downgrade (`docs/rls-rollout.md` §3.2). The
authenticated user is bound as transaction-local `app.user_id` before the
pre-tenant identity lookups; every platform membership/invitation operation
binds exactly the organisation it targets; the cross-tenant teardown deletes
run under the user-keyed read plus a per-organisation delete, so the group
needs no `app_operator`; and runtime `UPDATE` on `invitations` is
column-restricted to `status`/`updated_at`. The required human review of the
group-5 tenant-isolation, permission/identity access and database
migration/control-plane changes was recorded (2026-09-19), and the review's
mandatory fixes were applied: the `membership_roles` read visibility is split
from write authority (a new `membership_roles_organisation_isolation` policy
requires the parent membership's durable organisation to equal the validated
tenant, so a pre-tenant user-only context cannot mutate a grant), the verified
webhook bootstrap is split into `invitations_webhook_provider_select`/
`invitations_webhook_provider_update` so binding a provider id can read/lock
and flip `status` but never insert or delete, and the lost-race invitation
retry rebinds `app.user_id` after its rollback before re-reading the invitee
rows. The real-PostgreSQL suite now proves those pre-tenant and provider
write-denial paths. Group 6 (operational ledgers: `audit_events`,
`outbox_events`, `maintenance_runs`, `webhook_events`, including the
`app_operator` credential) is delivered by production enablement migration
`a2b3c4d5e6f7`: null-safe read policies, a **tenant-checked** audit append
policy, the validated transaction-local platform context, the isolated
`app_operator` credential, and a reversible downgrade. The required human
review of the group-6 tenant-isolation, database-role/grant/policy, migration,
control-plane/platform-context and backup/recovery changes was recorded
(2026-09-20), and the review's mandatory fixes were applied: the coordinator's
`outbox_events` UPDATE authority is **column-level** (the lifecycle columns
only, with real-PostgreSQL denial tests for tenant-key, payload/contract,
aggregate and identity rewrites); the audit append `WITH CHECK` is
tenant-checked so a foreign-tenant attribution is denied for an ordinary tenant
context; a pre-existing `app_operator` is adopted only after revoking
memberships in **both** directions, and the downgrade always revokes the
migration's read grants (with adversarial upgrade/downgrade tests); and
`docs/backup-and-recovery.md` now uses an executable libpq-compatible operator
DSN derived from `DATABASE_OPERATOR_URL`. Group 7 (platform-only plane) is
implemented and human-reviewed by production enablement migration
`b4c5d6e7f8a9`: the global
platform catalogue (`platform_roles`, `platform_role_permissions`) takes a
runtime read policy and has its inherited table-wide DML grant revoked,
`platform_memberships` takes the **SELECT-only**
pre-authorisation user-keyed policy plus the cross-user platform-access policy,
and `bootstrap_states` takes context-gated read/insert/delete policies (no
runtime-wide read, no UPDATE) with its inherited grant narrowed to
`SELECT, INSERT, DELETE`. The
platform permission dependency rebinds `app.user_id` before the caller's own
membership lookup and then binds `app.platform_admin`; the one-time bootstrap
grant, the signature-verified `user.deleted` webhook and the operator
recovery/teardown CLI bind the separate narrow `app.platform_service` context
(never the platform-admin flag, which also opens the cross-tenant audit read),
so none of those paths needs a bypass. RLS is enabled and forced on all four
tables, and the group's real-PostgreSQL suite proves the pre-authorisation self
read, the write denial, the cross-user platform/service access, the bootstrap
sentinel gating, the service-path bindings, pool-reuse safety and migration
reversibility. The review's blocking findings were applied before approval: the
identity-bearing bootstrap sentinel is no longer readable runtime-wide (the
hook verifies the profile, then binds the trusted context before reading it),
the earlier groups' inherited table-wide DML grant is revoked so each platform
table holds only its least privilege, and the real-PostgreSQL suite now
exercises the caller's own-row write denial, the service context's lack of
tenant access, the `user.deleted` binding and upgraded/downgraded grants.
**Human review recorded 2026-09-20:** the tenant-isolation,
database-role/grant/policy, control-plane platform-context and service-context
changes were reviewed and approved.

**Human review recorded 2026-09-20 (automated runtime-role check):** the
tenant-isolation and database-role safety of the startup/deployment verifier,
the control-plane isolation of `app_operator`, and the production startup and
backup/recovery implications of requiring it were reviewed and approved after
the blocking indirect-membership-traversal and fail-closed `SET ROLE` probe
findings were corrected. The indirect-rows bullet and the aggregate P4
completion evidence below remain unchecked.

**Human review recorded 2026-09-22 (indirect-row strategy conformance):** the
tenant-isolation and control-plane implications of the P4 indirect-row
conformance evidence were reviewed and approved. The unit adds the
real-PostgreSQL `app_runtime` suite over `job_attempts` (denormalised key),
`notification_deliveries` (parent existence) and `membership_roles` (parent read
split from tenant-checked write), and the ADR-0022 decision-6 P4 final-strategy
note recording those strategies in place of the interim exclusion. The review's
mandatory documentation-accuracy fix (the decision-6 contradiction) and its
non-blocking test-hardening findings were applied before approval.

Known follow-up (separately scoped, not group 6): `alembic check` reports drift
on `ix_invitations_lower_email` because the group-5 functional partial index is
not declared on the `Invitation` ORM model. It must be picked up as its own work
unit so the P2 migration-drift evidence returns green.

- [x] Implement the approved pre-tenant membership lookup without accepting
      arbitrary tenant context.
- [x] Protect membership, invitation and membership-role access according to
      their identity/control-plane classification.
- [x] Protect job attempts, notification deliveries and other indirect rows
      using the approved parent or denormalised-key strategy.
- [x] Handle global versus tenant audit/outbox events without treating a null
      tenant key as unrestricted access.
- [x] Give platform operations an explicit, narrowly scoped path and test that
      platform status alone cannot read ordinary tenant data.
- [x] Document and test migration, support, backup, restore and emergency
      access roles.
- [x] Audit use of any privileged operational path without placing secrets or
      row contents in the audit event.
- [x] Add startup or deployment checks proving runtime roles do not own
      protected tables and lack `BYPASSRLS`.

P4 completion evidence:

- [x] The table inventory records a final policy or reviewed exclusion for
      every table.
- [ ] No normal API or worker path uses owner, superuser or `BYPASSRLS`
      credentials.
- [ ] Platform and recovery procedures work without creating a hidden tenant
      bypass in the ordinary application.

## Reference map

| Checkpoint | Governing sources | What to extract |
| --- | --- | --- |
| P1 | BP §§8–13, §§28–31 and §§37–39; `SECURITY.md`; `docs/operations.md`; `docs/backup-and-recovery.md`; `AGENTS.md`; `docs/rls-table-inventory.md`; `backend/tests/tenant_isolation_registry.py`; ORM models under `backend/app/modules/` and `backend/app/ai/persistence/` | Table classification, tenant keys and ownership paths, readers/writers and access paths, raw SQL and bulk/relationship access, connection types, role and control-plane design, threat model and prototype criteria |
| P2 | BP §§9–11, §30 and §31; `docs/decisions/0022-postgresql-row-level-security.md`; `backend/app/db/session.py`; `backend/app/api/dependencies.py`; `backend/tests/test_org_isolation_matrix_db.py`; `backend/alembic/` | Separate runtime/owner roles, transaction-local context propagation, default-deny `USING`/`WITH CHECK` policies, pool-reuse safety, real-PostgreSQL tests, migration reversibility and performance measurement |
| P3 | BP §§9, 11, §§17–20 and §§28–31; `docs/decisions/0022-postgresql-row-level-security.md`; `docs/rls-rollout.md`; `backend/app/modules/` services and `queries.py`; `backend/tests/org_isolation_helpers.py` | Bounded table-group rollout and its per-group progress, user-private policies, worker context from durable rows, outbox/retry/reconciliation without bypass, index and plan review, error non-disclosure |
| P4 | BP §§8–10, §§28–31 and §39; `docs/decisions/0022-postgresql-row-level-security.md`; `backend/app/modules/platform_admin/`, `invitations/` and `permissions/`; `docs/backup-and-recovery.md` | Pre-tenant membership lookup, identity/control-plane classification, indirect-table tenant keys, nullable-tenant event treatment, explicit platform/operational paths, role-ownership and `BYPASSRLS` deployment checks |

## API, data and security impact

- **API and generated types:** P1 changes no route, request/response schema or
  generated frontend type; `PROTECTED_ROUTES` is unchanged. The prototype and
  rollout are internal access-control changes, so no public API break is
  planned.
- **Data and migrations:** prototype and rollout migrations are additive and
  reversible; prototype configuration is separate from any production
  enablement migration and is fully removable. Mixed-version and rollback
  behaviour is defined before enforcement.
- **Security:** default deny, least privilege and no runtime bypass. The normal
  runtime role is non-owner and non-`BYPASSRLS`; platform, maintenance and
  recovery paths are explicit, narrowly scoped and audited without recording
  row contents or secrets. A `NULL` tenant key never means unrestricted access.
- **Operations:** backup/restore, support and emergency roles are documented and
  tested with the intended role and policy definitions.

## Validation plan

- [ ] Run the complete two-organisation/different-role contract matrix against
      real PostgreSQL with the production-like runtime role.
- [ ] Run connection-pool stress tests with interleaved organisations.
- [ ] Run migration, lint, type, backend/frontend, generated-client and
      mandatory security gates on the supported toolchain.
- [ ] Test a mixed-version deployment and rollback before production
      enforcement.
- [ ] Test backup/restore into isolated infrastructure using the intended role
      and policy definitions.
- [ ] Update architecture, security and operations documentation with the
      exact guarantees, exclusions and privileged access procedure.
- [ ] Require human review of final policies, grants, role ownership and
      deployment configuration.

## Review and delivery

RLS affects tenant isolation, permissions, migrations, database roles and
operations. Human review is required before changes are applied.

- [x] Version-scope assignment (recorded human decision, 2026-09-18): the RLS
      evaluation is governed by this active plan, and a versioned release scope
      and immutable tag (anticipated as v0.9) are authored at release time,
      before production-wide enablement — not at the adoption gate. New code and
      documentation use version-prefixed citations from the point the release
      scope exists.
- [ ] Deliver each work unit through `implement -> review -> apply-and-commit`.
- [ ] Keep migrations additive and reversible during the prototype and staged
      rollout.
- [ ] Separate prototype migrations from any production enablement migration.
- [ ] Define rollback and mixed-version behaviour before enabling enforcement.
- [ ] Run policy and connection-pool tests against real PostgreSQL; mocked
      session tests are not sufficient evidence.
- [ ] Do not apply a production-wide policy rollout as part of approving the
      prototype.

Recommended sequence and expected size:

```text
P1 inventory and ADR
        |
        v
P2 records prototype (2–4 days)
        |
        v
 adoption gate -----> defer and record residual risk
        |
        v
P3 direct tenant data (5–8 days)
        |
        v
P4 control/indirect tables (3–7 days)
        |
        v
release verification and documentation
```

A safe comprehensive implementation is expected to require roughly 10–15
engineering days plus human review and deployment coordination. The prototype
is intentionally valuable on its own: it must be possible to stop after P2
without implicitly committing the project to a full rollout.
