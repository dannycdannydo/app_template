# ADR 0022: PostgreSQL Row-Level Security as a Tenant-Isolation Backstop

Status: Accepted (2026-09-18 adoption gate: RLS is adopted as a
tenant-isolation backstop after the P2 `records` prototype met its success
criteria). Production enablement proceeds through plan P3/P4 as separately
reviewed work units under the active RLS plan.

## Context

The organisation is the hard tenant boundary (BP §9). Today that boundary is
enforced **only in the application**:

- a validated `X-Org-Id` context and an active-membership check resolve the
  caller's organisation (`app/api/dependencies.py`);
- every tenant query filters on `organisation_id` in `queries.py`/services, and
  a foreign row is a `404`;
- `backend/tests/tenant_isolation_registry.py` classifies every table and
  `backend/tests/test_org_isolation_matrix_db.py` proves the boundary against
  real PostgreSQL with a two-organisation, two-plane world.

That layer is strong, but it has one structural weakness: it is only as correct
as the last query someone wrote. A single missed or wrong `organisation_id`
predicate in a new service, a refactor, a raw/bulk statement or a relationship
load is a direct cross-tenant read or write, and nothing independent stops it.

PostgreSQL Row-Level Security (RLS) can supply that independent backstop: a
default-deny policy on the row itself, evaluated by the database, that returns
no foreign rows even when the application predicate is absent. It is not free.
It requires separate database roles, transaction-local context that survives
neither commit nor pool reuse, a policy for every protected table, explicit
paths for control-plane work, migration and rollback care, and real-PostgreSQL
proof. The plan therefore separates the **design** (P1), a bounded **prototype**
on the representative `records` module (P2), an explicit **adoption gate**, and
only then a staged rollout (P3/P4).

The full table and connection inventory is
`docs/rls-table-inventory.md`. The machine-checked classification remains
`backend/tests/tenant_isolation_registry.py`.

## Options considered

1. **Continue with application-only enforcement (status quo).** No new database
   roles, pool-context handling, policy maintenance, planning risk or
   operational ceremony; the existing matrix and registry already cover the
   common regression. But the missed-predicate class remains unmitigated by an
   independent control, and that is the failure mode the plan exists to close.
2. **RLS as a defence-in-depth backstop on top of the application layer.**
   Retains every existing predicate, permission check and `404` behaviour as the
   first layer, and adds a database default-deny that makes a missed predicate
   return no rows instead of foreign rows. Costs: role separation, policy
   design, transaction-local context, plan/index review, migration and
   backup/restore work, and an explicit platform/maintenance path.
3. **Replace application scoping with RLS.** Rejected: it would make the
   database the sole authority, remove the `404` semantics and permission
   reasoning that the API and tests depend on, and concentrate all tenant logic
   in policies that are harder to unit-test and review.
4. **Full rollout now.** Rejected: the plan deliberately makes adoption
   conditional on a bounded, measurable prototype and a recorded human decision.

**Decision:** pursue option 2, conditionally, starting with the P2 prototype.
P1 approves *design and prototyping*, not production enablement. If P2 does not
meet its criteria, the plan records the residual risk and closes without P3/P4.

## Decision

### 1. Enforcement model

Application-level scoping, permissions and foreign-resource `404` behaviour stay
in place as the first enforcement layer and are never relaxed when RLS is
enabled. RLS is added only as a default-deny backstop for the ordinary runtime
connection. The target invariant is:

> an ordinary application connection cannot read or change an
> organisation-owned row without trusted transaction-local context authorising
> that row.

### 2. Separate schema-owner, runtime and coordinator roles

The roles are introduced (names are illustrative; the deployment names them):

- **`app_owner`** — owns the tables and runs Alembic DDL/seed migrations. It is
  never the application runtime credential.
- **`app_runtime`** — the normal application path (API and Dramatiq workers).
  It is a non-owner, non-superuser role **without `BYPASSRLS`** and is subject
  to every enabled policy.
