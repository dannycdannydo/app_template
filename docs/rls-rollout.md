# RLS Production Rollout Order and Rollback Procedure

Status: approved at the plan adoption gate (2026-09-18); execution guidance for
plan P3/P4 under the active RLS plan.

This is the approved table-group rollout order and rollback procedure for
enabling the PostgreSQL Row-Level Security (RLS) backstop designed in
`docs/decisions/0022-postgresql-row-level-security.md` (ADR-0022) and proven by
the P2 `records` prototype (`docs/rls-prototype-findings.md`). It supplements,
and never replaces, the application layer:

- validated organisation membership and permissions remain the first layer;
- every service query keeps its explicit `organisation_id` predicate;
- a foreign row remains a `404`; and
- the real-database isolation matrix and the mandatory security suite remain
  release gates.

## 1. Database roles

| Role | Credential | Used by | Attributes |
| --- | --- | --- | --- |
| `app_owner` | `DATABASE_URL` | Alembic DDL/seed only | schema owner; never the application runtime path |
| `app_runtime` | `DATABASE_RUNTIME_URL` | API and Dramatiq workers | non-owner, non-superuser, `NOBYPASSRLS`, `NOINHERIT` |
| `app_coordinator` | (P4 credential) | outbox coordinator | second non-bypass role, policies scoped to dispatch state, not a tenant |
| `app_operator` | (P4 credential) | backup/restore, support and emergency CLI | isolated, audited operational credential; loaded only by CLI/ops tooling |

`app_runtime` is provisioned by the P2 prototype migration (created `NOLOGIN`,
or safely adopted if a deployment pre-provisioned it, then granted a login
credential out of band). `app_coordinator` and `app_operator` are P4 designs and
are not created by the prototype. No runtime credential may be a member of a
superuser, a `BYPASSRLS` role or a protected-table owner.

## 2. Rollout principles

Every table group is enabled by its own bounded, additive, reversible Alembic
migration. A group is enabled only when all of the following hold:

1. the group migration **grants** the new tables/sequences to `app_runtime`
   (the P2 `ALL TABLES` grant covers only the tables present when it ran);
2. the group migration **enables and forces** RLS and installs default-deny
   policies with matching `USING` and `WITH CHECK`;
3. cross-organisation select/insert/update/delete tests for the group are green
   against real PostgreSQL with the restricted runtime role;
4. the representative query plans and supporting indexes have been reviewed and
   show no sequential-scan regression; and
5. the application-level predicates and `404` behaviour for the group are
   unchanged.

Rollout **stops** if any table needs an unexplained bypass; it returns to design
review rather than granting the runtime role an exemption.

## 3. Table-group order

The order follows plan P3 (direct tenant data), then plan P4 (identity,
control-plane and indirect tables). Within P3 the groups are ordered by risk and
blast radius.

| Order | Group | Tables | Policy / special handling |
| --- | --- | --- | --- |
| 0 | records (P3) | `records`, `record_revisions` | Group 0. Proven by P2; P3 repeats the policies as a production enablement migration separate from the prototype, with its own cross-organisation tests and reversible downgrade (plan P3, first bullet). |
| 1 | files (P3) | `files` | Organisation context. Signed download/promotion paths resolve the protected row first. |
| 2 | notifications (P3) | `notifications`, `notification_deliveries` | User-private: policy requires both organisation context and a transaction-local user ID. `notification_deliveries` inherits the parent boundary through `notifications`. |
| 3 | AI data (P3) | `ai_requests`, `ai_outputs`, `ai_attachment_references`, `ai_scratch_uploads` | Organisation context. Attachment resolution stays scoped by the protected row. |
| 4 | jobs and organisation settings (P3) | `jobs`, `job_attempts`, `organisation_features`, `organisation_ai_settings` | Workers bind `app.job_id` for a single-row bootstrap read, then bind organisation context from the durable `jobs` row. `job_attempts` takes a denormalised non-null `organisation_id` (additive, backfilled) or an approved parent-existence policy. |
| 5 | identity and control plane (P4) | `organisation_memberships`, `membership_roles`, `invitations`, platform tables | Pre-tenant membership lookup uses a user-keyed policy; platform operations use an explicit validated platform context, never an exemption. The cross-tenant teardown deletes are routed through the platform policy or `app_operator`. |
| 6 | operational ledgers (P4) | `audit_events`, `outbox_events`, `maintenance_runs`, `webhook_events` | A `NULL` tenant key never means "all rows". The coordinator uses `app_coordinator`, scoped to due/unclaimed dispatch state. `outbox_events` has no client read path. |
| 7 | platform-only plane (P4) | `platform_roles`, `platform_role_permissions`, `platform_memberships`, `bootstrap_states` | Control-plane policies keyed to the platform context. Platform status alone grants no tenant-row access. |

The machine-checked classification for every table is
`backend/tests/tenant_isolation_registry.py`; the human-readable detail is
`docs/rls-table-inventory.md`. Every table ends with either a tested policy or an
explicit reviewed exclusion recorded in the inventory.

## 4. Rollback procedure

Each group migration ships with a downgrade that reverses exactly that group and
leaves earlier groups intact:

1. `DROP POLICY IF EXISTS <group policies>` on each table in the group;
2. `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY` and
   `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` on each table in the group;
3. for the migration downgrade only, drop any group-added tenant-key
   column/backfill added by that migration — a destructive schema change that
   is applied through the destructive-migration human gate. A live operational
   policy rollback instead disables RLS and **retains** the additive column
   (see the two scopes below);
