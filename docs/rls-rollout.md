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
| `app_metrics` | none (NOLOGIN) | the `SECURITY DEFINER` aggregate metrics function only | non-owner, non-superuser, `NOBYPASSRLS`, `NOINHERIT`; narrow policy scoped to attention-required delivery rows |
| `app_coordinator` | `DATABASE_COORDINATOR_URL` (group 4b) | outbox coordinator, reliability-metrics refresh and `reconcile_jobs` CLI | second non-bypass role, policies scoped to dispatch state, not a tenant |
| `app_operator` | `DATABASE_OPERATOR_URL` (group 6) | backup/restore, support and emergency CLI | isolated, audited operational credential; owns no table, member of no other role; may carry `BYPASSRLS`; loaded only by CLI/ops tooling |

`app_runtime` is provisioned by the P2 prototype migration (created `NOLOGIN`,
or safely adopted if a deployment pre-provisioned it, then granted a login
credential out of band). `app_coordinator` is created by its group-4b
migration, and `app_operator` by the group-6 migration, each `NOLOGIN` and
safely adopted if a deployment pre-provisioned it. No runtime credential may be
a member of a superuser, a `BYPASSRLS` role or a protected-table owner.

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
| 0 | records (P3) | `records`, `record_revisions` | **Delivered.** Group 0. Proven by P2; production enablement migration `d2e3f4a5b6c7` installs the canonical `<table>_organisation_isolation` policies, replaces the prototype policy, and ships a reversible downgrade and the `test_rls_records_enablement_db.py` cross-organisation suite. |
| 1 | files (P3) | `files` | **Delivered.** Group 1. Production enablement migration `e3f4a5b6c7d8` installs the canonical `files_organisation_isolation` policy, enables and forces RLS, and ships a reversible downgrade. Organisation context is bound by the files service for every protected API/worker transaction and by `DocumentSourceAuthority` for the durable AI/document-authority read, both covered by the `test_rls_files_enablement_db.py` cross-organisation suite. |
| 2 | notifications (P3) | `notifications`, `notification_deliveries` | **Delivered.** Group 2. Production enablement migration `f5a6b7c8d9e0` installs the canonical user-private `notifications_user_isolation` policy (organisation + transaction-local user) and the `notification_deliveries_parent_isolation` policy (parent `EXISTS` with the same organisation+user predicate), enables and forces RLS, owns the new `app_current_user_id()` helper, and ships a reversible downgrade. The API dependency binds the authenticated user alongside the tenant, and the email worker binds the durable `jobs` row's `organisation_id` and `created_by_user_id` on every self-committing protected transaction; both are covered by the `test_rls_notifications_enablement_db.py` cross-organisation/cross-recipient suite. The group also ships its required operational read: the `app_metrics` non-`BYPASSRLS` role owns the `notification_deliveries_operational_count` `FOR SELECT` policy (scoped to `attention_required` rows) and the `SECURITY DEFINER` aggregate `app_attention_required_delivery_count()`, which the runtime role may execute; the in-process `attention_required_email_deliveries` metric therefore reports the true cross-tenant count without a bypass and without exposing delivery rows (ADR-0022 decision 3). |
| 3 | AI data (P3) | `ai_requests`, `ai_outputs`, `ai_attachment_references`, `ai_scratch_uploads` | **Delivered.** Group 3. Production enablement migration `b8c9d0e1f2a3` installs the canonical `<table>_organisation_isolation` policies, enables and forces RLS, and ships a reversible downgrade. Organisation context is bound by the `ai.execute` worker from the durable `jobs` row (ADR-0022 decision 3) and rebound after every internal commit by the AI persistence port and the transfer reference store; the API path keeps its membership-dependency context. Attachment resolution stays scoped by the protected row (`DocumentSourceAuthority`), and the three formerly global cross-tenant sweeps — retention/stale reservation (`enforce_ai_retention`), scratch expiry (`expire_scratch_uploads`) and provider-file reconciliation (`reconcile_provider_file_references`) — now iterate the global, unprotected `organisations` table and bind each tenant before touching its AI rows, so they satisfy the default-deny policies without a bypass (ADR-0022 decision 4); the reconciliation sweep allocates its global batch budget fairly across organisations so one tenant cannot starve later tenants. Covered by the `test_rls_ai_data_enablement_db.py` cross-organisation, worker-binding and per-organisation-sweep suite, which exercises own-tenant and cross-tenant insert/update/delete and tenant-key-move on every enabled table. |
| 4a | organisation settings (P3) | `organisation_features`, `organisation_ai_settings` | **Delivered.** Group 4a. Production enablement migration `c9d0e1f2a3b4` installs the canonical `<table>_organisation_isolation` policies, enables and forces RLS, and ships a reversible downgrade. Both tables are organisation-owned but managed from the platform plane; the feature-flag and AI-settings platform services bind exactly the organisation they target after the platform permission dependency validates the caller, and every organisation-creation path (tenant `create_organisation`, platform `create_platform_organisation`, platform bootstrap) binds the new organisation before writing its default settings row. The per-organisation platform binding is the narrow, human-reviewed exception to ADR-0022 decision 4 recorded there and in §3.1. Covered by the `test_rls_organisation_settings_enablement_db.py` suite: cross-organisation select/insert/update/delete and tenant-key-move, platform-plane service binding, the missing-row and organisation-creation create/update paths under the restricted role, representative multi-tenant `EXPLAIN` plans for both lookups (no sequential-scan regression, rollout principle 4), and migration reversibility. The group has **no tenant resource-detail error surface** (both tables are reached only through the per-organisation platform plane; its sole `404` is `organisation_not_found` from the unprotected `organisations` table), so it claims no part of the aggregate P3 "application errors do not disclose whether RLS hid a foreign row" checkbox. |
| 4b | jobs (P3) | `jobs`, `job_attempts` | **Delivered.** Group 4b. Production enablement migration `d0e1f2a3b4c5` installs the canonical runtime `<table>_organisation_isolation` policies, the single-row `jobs_worker_bootstrap` `FOR SELECT` policy and the `app_current_job_id()` helper, adds the denormalised non-null `job_attempts.organisation_id` (ADR-0022 decision 6) tied to its parent job by a composite `(job_id, organisation_id)` foreign key (with the matching unique pair on `jobs`), enables and forces RLS on both tables, and ships a reversible downgrade. It also delivers the part-4b prerequisite: the non-bypass `app_coordinator` role (`DATABASE_COORDINATOR_URL`) with dispatch-state-scoped read policies and **column-level** UPDATE grants limited to the settlement/reconciliation columns on `jobs`/`job_attempts` (so the coordinator can settle a dispatch but cannot move a tenant key, rewrite a payload/reference, edit progress or change ownership identity). The outbox coordinator, the in-process reliability-metrics refresh and the `reconcile_jobs` CLI now connect as `app_coordinator`. Workers bind `app.job_id` for a single-row bootstrap read, clear it, then bind the durable row's organisation before any protected read or write; the locking `FOR UPDATE` also runs under tenant context, because PostgreSQL applies the UPDATE policies to a locking read and the bootstrap deliberately has no UPDATE policy. Covered by the `test_rls_jobs_enablement_db.py` cross-organisation, worker-bootstrap, coordinator least-privilege and parent/tenant-consistency suite, plus migration reversibility. |
| 5 | identity and control plane (P4) | `organisation_memberships`, `membership_roles`, `invitations` | **Delivered.** Group 5. Production enablement migration `f1a2b3c4d5e6` installs the canonical organisation-isolation policy on the two organisation-owned tables, a **SELECT-only** pre-tenant user-keyed policy on `organisation_memberships`, the `membership_roles_parent_isolation` parent-existence **read** policy plus the `membership_roles_organisation_isolation` write policy (which requires the parent membership's durable organisation to equal the validated tenant), the invitee email-keyed `invitations_invitee_select`/`invitations_invitee_update` pair and the verified-webhook single-row `invitations_webhook_provider_select`/`invitations_webhook_provider_update` bootstrap, enables and forces RLS, and ships a reversible downgrade. The authenticated user is bound as transaction-local `app.user_id` before the pre-tenant membership/invitation lookups (ADR-0022 decision 8); every platform-plane operation binds exactly the organisation it targets after the platform permission dependency validated the caller (the per-organisation platform path, never a bypass); the cross-tenant teardown deletes read the user's memberships under the user-keyed policy and then delete per organisation; and runtime `UPDATE` on `invitations` is column-restricted to `status`/`updated_at`, so no invitee path can move an invitation's organisation, email or role. Covered by the `test_rls_identity_enablement_db.py` cross-organisation, pre-tenant-lookup, pre-tenant-write-denial, invitee, webhook, platform-binding, teardown, pool-reuse and migration-reversibility suite. |
| 6 | operational ledgers (P4) | `audit_events`, `outbox_events`, `maintenance_runs`, `webhook_events` | **Delivered.** Group 6. Production enablement migration `a2b3c4d5e6f7` installs the null-safe operational-ledger policies and the isolated `app_operator` credential, enables and forces RLS, and ships a reversible downgrade. A `NULL` tenant key never means "all rows": a tenant context reads only its own `audit_events`/`outbox_events` rows, while the cross-tenant and global audit history is reachable only through the explicit, validated transaction-local platform context (`app.platform_admin`) bound by the platform permission dependency after authorisation — never by exempting the table. The audit append is **tenant-checked** (own tenant, global null-tenant, or validated platform context) so a foreign-tenant attribution is denied even if a service predicate is missed. The coordinator (`app_coordinator`) reads the whole dispatch ledger, moves a dispatch through its lifecycle via **column-level** UPDATE grants limited to the claim/settle/release/recovery columns, and purges only published rows. `app_operator` is adopted only after membership normalisation in both directions, and the downgrade always revokes the migration's read grants. `outbox_events` still has no client read path. Covered by the `test_rls_operational_ledgers_enablement_db.py` cross-organisation, platform-context, coordinator-lifecycle, coordinator-column-denial, adversarial-adoption, global-ledger, pool-reuse and migration-reversibility suite. |
| 7 | platform-only plane (P4) | `platform_roles`, `platform_role_permissions`, `platform_memberships`, `bootstrap_states` | **Delivered.** Group 7. Production enablement migration `b4c5d6e7f8a9` installs the global catalogue read policies on `platform_roles`/`platform_role_permissions`, the **SELECT-only** pre-authorisation `platform_memberships_self_isolation` policy (a user reads only their own membership before any platform context exists, ADR-0022 decision 8), the cross-user `platform_memberships_platform_access` policy keyed to the validated transaction-local platform context (or the trusted service context below), and the context-gated `bootstrap_states_service_read`/`bootstrap_states_service_insert`/`bootstrap_states_service_delete` policies (no runtime-wide read and no UPDATE, and the inherited table-wide DML grant narrowed to the platform-plane need); it enables and forces RLS on all four tables and ships a reversible downgrade. The platform permission dependency rebinds `app.user_id` before resolving the caller's own membership and then binds `app.platform_admin`; the one-time bootstrap grant, the signature-verified `user.deleted` webhook and the operator recovery/teardown CLI bind the **separate** `app.platform_service` flag (never `app.platform_admin`, which also opens the cross-tenant audit read). Platform status alone grants no tenant-row access. Covered by the `test_rls_platform_plane_enablement_db.py` cross-user, pre-authorisation, write-denial, bootstrap-sentinel, service-path, pool-reuse and migration-reversibility suite. |

The machine-checked classification for every table is
`backend/tests/tenant_isolation_registry.py`; the human-readable detail is
`docs/rls-table-inventory.md`. Every table ends with either a tested policy or an
explicit reviewed exclusion recorded in the inventory. The plan-P4 indirect-row
strategies across groups — `job_attempts` (denormalised key and composite parent
FK), `notification_deliveries` (parent existence) and `membership_roles` (parent
read split from tenant-checked write) — are proven together by
`backend/tests/test_rls_indirect_rows_db.py` rather than only inside each group
suite.

The plan-P3 aggregate requirement that retries, leases, reconciliation and
outbox dispatch work **without a bypass role** is proven on the restricted
logins themselves by `backend/tests/test_rls_jobs_enablement_db.py`:
`test_worker_takes_over_an_expired_lease_under_enforced_rls` runs a claim and an
expired-lease takeover on the `app_runtime` worker login,
`test_transient_failure_retry_is_durable_under_enforced_rls` settles a transient
failure into a durable retry dispatch on `app_runtime`, runs the coordinator's
real publish cycle (`run_cycle`: claim, job-aggregate read, publish and
owner-checked settle) on the non-bypass `app_coordinator`, then has the worker
re-claim the published retry, and
`test_coordinator_reconciles_stranded_jobs_under_enforced_rls` recovers stranded
queued and lease-expired running jobs through the `app_coordinator` reconciliation
sweeps. All three run under the enforced `jobs`/`job_attempts`/`outbox_events`
policies, so the two restricted credentials are the evidence rather than the
owner-side lifecycle suites.

### 3.1 Group 4 split and the group-4b prerequisite

The plan's original group 4 ("jobs and organisation settings") was split during
implementation review into **4a** (this delivery: `organisation_features`,
`organisation_ai_settings`) and **4b** (`jobs`, `job_attempts`), because the two
halves have different prerequisites:

- **4a** is organisation-owned but platform-managed. Enabling its default-deny
  policy is safe once the platform-plane services bind exactly the organisation
  they target (an explicit per-organisation platform path, never a bypass —
  ADR-0022 decision 4) and the organisation-creation paths bind the new
  organisation before writing its default settings row. This migration ships
  both bindings.
- **4b** originally could not be enabled safely under the then-current ordering:
  ADR-0022 decision 3 requires the `app_coordinator` non-bypass role before the
  `jobs` group, but §1 of this document (and the plan's P4) had placed that role
  in a later work unit. Three consumers read `jobs`/`job_attempts` across
  tenants on the runtime role and would silently return no rows under the
  enforced policy: the outbox coordinator (`app/job_coordinator/loop.py`,
  `reconciliation.py`), the in-process reliability-metrics refresh
  (`app/main.py` → `app/observability/`), and the `scripts/reconcile_jobs.py`
  operator CLI. The group-3 "iterate `organisations` and bind each tenant"
  pattern does not fit these, because they must find and aggregate rows across
  tenants before a tenant is known. **Group 4b is now delivered as its own
   reviewed work unit**, bringing the `app_coordinator` role and its
   dispatch-state policies forward with it; all three consumers connect through
   `app_coordinator` and set no tenant context, satisfying coordinator policies
   scoped to dispatch state rather than a tenant. The coordinator's UPDATE
   authority is granted **per column** (settlement/reconciliation columns only)
   and its settle policies restrict the reachable post-update states, so the
   dispatch-scoped role remains least-privilege rather than table-wide-write.
- **4b worker bootstrap has no UPDATE policy.** `SELECT ... FOR UPDATE` is
  governed by both the SELECT and the UPDATE policies, so a permissive
  job-keyed bootstrap UPDATE policy would have authorised a real update of the
  named row (including a tenant-key move). The bootstrap is therefore
  `FOR SELECT` only: the worker reads the one row its `app.job_id` names, clears
  that setting, binds the durable row's `organisation_id`, and only then takes
  the row lock under tenant authority (ADR-0022 decision 3's "cleared before the
  tenant-context phase"). `job_attempts` additionally carries a composite
  `(job_id, organisation_id)` foreign key to `jobs (id, organisation_id)` so an
  attempt's copied tenant key can never reference a parent job in another
  organisation.
- **4b notification-exhaustion addendum.** When the coordinator's bounded
  recovery terminally fails an attempt, the registered `notification.email`
  exhaustion hook finalizes the delivery row. That hook binds the durable job's
  own organisation and recipient user before touching the user-private rows, so
  the group-4b migration grants `app_coordinator` DML on
  `notifications`/`notification_deliveries` (no new, tenant-broad policy; the
  existing context-gated user-private policies still apply) and no tenant
  payload is readable without binding that row's context. This narrow,
  human-reviewed consequence of the coordinator running the same settlement
  code is recorded in ADR-0022 and was approved 2026-09-19 with the group-4b
  tenant-isolation, database-role/grant/policy, worker-context and
  destructive-downgrade changes.

### 3.2 Group 5 design notes (identity and control plane)

Group 5 is the first group whose rows are read **before** any organisation
context exists, so the canonical organisation policy is supplemented rather
than replaced:

- **Pre-tenant membership lookup (ADR-0022 decision 8).** The authenticated
  user is bound as transaction-local `app.user_id` in `get_current_user` and
  before the membership lookup in `get_current_membership`. The
  `organisation_memberships_user_isolation` policy is **SELECT only**: a user
  can read their own memberships for `/me`, context resolution and the
  teardown, but can never insert, update or delete one. Every membership write
  is reached through an organisation context (the canonical policy) or the
  validated platform path.
- **Indirect role grants (ADR-0022 decision 6, parent strategy).** The plan
  allows a denormalised tenant key or the reviewed parent strategy;
  `membership_roles` has no key of its own, so it uses the parent strategy with
  **read visibility split from write authority**. The
  `membership_roles_parent_isolation` policy is `FOR SELECT` only: the parent
  `organisation_memberships` policies are themselves RLS-filtered, so a role
  grant is visible exactly when its parent membership is visible — an
  organisation context admits the organisation's grants, a pre-tenant user
  context admits only the user's own, and no context admits none. A separate
  `membership_roles_organisation_isolation` policy is `FOR ALL` and requires
  the parent membership's own `organisation_id` to equal the validated
  `app_current_tenant_id()`, so a pre-tenant user context (no organisation)
  can read its own grants but can never insert, update or delete one. This
  mirrors the group-2 `notification_deliveries_parent_isolation` read policy
  while adding the explicit write predicate the read-only parent check cannot
  provide.
- **Invitee invitation access.** Login-time linking resolves and accepts the
  invitee's own pending invitations with no organisation context, so
  `invitations_invitee_select` and `invitations_invitee_update` are keyed to the
  authenticated user's verified email via `app_current_user_email()`. Runtime
  `UPDATE` on `invitations` is **column-restricted** to `status`/`updated_at`,
  so the invitee policy (and every other runtime update) can only advance the
  status and can never rewrite the organisation, email or role of a row. The
  two policies are separate rather than one `FOR ALL` policy, so an invitee
  email can never authorise an insert.
- **Verified webhook bootstrap.** The `invitation.revoked` consumer is a
  signature-gated control-plane path with no tenant or user identity. It binds
  the verified event's own provider invitation id as transaction-local
  `app.invitation_provider_id`; `invitations_webhook_provider_select` and
  `invitations_webhook_provider_update` admit exactly the one row whose
  `workos_invitation_id` matches, for the read/lock and the status flip the
  operation actually performs. The bootstrap is deliberately not `FOR ALL`:
  binding a provider id must not create insert or delete authority (the same
  reason the group-4b job bootstrap is `FOR SELECT` only). This is the
  `app.job_id` single-row bootstrap pattern applied to a webhook, never a bypass
  or a cross-tenant scan.
- **Platform plane and teardown.** Every platform membership/invitation
  operation names exactly one organisation in the path and binds it after the
  platform permission dependency validated the caller (the group-4a
  per-organisation platform path). The cross-tenant `delete_provisioned_user`
  teardown does not need `app_operator`: it binds the target user to read their
  memberships under the user-keyed policy, then binds each membership's
  organisation before deleting the membership and its role grants.
- **Invitee lookup index (rollout principle 4).** The pre-tenant login lookup
  filters ``lower(email)`` with ``status = 'sent'``; the existing ``email``
  index cannot serve a ``lower()`` predicate and the pending-uniqueness index
  leads with ``organisation_id``, so the migration adds the partial functional
  index ``ix_invitations_lower_email``. The identity suite's representative
  ``EXPLAIN`` review proves the membership, ``/me`` and invitee lookups use
  their indexes with no sequential scan.
- **Error non-disclosure.** A foreign membership/invitation is hidden by the
  policies and is reported by the application as the same `404`/`403` a missing
  or unauthorised row already produces; no new tenant resource-detail surface
  is introduced.

### 3.3 Group 6 design notes (operational ledgers)

Group 6 protects the operational ledgers whose tenant key is **nullable** or
absent, so the canonical `organisation_id = app_current_tenant_id()` policy
alone would be wrong in both directions (it would hide global rows from the
platform and could not express the coordinator's cross-tenant dispatch scope):

- **`audit_events` (append-only, nullable tenant).** The runtime role gets an
  own-tenant SELECT policy, a **tenant-checked** INSERT policy, and no
  UPDATE/DELETE policy at all — on top of the append-only trigger, so the ledger
  stays append-only under RLS. The INSERT `WITH CHECK` admits only the writer's
  own validated tenant, a global (null-tenant) row, or any row under the
  validated platform context; a foreign-tenant attribution is a policy violation
  even if an application predicate is ever missed. Global and foreign rows are
  never visible to a tenant context. The platform audit screen
  (`GET /api/v1/platform/audit-events`) has no tenant filter, so after
  `require_platform_permission` validates the caller it binds the
  transaction-local `app.platform_admin` flag; the explicit
  `audit_events_platform_read` policy admits the cross-tenant and global rows
  **only** under that flag. This is ADR-0022 decision 4's reviewed platform
  path, not a table exemption: the flag is a boolean, is bound only after the
  permission check, and is referenced by no other table's policy. The
  coordinator's job-failed settlement appends audit rows with a separate
  `app_coordinator` INSERT policy (also tenant-checked, and the settlement binds
  the durable job's own organisation first); it has no audit read path.
- **`audit_events` INSERT and `INSERT ... RETURNING`.** An append-only ledger
  deliberately grants no SELECT to every writer, and PostgreSQL applies the
  SELECT policies to an `INSERT ... RETURNING` clause. The append-only timestamp
  columns therefore carry a Python-side default as well as the database default
  (`audit_events.created_at`; and for the same reason
  `outbox_events.available_at`/`created_at`, `maintenance_runs.created_at`,
  `webhook_events.received_at`), so SQLAlchemy no longer needs a `RETURNING`
  read of a row the writer may not be allowed to see. The schema default is
  retained for direct SQL and backfills. Accepted skew: `outbox_events.available_at`
  is therefore the application host's UTC clock rather than PostgreSQL's, and
  hosts/database are required to be NTP-synchronised (the claim query compares
  against the same application clock, bounding skew to sub-second).
- **`outbox_events` (nullable tenant).** Runtime may read its own tenant's rows
  and append either its own tenant's dispatch rows or the global null-tenant
  maintenance rows; it cannot read or mutate a foreign row and has no UPDATE or
  DELETE policy. `app_coordinator` reads the whole dispatch ledger (there is no
  client read path) and owns the lifecycle: an UPDATE policy whose `USING`
  covers `pending`/`publishing`/`published` (the retention sweep takes a
  `SELECT ... FOR UPDATE` lock, and PostgreSQL applies the UPDATE policy to a
  locking read) with a `WITH CHECK` confined to the four lifecycle states, a
  DELETE policy that admits only `published` rows, and — crucially — a
  **column-level** UPDATE grant limited to the claim/settle/release/recovery
  columns (`status`, `claimed_at`, `claim_token`, `attempt_count`,
  `processed_at`, `last_error`, `available_at`). The coordinator can move a
  dispatch through its states but can never rewrite its tenant key, event
  identity/contract, payload, aggregate reference or immutable timestamp.
- **`maintenance_runs` and `webhook_events` (no tenant key).** Both are global
  infrastructure with no tenant payload. RLS is enabled and admits only the
  roles that own the paths — the runtime maintenance worker and the coordinator
  for runs, the runtime webhook consumer for the dedup ledger — and denies
  every other role.
- **`app_operator` (reviewed operational credential, ADR-0022 decision 4).**
  The group migration also creates the isolated `app_operator` role
  (`NOLOGIN`, non-owner, member of no application role, `BYPASSRLS`) and grants
  it read access (tables and sequences: `pg_dump` reads sequence `last_value`)
  for backup/support tooling. `BYPASSRLS` is the deliberate,
  reviewed privilege whose scope is exactly the cross-tenant read a policy
  cannot express; the credential is resolved only by
  `DATABASE_OPERATOR_URL`/`app.db.session.resolve_operator_database_url`, is
  never loaded by an HTTP process or a worker, and `resolve_operator_database_url`
  refuses to fall back to the runtime or owner credential. Destructive restore
  remains an explicit, separately reviewed operation. Because the *runtime*
  role must stay non-bypass, the deployment check in §5 is extended to assert
  `app_operator` is the only `BYPASSRLS` application role and that `app_runtime`
  cannot `SET ROLE` it. A **deployment-provisioned** (pre-existing) role is
  adopted only after full normalisation: the migration forces the safe
  attributes, refuses a role that owns a table, and revokes pre-existing
  memberships in **both** directions — including a dangerous
  `app_runtime -> app_operator` grant that would let the ordinary role assume
  the bypass credential. The downgrade always revokes the migration-added
  `USAGE`/`SELECT` grants, and drops the role only when this migration created
  it, so an adopted credential keeps its own out-of-band attributes but never
  the migration's operational read surface.

### 3.4 Group 7 design notes (platform-only plane)

Group 7 protects the platform authorisation plane, which grants no tenant rows by
itself and is read at three distinct trust levels:

- **Pre-authorisation self read (ADR-0022 decision 8, platform-plane analogue).**
  ``require_platform_permission`` and ``/me`` resolve the caller's *own* platform
  memberships **before** any platform context can exist — the permission check is
  the authorisation itself. The authenticated user is bound as transaction-local
  ``app.user_id``, so ``platform_memberships_self_isolation`` is **SELECT only**
  and keyed to ``user_id``: a user reads their own membership and can never
  insert, update or delete one. A user-keyed insert is deliberately absent, so a
  user context cannot grant itself platform authority. ``platform_roles`` and
  ``platform_role_permissions`` are the global catalogue that lookup joins
  through and take a runtime read policy, exactly as the organisation
  ``roles``/``permissions`` catalogue is not tenant-scoped. Group 7 revokes the
  table-wide DML grant the earlier groups inherited on all four platform tables
  and re-grants only the catalogue ``SELECT`` (the runtime role has no write
  grant on either; the seed migration owns them).
- **Validated platform context (ADR-0022 decision 4).** After the permission
  check authorises the caller, ``require_platform_permission`` binds
  **``app.platform_admin``**. ``platform_memberships_platform_access`` (``FOR
  ALL`` with matching ``USING``/``WITH CHECK``) admits the cross-user
  list/grant/revoke. The platform tables carry no tenant rows, so this context
  grants no tenant-data access — proven by the group suite, which shows an
  ordinary tenant table stays default-denied under it.
- **Trusted service context for non-interactive control-plane paths.** Three
  paths need cross-user platform-table access with **no platform administrator
  present**: the one-time bootstrap grant (verified email, then sentinel
  read/insert), the signature-verified ``user.deleted`` webhook deactivation and
  the operator recovery/teardown CLI. They bind a **separate** transaction-local
  **``app.platform_service``** flag. It is deliberately *not* ``app.platform_admin``:
  that flag also opens the group-6 cross-tenant audit read, so reusing it would
  hand these paths audit access they do not need. ``app.platform_service`` is
  referenced only by the group-7 policies, is bound only by those trusted paths
  after their own validation (never from a request), and grants no tenant-row
  access.
- **``bootstrap_states``.** The sentinel row records the consuming
  administrator's verified email, user id and timestamp, so it is **not**
  readable runtime-wide. The bootstrap hook resolves the verified WorkOS
  profile first, then binds the trusted service context and reads the singleton
  to decide whether it is already consumed; the read, insert and delete are all
  gated to the validated platform/service context, so a tenant context can
  neither read nor claim nor clear the bootstrap. There is no UPDATE policy and
  no runtime UPDATE grant: the sentinel is immutable once consumed.
- **Error non-disclosure.** The platform plane has no tenant resource-detail
  surface, so group 7 claims no part of the aggregate P3 error-non-disclosure
  checkbox.

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
per-environment verification is repeated in `docs/operations.md`.

**Automated check (plan P4).** The catalogue verification above is now executed
by `app.db.role_checks`. A production process runs it from `create_app`'s
lifespan and refuses to serve traffic when the runtime credential owns a table,
carries `SUPERUSER`/`BYPASSRLS`/`CREATEDB`/`CREATEROLE` or inherits a privileged
role; the check never connects with the owner credential, so an environment that
points both URLs at the same role is rejected rather than silently accepted. The
coordinator credential is checked the same way when configured. The same check
is available before deployment:

```bash
make verify-db-roles
# or: cd backend && uv run python -m scripts.verify_db_roles
```

Group 6 adds the isolated operational credential, and it uses the same checks
with one deliberate difference — `app_operator` is the **only** application
role allowed to carry `BYPASSRLS`:

```sql
-- Connect with DATABASE_OPERATOR_URL and run:
SELECT current_user;   -- must be app_operator (distinct from both roles above)

SELECT rolname, rolsuper, rolbypassrls FROM pg_roles
WHERE rolname = 'app_operator';
-- app_operator must be rolsuper=false and rolbypassrls=true.

-- app_operator must own no table and have no membership in either direction:
SELECT count(*) FROM pg_tables
WHERE schemaname = 'public' AND tableowner = 'app_operator';  -- must be 0
SELECT count(*) FROM pg_auth_members m
JOIN pg_roles member ON member.oid = m.member
WHERE member.rolname = 'app_operator';  -- must be 0 (operator is a member of none)
SELECT count(*) FROM pg_auth_members m
JOIN pg_roles granted ON granted.oid = m.roleid
WHERE granted.rolname = 'app_operator';  -- must be 0 (no role is a member of operator)

-- And the runtime role must NOT be able to assume it:
--   connect with DATABASE_RUNTIME_URL and run: SET ROLE app_operator;
--   this must fail with "permission denied to set role".
```

Every use of `app_operator` is a privileged operational path: record who ran it,
what operation it performed and against which environment without placing row
contents or secrets in the audit event (`docs/operations.md` →
Operational database access). The automated check also asserts that
`app_operator` is the only application role carrying `BYPASSRLS`, owns no table
and has no membership in either direction, and that the runtime role cannot
`SET ROLE` it.

## 6. Execution contract and release bookkeeping

The rollout is executed by the active plan
`plans/2026-09-18-postgresql-row-level-security-plan.md` (checkpoints P3 and
P4); no separate scope is required to make progress. A versioned release scope
and immutable tag (anticipated as v0.9) are authored at release time, before
production-wide enablement, and rollout code and documentation adopt
version-prefixed citations from that point. P3 and P4 remain separately
reviewed work units, and no production policy is enabled as part of approving
the prototype.