- **`app_coordinator`** — the outbox coordinator's role. Its legitimate scope is
  the global dispatch ledgers across all tenants, so it is a **second
  non-bypass** role with policies scoped to dispatch state rather than to a
  tenant (decision 3). It is non-owner and lacks `BYPASSRLS`.
- **`app_operator`** — the isolated operational credential (decision 4).

`app_owner`, `app_coordinator` and `app_operator` credentials are configured
separately from the runtime `DATABASE_URL`. The P2 prototype must demonstrate
that the runtime credential cannot disable policies, alter protected schema or
assume the owner role. A deployment/startup check proving the runtime role is
non-owner and lacks `BYPASSRLS` is P4 evidence.

### 3. Workers share the runtime role; the coordinator gets an explicit non-bypass role

**Dramatiq workers share `app_runtime`.** The reference-only broker message
carries exactly one opaque UUID (`job_id`; `app/job_coordinator/registry.py`), and a worker
must read its durable `jobs` row before it can know the organisation. Once
`jobs` is protected this is circular unless designed for, so the design defines
a narrow bootstrap rather than a bypass:

1. The worker sets transaction-local `app.job_id` from the message's opaque id
   and reads exactly the one `jobs` row whose `id` matches, under a policy of
   the form `id = current_setting('app.job_id')`. This is a single-row read by
   an unguessable UUID; it grants no enumeration and does not treat the broker
   as a tenant authority.
2. The worker validates the row through the existing claim/fencing path (job
   type, current dispatch and owner token) and only then binds
   `app.organisation_id` from the row's own `organisation_id`. The tenant
   context therefore comes from the durable row, never from the broker.
3. A message whose id matches no row, or whose row is not the current dispatch,
   returns no row and is acknowledged as stale — the existing terminal/stale
   behaviour.
4. `app.job_id` is transaction-local and is cleared before the tenant-context
   phase; it cannot survive commit or pool reuse. The same pattern covers
   `settle_after_retries_exhausted` (`app/modules/jobs/tasks.py`).

**The outbox coordinator uses `app_coordinator`, not `app_runtime`.** The
coordinator legitimately reads and writes `outbox_events`, `jobs` dispatch
state and `maintenance_runs` across every tenant; that is global dispatch, not
tenant data. `app_coordinator` is a second non-bypass role whose policies are
scoped to dispatch state (due/unclaimed rows and their dispatch/lease
correlation) rather than to an `organisation_id`, and it cannot read tenant
payload tables (`records`, `files`, `notifications`, AI content). This is the
plan's "second non-bypass role with equally explicit context" option; it never
sets an arbitrary `app.organisation_id` and never holds `BYPASSRLS`.

**Group-4b amendment (approved 2026-09-19).** Group 4b's
coordinator runs the bounded recovery that can terminally fail an attempt at
the global ceiling, and the registered `notification.email` exhaustion hook
runs inside that settlement transaction to finalize the delivery row. That hook
binds the durable job's own organisation and recipient user as transaction-local
context before it touches the user-private rows, so the group-4b migration
grants `app_coordinator` DML on `notifications`/`notification_deliveries` and
relies on the existing, context-gated user-private policies — never a
tenant-broad or request-selectable read. The coordinator still holds no
`BYPASSRLS` and cannot read those rows without the durable row's context. This
narrow exception is the only deviation from the "no notification access"
sentence above; it is recorded here and was approved 2026-09-19 together with
the group-4b tenant-isolation, database-role/grant/policy, worker-context and
destructive-downgrade changes.

Two further group-4b constraints keep the coordinator and worker paths
least-privilege. The coordinator's UPDATE authority on `jobs`/`job_attempts` is
granted **per column** (only the settlement/reconciliation columns) and its
settle policies restrict the reachable post-update states, so no permissive
`WITH CHECK (true)` can let it move a tenant key or rewrite a payload,
reference, progress or ownership field. The `jobs` worker bootstrap is
`FOR SELECT` only — because PostgreSQL applies UPDATE policies to
`SELECT ... FOR UPDATE`, a job-keyed bootstrap UPDATE policy would have
authorised a real update by job id; the worker instead clears `app.job_id`,
binds the durable row's organisation, and locks under tenant authority.
`job_attempts.organisation_id` is tied to its parent job by a composite
`(job_id, organisation_id)` foreign key (with the matching unique pair on
`jobs`), so the copied tenant key can never diverge from the parent.