4. `REVOKE` only the grants that migration added; and
5. when the group is the last enabled group, remove the shared
   `app_current_tenant_id()` function and the `app_runtime` role exactly as the
   P2 prototype downgrade does (an adopted, deployment-managed role is left in
   place with its out-of-band credential).

Because the application predicates are retained, disabling a group's RLS
returns the application to the existing application-only enforcement rather than
to a broken state. There are two distinct rollback scopes:

- **Policy-layer rollback (operational).** Disable the group's RLS
  (`NO FORCE`/`DISABLE`) and keep the schema: the group-added tenant-key
  columns, backfills and grants stay in place, so the previous release still
  reads and writes normally. This is what the mixed-version window and the
  emergency stop below use, and it is *not* a schema rollback.
- **Migration downgrade (schema).** Running the group migration's `downgrade`
  reverses everything that migration added, including any group-added tenant-key
  column/backfill and its grants. Dropping a populated column is destructive, so
  it is scheduled through the destructive-migration human gate and only after
  the schema restore point is confirmed.

Rollback of the **database schema** otherwise remains a restore, not a partial
migration, per `docs/backup-and-recovery.md`.

**Mixed-version window (expand/contract).** Enabling a default-deny policy is
compatible only with consumers that already bind the group's transaction-local
context. Today's worker entry points read durable `jobs` and `files` rows before
binding any RLS context (`app/modules/files/tasks.py:100`,
`app/modules/notifications/tasks.py:96`), so enabling a `files`, `jobs` or
`notifications` policy while an old worker is still running makes those reads
return no rows. The documented migrate-first order is therefore **not** safe for
existing workers, and each group uses an expand/contract order instead:

1. **Expand** — deploy the context-propagating application code with policies
   still disabled: workers bind `app.job_id` for the single-row bootstrap read,
   then `app.organisation_id` from the durable row (ADR-0022 decision 3). The
   existing application predicates keep today's behaviour.
2. **Drain** — confirm no policy-incompatible process still runs, by replacing
   and draining the affected API/worker/coordinator processes for that group.
3. **Contract** — apply the group migration, which enables and forces RLS.
4. **Rollback** reverses the order: disable the group's policies first (the
   emergency stop below or the policy-layer downgrade), then roll back the
   application. A policy enabled while a context-free consumer still reads the
   group is an incident, not a supported mixed-version state.

**Per-group prerequisite.** Before enabling any group, every process that reads
its tables must bind the required context, proven by the group's
real-PostgreSQL policy tests and an explicit reader/writer inventory (section 2,
rollout principle 5). The `records` group needs no worker bootstrap beyond the
existing API context path; the `files`, `notifications`, AI-data and `jobs`
groups each gate on their worker bootstrap being deployed and drained.

**Emergency stop.** An operator with `app_owner` may neutralise a single group
without a migration:

```sql
ALTER TABLE <table> NO FORCE ROW LEVEL SECURITY;
ALTER TABLE <table> DISABLE ROW LEVEL SECURITY;
```

This is an incident action: record it without row contents or secrets, then
follow up with the group's reviewed downgrade so the schema and the migration
history agree.

**Verification after rollback.** `/ready` is healthy, `alembic current` matches
the expected revision, and the real-database isolation matrix and security suite
are green.

## 5. Deployment role confirmation

Before its first group is enforced, every environment must confirm that the
runtime credential authenticates as a distinct, restricted role. The production
template already declares `DATABASE_URL` (`app_owner`) and
`DATABASE_RUNTIME_URL` (`app_runtime`) in `.env.production.example`, and
`resolve_database_url` refuses to start a production process when the runtime
URL is unset. That is a **capability**, not proof: the resolver rejects only an
empty runtime URL and compares nothing about the credential itself, so an
environment can still configure both URLs to the same role. Actual separation
is confirmed by connecting through each credential and checking the
authenticated identity and its privileges:

```sql
-- Connect with DATABASE_URL and run:
SELECT current_user;   -- must be app_owner

-- Connect with DATABASE_RUNTIME_URL and run:
SELECT current_user;   -- must be app_runtime (distinct from the row above)

SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
FROM pg_roles
WHERE rolname IN ('app_owner', 'app_runtime');
-- app_owner may own objects; app_runtime must be rolsuper=false,
-- rolbypassrls=false, rolcreatedb=false, rolcreaterole=false.

-- The runtime role must own no protected table:
SELECT count(*) FROM pg_tables
WHERE schemaname = 'public' AND tableowner = 'app_runtime';

-- The runtime role must inherit no superuser/BYPASSRLS/protected-owner role:
SELECT granted.rolname
FROM pg_auth_members m
JOIN pg_roles granted ON granted.oid = m.roleid
JOIN pg_roles member  ON member.oid  = m.member
WHERE member.rolname = 'app_runtime';
```

The two `current_user` results must differ and name the intended roles, the
protected-table-ownership count must be zero, and the inherited-membership set
must contain no superuser, `BYPASSRLS` or protected-table-owner role. The same
per-environment verification is repeated in `docs/operations.md`. Until it is
automated, the adoption gate records this confirmation as **capability**; plan
P4 adds the startup/deployment check that proves it automatically.

## 6. Execution contract and release bookkeeping

The rollout is executed by the active plan
`plans/2026-09-18-postgresql-row-level-security-plan.md` (checkpoints P3 and
P4); no separate scope is required to make progress. A versioned release scope
and immutable tag (anticipated as v0.9) are authored at release time, before
production-wide enablement, and rollout code and documentation adopt
version-prefixed citations from that point. P3 and P4 remain separately
reviewed work units, and no production policy is enabled as part of approving
the prototype.
