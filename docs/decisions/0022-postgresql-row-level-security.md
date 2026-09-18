# ADR 0022: PostgreSQL Row-Level Security as a Tenant-Isolation Backstop

Status: Proposed (P1 design; awaiting human review to proceed to the P2
`records` prototype). Adoption is decided at the plan's adoption gate after P2.

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
sets `app.organisation_id` and never holds `BYPASSRLS`.

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
- Platform, health, authentication and public routes stay functional without
  fabricating a tenant context.

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

## Consequences

- P1 adds no runtime behaviour: this ADR and
  `docs/rls-table-inventory.md` are the deliverables, reviewed before P2.
- P2 introduces a prototype runtime role, reversible migrations that
  `ENABLE`/`FORCE` RLS on `records` and `record_revisions`, default-deny
  policies, parameterised transaction-local context, and real-PostgreSQL
  tests including pool-reuse and cross-organisation cases. The existing
  application-level scoping and `404` behaviour are unchanged.
- The adoption gate updates this ADR to one reviewed decision. A production
  rollout (P3/P4) is a separate, versioned, human-reviewed work stream; the
  versioned scope that carries it is assigned at that gate so the evaluation
  can be closed without implicitly committing to a rollout. Version-prefixed
  citations in P2+ code and documentation reference that scope once it exists.
- The plan's versioned-scope rule is resolved by recorded human decision
  (2026-09-18): the RLS evaluation is governed by the active plan, and a
  versioned release scope is assigned at the adoption gate before any
  production enablement. Prototype (P2) code is removable evaluation work with
  no release tag; version-prefixed citations apply once the release scope
  exists.

This decision follows blueprint §8 (authentication), §9 (organisations and
permissions), §10 (database conventions), §11 (transactions), §28
(observability), §29 (audit), §30 (security baseline — tenant-scoped queries,
least-privilege credentials and "no hidden universal bypass") and §31 (testing),
plus `SECURITY.md` and `docs/backup-and-recovery.md`.