Both mechanisms are required before the affected table group is enabled: the
`records`/`record_revisions` prototype does not depend on them, but the `jobs`
and operational-ledger groups (P3/P4) do, and they are tested there. The
in-process metrics loop reads only aggregate operational counts and uses the
same explicit coordinator/operational policy rather than tenant context.

### 4. Narrow platform and maintenance access — no universal runtime bypass

There is **no implicit and no request-selectable universal bypass**. The runtime
role cannot set a flag or header that disables RLS, and no request handler
chooses its own role. Control-plane access is split:

- **In-app platform plane.** The `/api/v1/platform/*` routes keep running on
  `app_runtime` and remain subject to RLS. Cross-tenant platform reads/writes
  are permitted only through explicit policies keyed to a validated,
  transaction-local *platform context*, designed and tested in P4 — never by
  exempting the table or by a runtime bypass. Platform status still grants no
  tenant-row access by itself (BP §30 "no hidden universal bypass").
- **P3 group-4a per-organisation settings binding (narrow, approved
  exception).** The two platform-managed organisation-settings tables
  (`organisation_features`, `organisation_ai_settings`) are organisation-owned
  but have no tenant-plane resource-detail surface: each platform operation
  names exactly one organisation and touches only that organisation's settings
  row, and the platform permission dependency has already validated the caller
  before the service runs. Enabling their default-deny policies in P3 group 4a
  would otherwise strand that plane until P4. As a deliberately narrow,
  human-reviewed exception, the feature-flag and AI-settings platform services
  bind exactly the targeted organisation's transaction-local context after that
  permission check, and the organisation-creation paths bind the new
  organisation before writing its default settings row. This is not a
  request-selectable or universal bypass: the bound organisation is the
  operation's own validated target and no other protected table is read in
  those transactions. The P4 validated platform context remains the design for
  general cross-tenant platform access. Amended 2026-09-19 with recorded human
  approval of the tenant-isolation, migration/database-role and
  platform-binding changes.
- **Operational tooling.** Backup/restore, support recovery, emergency and
  data-bearing maintenance use a separate **`app_operator`** credential with the
  privilege they require (potentially `BYPASSRLS`). It is loaded only by audited
  CLI/ops tooling and never by an HTTP process or worker. `app_operator` use is
  audited without placing row contents or secrets in the audit event.
- Plain DDL migrations run on `app_owner`, not `app_operator`; they do not need
  a bypass.

### 5. User-private policies require a transaction-local user ID

`user-private` tables (`notifications`, and any future recipient-scoped table)
take a policy requiring both organisation context and a transaction-local user
ID. The application `user_id` predicate stays regardless. `records` and
`record_revisions` are organisation-owned only, so the P2 prototype does not
introduce user context; user-private enforcement arrives with the P3
notification/AI table group.

### 6. Indirectly owned tables — add the tenant key, otherwise document an exclusion

The preferred strategy is to add a denormalised, non-null `organisation_id` to
indirectly owned tables whose parent is organisation-owned (`job_attempts`,
`notification_deliveries`, `membership_roles`), backfilled in an additive,
reversible migration, with the existing parent foreign key retained. A single
direct policy form then applies and correctness no longer depends on a join.

Until that migration exists those tables receive **no** generic
`organisation_id = context` policy (which would deny every row) and are recorded
in the inventory as an explicit, reviewed exclusion enforced only by
parent-existence application predicates. `record_revisions` and `ai_outputs`
already carry `organisation_id` and keep their parent/composite foreign keys.

### 7. Nullable-tenant audit/outbox rows and global events

A `NULL organisation_id` never means "all rows". The policy form is
`organisation_id = current_setting('app.organisation_id')`; an `IS NULL OR`
variant that would expose global/system rows to every tenant is prohibited.
Tenant reads of `audit_events` filter on the non-null value. Global system
events (null tenant) are reachable only through `app_operator` or a future
dedicated global context, never through a tenant context. `outbox_events`
remains an internal ledger with no client read path.

### 8. Pre-tenant authentication and membership lookup

Authentication and membership resolution necessarily happen before an
organisation context exists. The design binds context in two stages:

1. After session validation, bind transaction-local **`app.user_id`**.
2. `organisation_memberships` is resolved by the caller's `user_id` under a
   user-keyed policy while the active membership is validated.
3. Only after the membership is confirmed `active` for the `X-Org-Id`
   organisation is **`app.organisation_id`** bound, for that transaction only.

Absent, empty or malformed context returns no tenant rows and fails closed for
writes; it never means unrestricted. Membership lookup never accepts an
arbitrary tenant context from a request body or broker message.

**Group-5 implementation amendment.** *This note describes the intended
implementation; it does not itself record the human review that plan P4 and
`AGENTS.md` require before apply-and-commit.* The identity group
(`organisation_memberships`, `membership_roles`, `invitations`) implements
decision 8 as follows. The authenticated user is
bound as transaction-local `app.user_id` before the pre-tenant lookups;
`organisation_memberships` takes a **SELECT-only** user-keyed policy (a user
can read their own memberships but can never write one, so no user-keyed insert
can join an arbitrary organisation); `membership_roles` splits decision-6
**read visibility** from **write authority** — a `FOR SELECT` parent-existence
policy follows the RLS-filtered parent membership's visibility rather than a
denormalised key, while a separate `FOR ALL` policy requires the parent
membership's own `organisation_id` to equal the validated
`app_current_tenant_id()`, so a pre-tenant user context can read its own grants
but can never insert, update or delete one; and `invitations` takes the
canonical organisation policy plus an invitee email-keyed select/update pair
(`app_current_user_email()`) for login-time linking. Runtime `UPDATE` on
`invitations` is **column-restricted to `status`/`updated_at`**, so the invitee
path cannot move an invitation's organisation, email or role. The
signature-verified `invitation.revoked` webhook is a control-plane path with no
tenant or user identity: it binds the verified event's provider invitation id
as transaction-local `app.invitation_provider_id` and the single-row
`invitations_webhook_provider_select`/`invitations_webhook_provider_update`
policies admit exactly that row for a read/lock and a status flip only (no
insert or delete authority) — the decision-3 `app.job_id` bootstrap pattern,
never a bypass. Platform membership
and invitation operations continue to bind exactly the organisation they target
(decision 4's per-organisation platform path), and the cross-tenant
`delete_provisioned_user` teardown binds the target user, reads their
memberships under the user-keyed policy, then deletes per organisation without
`app_operator`.

### 9. Context propagation

- Set context with a **parameterised** transaction-local operation
  (`set_config('app.organisation_id', $1, true)`), never by interpolating a
  request value into SQL.
- The `SET LOCAL` and the protected query execute in the **same transaction**;
  context is set only after the membership/permission path has validated the
  selected organisation.
- Transaction-local context clears automatically on commit and rollback; it
  must be provably absent after commit, rollback, exception, cancellation,
  timeout and pooled-connection reuse.
- Health, authentication and public routes stay functional without fabricating
  a tenant context. Platform routes bind no request-selected tenant either,
  with the single reviewed exception of decision 4's P3 group-4a
  per-organisation settings binding.

### 10. Threat model, cost and residual risk

**In scope** (what RLS is expected to stop):

- a missed or wrong `organisation_id` predicate in a new or refactored query;
- a bulk update/delete or raw statement that skips the service predicate;
- a relationship/lazy load that traverses a parent without the tenant filter;
- a worker/coordinator operating on a row whose context is absent or wrong;
- context leaking across pooled connections.

**Out of scope / residual**:

- a runtime credential able to run arbitrary SQL and set arbitrary context
  (RLS is a backstop, not a substitute for least privilege or network
  isolation);
- a compromised `app_owner`/`app_operator` credential or database host;
- timing/side-channel row-count inference (mitigated by `404` semantics, not
  fully by RLS);
- inference through shared global/catalogue tables.

**Operational cost**: role provisioning and credential separation in every
environment; additive, reversible migrations and mixed-version rollback; pool
context correctness; policy-driven plan/index review; a larger real-PostgreSQL
test matrix; and documented backup/restore role handling.

**Residual risk if the prototype fails or adoption is rejected**: the
missed-predicate class stays unmitigated by an independent control. The existing
real-database matrix and registry reduce but do not eliminate it; the adoption
gate records this explicitly (plan §7).

### 11. Objective P2 prototype success criteria

The prototype on `records`/`record_revisions` succeeds only if, against real
PostgreSQL with a non-owner, non-`BYPASSRLS` runtime role:

1. default denial — no/empty/malformed context returns zero tenant rows and
   fails closed for writes;
2. organisation A can read its rows and cannot select, insert, update or delete
   organisation B rows;
3. an **unscoped** query returns only the authorised organisation's rows;
4. `WITH CHECK` rejects a mismatched insert and a tenant-key update (equivalent
   `USING` and `WITH CHECK`);
5. context cannot survive commit, rollback, exception, cancellation, timeout or
   pooled-connection reuse;
6. a user who is owner in A and viewer in B receives the correct context and
   application permissions in each request;
7. the runtime credentials cannot disable policies, alter protected schema or
   assume the owner role;
8. migration upgrade, downgrade and re-upgrade pass, and `alembic check` stays
   green;
9. the existing records API and mandatory security tests remain green; and
10. query plans and latency for representative list/detail/write operations are
    measured, recorded, and show no sequential-scan regression with a bounded,
    documented overhead.

Prototype configuration (role, policies, migrations) is removable without
residue; production enablement is a separate, later, human-reviewed migration.

### 12. Operational-ledger and operator-credential implementation (group 6)

Plan P4 group 6 (migration `a2b3c4d5e6f7`) implements decisions 4 and 7 for the
operational ledgers:

- `audit_events` is append-only under RLS: an own-tenant SELECT policy, a
  **tenant-checked** INSERT policy and **no** UPDATE/DELETE policy. The INSERT
  `WITH CHECK` admits only the writer's own validated tenant, a global
  (null-tenant) row, or any row under the validated platform context, so a
  foreign-tenant attribution is denied at the policy even if a service predicate
  is ever missed. The cross-tenant and global (null-tenant) history is admitted
  only by `audit_events_platform_read`, keyed to the transaction-local
  `app.platform_admin` flag that `require_platform_permission` binds after it
  has validated the caller. This is decision 4's reviewed platform context, not
  a table exemption, and the flag is referenced by no other table's policy.
- `outbox_events` keeps a null-safe split: the runtime role reads/appends only
  its own tenant's rows (or the global maintenance rows it produces), while
  `app_coordinator` owns the dispatch lifecycle with UPDATE `USING` bounded to
  the dispatch states (including `published` so the retention sweep can take a
  locking read), DELETE bounded to `published` rows, and **column-level** UPDATE
  grants limited to the claim/settle/release/recovery columns (`status`,
  `claimed_at`, `claim_token`, `attempt_count`, `processed_at`, `last_error`,
  `available_at`), so it cannot move a tenant key or rewrite a payload.
- `maintenance_runs` and `webhook_events` carry no tenant key and admit only the
  roles that own their paths.
- Because an append-only ledger grants no SELECT to every writer and PostgreSQL
  applies the SELECT policies to `INSERT ... RETURNING`, the ledger timestamp
  columns carry a Python-side default as well as the database default, so a
  writer never needs a `RETURNING` read it may not be permitted (the schema
  default is retained for direct SQL).
- The isolated `app_operator` credential is created (`NOLOGIN`, non-owner,
  member of no application role, `BYPASSRLS`) and resolved only by
  `DATABASE_OPERATOR_URL`/`resolve_operator_database_url` for audited CLI/ops
  tooling. The ordinary runtime role remains non-bypass and cannot assume it. A
  pre-existing (deployment-provisioned) role is adopted only after full
  normalisation: safe attributes forced, table ownership refused, and
  memberships revoked in **both** directions (including a dangerous
  `app_runtime -> app_operator` grant). The downgrade always revokes the
  migration-added `USAGE`/`SELECT` grants and drops the role only when this
  migration created it.

This note describes the implementation; it does not itself record the human
review that plan P4 and `AGENTS.md` require before apply-and-commit.

### 13. Platform-only-plane implementation (group 7)

Plan P4 group 7 (migration `b4c5d6e7f8a9`) implements decision 4 for the
platform-only plane (`platform_roles`, `platform_role_permissions`,
`platform_memberships`, `bootstrap_states`), which grants no tenant rows by
itself:

- The **pre-authorisation self read** is the platform-plane analogue of
  decision 8: `require_platform_permission` and `/me` resolve the caller's own
  platform membership before a platform context exists, so
  `platform_memberships_self_isolation` is **SELECT only** and keyed to
  `app.user_id`. A user context reads its own membership but can never insert,
  update or delete one — a user-keyed insert is deliberately absent, so no user
  context can grant itself platform authority. `platform_roles` and
  `platform_role_permissions` take a runtime read policy as the global
  catalogue the lookup joins through (the organisation `roles`/`permissions`
  catalogue is likewise not tenant-scoped). Group 7 revokes the table-wide DML
  grant the earlier groups inherited on all four platform tables and re-grants
  only the catalogue `SELECT`, so there is no runtime write grant on either.
- The **validated platform context** (`app.platform_admin`, decision 4) admits
  the cross-user list/grant/revoke through
  `platform_memberships_platform_access`. The platform tables carry no tenant
  rows, so this context grants no tenant-data access.
- A **separate narrow service context** (`app.platform_service`) is bound by the
  three trusted non-interactive paths that need cross-user platform-table
  access with no platform administrator present: the one-time bootstrap grant
  (verified email, then sentinel read/insert), the signature-verified
  `user.deleted` webhook deactivation and the operator recovery/teardown CLI. It
  is deliberately **not** `app.platform_admin`: that flag also opens the group-6
  cross-tenant audit read, so reusing it would hand those paths audit access
  they do not need. The service flag is referenced only by the group-7
  policies, is bound only by those trusted paths after their own validation,
  and grants no tenant-row access.
- `bootstrap_states` records the consuming administrator's verified email, user
  id and timestamp, so it is **not** readable runtime-wide: the bootstrap hook
  verifies the WorkOS profile first, then binds the trusted service context and
  reads the singleton to decide whether it is already consumed. Its read, insert
  and delete are all gated to the validated platform/service context (no
  runtime-wide read), its runtime grant is `SELECT, INSERT, DELETE` only, and
  there is no UPDATE policy, so a tenant context can neither read nor claim nor
  clear the bootstrap and the immutable sentinel can never be modified.

The ordinary runtime role remains non-owner and non-`BYPASSRLS` throughout; RLS
is enabled and forced on all four tables. This note describes the
implementation; it does not itself record the human review that plan P4 and
`AGENTS.md` require before apply-and-commit.

## Adoption decision (2026-09-18 gate)

The plan's post-P2 adoption gate is resolved: **PostgreSQL RLS is adopted** as a
defence-in-depth tenant-isolation backstop. The `records`/`record_revisions`
prototype met every objective success criterion in decision 11 against real
PostgreSQL with the restricted `app_runtime` role:

- default denial and fail-closed writes for absent, empty or malformed context;
- organisation A can read its own rows and cannot select, insert, update or
  delete organisation B rows;
- an **unscoped** query returns only the authorised organisation's rows;
- `WITH CHECK` rejects a mismatched insert and a tenant-key update;
- context does not survive commit, rollback, exception, cancellation, timeout or
  pooled-connection reuse;
- a user who is owner in A and viewer in B receives the correct context and
  application permissions in each request;
- the runtime credential cannot disable policies, alter the schema or assume the
  owner role;
- upgrade, downgrade and re-upgrade pass and `alembic check` stays green;
- the existing records API and mandatory security suites stay green; and
- representative list/detail plans show no sequential-scan regression and a
  bounded overhead (list ~2.7 ms, detail ~2.4 ms, insert ~5.2 ms).

Evidence: `docs/rls-prototype-findings.md` and
`backend/tests/test_rls_records_db.py`.

### Approved rollout order and rollback

The approved production enablement order, the per-table-group requirements and
the rollback procedure are recorded in `docs/rls-rollout.md`. Rollout proceeds
in bounded table groups, each as its own additive, reversible migration with
cross-organisation tests and a query-plan review before enforcement. The
`records` group is already proven by P2 and needs only its production
enablement migration.

### Release bookkeeping

The production rollout (plan P3/P4) continues under the active plan, which
remains the execution contract. A versioned release scope and immutable tag
(anticipated as v0.9) are authored at release time, before production-wide
enablement; rollout code and documentation adopt version-prefixed citations
from that point.

### Deployment role separation

Every deployment environment must provide distinct database credentials before
its table group is enabled:

- **`app_owner`** — schema owner; Alembic/DDL only, never the runtime path
  (`DATABASE_URL`);
- **`app_runtime`** — the ordinary API and worker path; non-owner,
  non-superuser and without `BYPASSRLS` (`DATABASE_RUNTIME_URL`);
- **`app_coordinator`** (P4) — the outbox coordinator's second non-bypass role,
  scoped to dispatch state rather than a tenant; and
- **`app_operator`** (P4) — the isolated, audited operational tooling
  credential.

The production template already carries `DATABASE_URL` (`app_owner`) and
`DATABASE_RUNTIME_URL` (`app_runtime`) as separate configuration, and
`app/db/session.py::resolve_database_url` refuses to start a production process
without the runtime credential. This is a **capability**, not proof of
separation: the resolver rejects only an empty runtime URL and never inspects
the credential, so an environment can still point both URLs at the same role.
Each environment must confirm the separation by connecting through each
credential and checking `current_user`, role attributes, protected-table
ownership and inherited memberships; the per-environment procedure is in
`docs/operations.md` and `docs/rls-rollout.md`. Plan P4 adds the automated
startup/deployment check.

## Consequences

- P1 adds no runtime behaviour: this ADR and
  `docs/rls-table-inventory.md` are the deliverables, reviewed before P2.
- P2 introduces a prototype runtime role, reversible migrations that
  `ENABLE`/`FORCE` RLS on `records` and `record_revisions`, default-deny
  policies, parameterised transaction-local context, and real-PostgreSQL
  tests including pool-reuse and cross-organisation cases. The existing
  application-level scoping and `404` behaviour are unchanged.
- The adoption gate (2026-09-18) resolved this ADR to **Accepted**: RLS is
  adopted. The production rollout (P3/P4) continues under the active plan as a
  separate, versioned, human-reviewed work stream, so approving the prototype
  did not by itself enable any production policy. A release scope and tag are
  authored at release time; until then the active plan is the execution
  contract.
- The plan's versioned-scope rule is resolved by recorded human decision
  (2026-09-18): the RLS evaluation is governed by the active plan, and a
  versioned release scope and immutable tag are authored at release time,
  before any production-wide enablement — not at the adoption gate. Prototype
  (P2) code is removable evaluation work with no release tag; version-prefixed
  citations apply once the release scope exists.

This decision follows blueprint §8 (authentication), §9 (organisations and
permissions), §10 (database conventions), §11 (transactions), §28
(observability), §29 (audit), §30 (security baseline — tenant-scoped queries,
least-privilege credentials and "no hidden universal bypass") and §31 (testing),
plus `SECURITY.md` and `docs/backup-and-recovery.md`.
