# Table Inventory and Access-Path Register

Status: P1 evidence for `plans/2026-09-18-postgresql-row-level-security-plan.md`
(work unit P1 — ADR, inventory and access-path design). Reviewed before the P2
prototype starts.

This register classifies every table the application owns and records how each
one is reached today. It is the human-readable companion to the machine-checked
registry in `backend/tests/tenant_isolation_registry.py`, which
`backend/tests/test_tenant_isolation_registry.py` enforces against
`Base.metadata`: a new model that does not declare an isolation strategy fails
that suite. This document adds what the registry does not carry — the legitimate
readers and writers, the application access paths, the raw/bulk/relationship
SQL, and the connection types that reach the database.

It is deliberately descriptive: it records the current application-only
enforcement so the P2 prototype and the P1 ADR
(`docs/decisions/0022-postgresql-row-level-security.md`) can reason about which
tables an RLS backstop would protect, in what order, and which control-plane
paths must be designed explicitly rather than bypassed.

Classification vocabulary (identical to `IsolationClass` in the registry):

| Class | Meaning |
| --- | --- |
| global | Deliberately outside the tenant boundary (identity, catalogue, tenant root). |
| organisation-owned | Carries `organisation_id`; every application query filters on it; a foreign row is a `404`. |
| user-private | Organisation-owned **and** recipient-scoped (`user_id`), so a second predicate narrows it further. |
| indirectly organisation-owned | No tenant key of its own; inherits the boundary from a parent row/table. |
| platform-only | Cross-tenant platform authorisation plane; never tenant data. |
| operational | Infrastructure ledgers (audit, outbox, maintenance, webhook dedup); tenant key may be nullable. |

## 1. Classification summary

Every table registered on `Base.metadata` appears exactly once. The tenant key
and parent path columns are empty where the class has no tenant relationship.

| Table | Class | Tenant key | Parent ownership path |
| --- | --- | --- | --- |
| `organisations` | global | — | — |
| `users` | global | — | — |
| `roles` | global | — | — |
| `permissions` | global | — | — |
| `role_permissions` | global | — | — |
| `organisation_memberships` | organisation-owned | `organisation_id` | — |
| `invitations` | organisation-owned | `organisation_id` | — |
| `records` | organisation-owned | `organisation_id` | — |
| `record_revisions` | organisation-owned | `organisation_id` | `records.record_id` |
| `files` | organisation-owned | `organisation_id` | — |
| `jobs` | organisation-owned | `organisation_id` | — |
| `notifications` | user-private | `organisation_id`, `user_id` | — |
| `organisation_features` | organisation-owned | `organisation_id` | — |
| `organisation_ai_settings` | organisation-owned | `organisation_id` | — |
| `ai_requests` | organisation-owned | `organisation_id` | — |
| `ai_outputs` | organisation-owned | `organisation_id` | `ai_requests.ai_request_id` (+ composite org FK) |
| `ai_attachment_references` | organisation-owned | `organisation_id` | — |
| `ai_scratch_uploads` | organisation-owned | `organisation_id` | — |
| `membership_roles` | indirectly organisation-owned | — | `organisation_memberships.membership_id` |
| `job_attempts` | indirectly organisation-owned | — | `jobs.job_id` |
| `notification_deliveries` | indirectly organisation-owned | — | `notifications.notification_id` |
| `audit_events` | operational | `organisation_id` (nullable) | — |
| `outbox_events` | operational | `organisation_id` (nullable) | — |
| `maintenance_runs` | operational | — | — |
| `webhook_events` | operational | — | — |
| `platform_roles` | platform-only | — | — |
| `platform_role_permissions` | platform-only | — | — |
| `platform_memberships` | platform-only | — | — |
| `bootstrap_states` | platform-only | — | — |

## 2. Inventory detail

Readers and writers name the module/service that legitimately touches the table;
the access path names the HTTP route or internal process. "Internal" means the
row is never returned to a client by that path.

### 2.1 Global — identity and catalogue

| Table | Legitimate readers | Legitimate writers | Application access paths |
| --- | --- | --- | --- |
| `organisations` | `platform_admin.service` (list/detail/create/update); `users.service` (`/me` membership names); `invitations.service` (send); `permissions`/platform membership resolution | `organisations.service.create_organisation`; `platform_admin.service` (create/update) | `POST /api/v1/organisations`; `POST /api/v1/platform/organisations`; `GET /api/v1/platform/organisations`; `GET/PATCH /api/v1/platform/organisations/{id}`; `GET /api/v1/me` |
| `users` | auth dependency (`get_current_user`); `users.service`; `platform_admin.service` (list/teardown); `webhooks.service` | `users.service.get_or_provision_user`; `webhooks.service`; bootstrap provisioning/recovery scripts | Every authenticated request (provisioning); `GET /api/v1/me`; `GET /api/v1/platform/users`; `POST /api/v1/webhooks/workos` |
| `roles` | permission resolution; `invitations.service`; `platform_admin.queries` | Seed migrations only | Internal permission/role resolution on protected requests |
| `permissions` | permission resolution; `platform_admin.queries` | Seed migrations only | Internal permission resolution |
| `role_permissions` | permission resolution | Seed migrations only | Internal permission resolution |

The platform-only tables are detailed under §2.5, not here, so the
human-readable grouping matches the `platform-only` classification.

`teams`/`team_memberships` from blueprint §9 are not part of the current schema
(no model is registered), so they are absent here rather than unclassified.

### 2.2 Organisation-owned — direct tenant data

| Table | Legitimate readers | Legitimate writers | Application access paths |
| --- | --- | --- | --- |
| `organisation_memberships` | `get_current_membership` (context resolution); `users.service` (`/me`); `platform_admin.service` (membership list) | `organisations.service` (creator owner); login-time invitation linking; `webhooks.service`; `platform_admin.service` (role/status/remove) | Every org-scoped request (context); `GET /api/v1/me`; `GET /api/v1/platform/organisations/{id}/memberships`; role/status/remove routes |
| `invitations` | `invitations.service`; `webhooks.service`; login-time linking | `invitations.service` (send/accept/revoke); `webhooks.service` | `POST/GET/DELETE /api/v1/platform/organisations/{id}/invitations`; login linking; `POST /api/v1/webhooks/workos` |
| `records` | `records.service` | `records.service` | `GET/POST /api/v1/records`; `GET/PATCH/DELETE /api/v1/records/{id}` |
| `record_revisions` | `records.service` (reconstruction); test-only history reads | `records.service._append_revision` on create/update/delete | Internal append-only ledger; no API route currently exposes it |
| `files` | `files.service`; `files.tasks`; AI source authority | `files.service` (intent/complete/delete); `files.tasks` (promotion) | `POST /api/v1/files`; `POST /api/v1/files/{id}/complete`; `GET /api/v1/files`; `GET /api/v1/files/{id}`; `GET /api/v1/files/{id}/download-url`; `DELETE /api/v1/files/{id}` |
| `jobs` | `jobs.service`; `job_coordinator`; observability metrics | `jobs.service` (create); task/coordinator ownership settlement | `GET /api/v1/jobs`; `GET /api/v1/jobs/{id}`; internal dispatch/reconciliation |
| `notifications` | `notifications.service`; user-scoped queries | `notifications.service`; `notifications.tasks` | `GET /api/v1/notifications`; `GET /api/v1/notifications/unread-count`; `PATCH .../{id}/read`; `PATCH .../read-all`; `POST .../test` |
| `organisation_features` | `feature_flags.service` | `feature_flags.service` (platform route) | `GET /api/v1/platform/feature-flags`; `PUT /api/v1/platform/feature-flags/{key}` |
| `organisation_ai_settings` | AI persistence/service; platform AI settings | `ai.persistence.service` (platform route) | `GET/PUT /api/v1/platform/organisations/{id}/ai-settings` |
| `ai_requests` | AI service/execution/persistence; AI demo route | `ai.service`; `ai.execution` | `POST /api/v1/ai/classify`; `POST /api/v1/ai/ask`; `GET /api/v1/ai/classify/requests/{id}` |
| `ai_outputs` | AI persistence/service | `ai.service`/`ai.execution` | Same AI routes (result surface) |
| `ai_attachment_references` | AI persistence/reconciliation; retention maintenance | `ai.persistence.references`; staging adapters | Internal AI transfer/reconcile paths |
| `ai_scratch_uploads` | `ai.scratch` | `ai.scratch` | `POST /api/v1/ai/scratch/uploads`; `POST /api/v1/ai/scratch/uploads/{id}/complete` |

### 2.3 Indirectly organisation-owned

| Table | Parent path | Legitimate readers | Legitimate writers | Application access paths |
| --- | --- | --- | --- | --- |
| `membership_roles` | `organisation_memberships` | permission resolution; `platform_admin.service` role lists; `users.service` roles | `permissions.service` (assign/remove); `platform_admin.service` (grant/revoke); login linking; org creation | Internal on protected requests; `POST/DELETE /api/v1/platform/organisations/{id}/memberships/{id}/roles[/{code}]` |
| `job_attempts` | `jobs` | `jobs.service` (history); observability metrics; coordinator | `jobs.execution`; `job_coordinator` | Internal attempt ledger; no client route |
| `notification_deliveries` | `notifications` | `notifications.service`; observability metrics | `notifications.tasks` | Internal delivery ledger; no client route |

### 2.4 Operational

| Table | Tenant key | Legitimate readers | Legitimate writers | Application access paths |
| --- | --- | --- | --- | --- |
| `audit_events` | `organisation_id` nullable | `audit.service`; platform audit route | `audit.service` (append-only) | `GET /api/v1/platform/audit-events`; appended by every service |
| `outbox_events` | `organisation_id` nullable | `job_coordinator`; observability metrics | services (enqueue); coordinator (claim/complete); reconciliation (purge) | Internal only; never read by a client |
| `maintenance_runs` | — | `job_coordinator`; observability metrics | `maintenance.service`; coordinator | Internal only |
| `webhook_events` | — | `webhooks.service` (dedup lookup) | `webhooks.service` (insert) | `POST /api/v1/webhooks/workos` (internal dedup only) |

### 2.5 Platform-only authorisation plane

| Table | Legitimate readers | Legitimate writers | Application access paths |
| --- | --- | --- | --- |
| `platform_roles` | `platform_admin.queries` | Seed migration only | Internal platform permission resolution |
| `platform_role_permissions` | `platform_admin.queries` | Seed migration only | Internal platform permission resolution |
| `platform_memberships` | `platform_admin.queries`; bootstrap recovery | bootstrap grant; `platform_admin.service` (grant/revoke); recovery script | `GET/POST/DELETE /api/v1/platform/admins`; `/me`; login-time bootstrap |
| `bootstrap_states` | bootstrap grant hook; provision/recovery scripts | bootstrap grant; provision/recovery scripts | `scripts/provision_bootstrap_admin.py`; `scripts/recover_platform_admin.py`; login-time bootstrap |

## 3. Raw SQL, bulk mutations and relationship loads

These are the places where a query does not go through a simple, obviously
org-scoped `select()`; each one is a candidate for the P2/P3 review even though
none of them is currently a cross-tenant read.

### 3.1 Raw SQL

| Location | Statement | Tables touched | Assessment |
| --- | --- | --- | --- |
| `app/api/health.py` | `SELECT 1` | none | Readiness probe; no tenant data. |
| `app/modules/maintenance/execution.py` | `pg_try_advisory_lock` / `pg_advisory_unlock` | none | Session/transaction advisory locks; no row data. |
| `app/modules/platform_admin/queries.py` | `pg_advisory_xact_lock` | none | Serialises concurrent bootstrap grants; no row data. |

### 3.2 Bulk updates and deletes

| Location | Operation | Tables touched | Assessment |
| --- | --- | --- | --- |
| `app/modules/notifications/queries.py` | `update(Notification)` mark-all-read | `notifications` | Bulk update by `organisation_id` + `user_id`; does not change the tenant keys, so `WITH CHECK` would hold. |
| `app/job_coordinator/loop.py` | `update(OutboxEvent)` claim/complete | `outbox_events` | Operational ledger transitions; no tenant key change. |
| `app/job_coordinator/reconciliation.py` | `delete(OutboxEvent).where(id.in_(...))` | `outbox_events` | Purges terminal rows by primary key. |
| `app/modules/platform_admin/service.py` (bootstrap teardown) | `delete(MembershipRole).where(MembershipRole.membership_id.in_(membership_ids))` | `membership_roles` | **Cross-tenant control-plane delete with no organisation predicate.** The ids are the teardown user's memberships, resolved across every organisation. Under RLS the runtime role is denied unless the platform/operational path permits it — see assessment below. |
| `app/modules/platform_admin/service.py` (bootstrap teardown) | `delete(OrganisationMembership).where(OrganisationMembership.user_id == user.id)` | `organisation_memberships` | **Cross-tenant control-plane delete with no organisation predicate.** Removes all of the user's memberships in one statement. Same policy implication as the row above. |
| `app/modules/permissions/service.py` | `session.delete(link)` | `membership_roles` | Single-row ORM delete reached through an already-resolved membership; no cross-tenant reach. |

**Policy implication of the platform teardown deletes.** `delete_user` in
`app/modules/platform_admin/service.py` intentionally removes a user's role
grants and memberships across every organisation; it is a control-plane
operation, not a tenant operation, and it cannot be expressed as an
`organisation_id` predicate. The intended RLS answer is ADR-0022 decision 4: the
statement runs under the validated platform context (or the isolated
`app_operator` credential), whose policy permits the cross-tenant delete, while
the ordinary runtime role is denied. The P2 prototype must not "fix" this by
granting `app_runtime` a bypass; a table-group rollout (P4) that protects
`organisation_memberships`/`membership_roles` must first ship the platform
context or route the teardown through `app_operator`, and prove it with a
cross-tenant test.

### 3.3 Relationship loads

| Relationship | Tables | Assessment |
| --- | --- | --- |
| `User.memberships` / `Organisation.memberships` (`cascade="all, delete-orphan"`) | `organisation_memberships` | Lazy/default load from the global `users`/`organisations` side. RLS would filter the child rows; the cascade delete is only reachable from the platform/service paths that already own the parent. |
| `OrganisationMembership.user` / `.organisation` | `users`, `organisations` | Load from a tenant-bound membership to global rows; carries no cross-tenant data. |

No `joinedload`/`selectinload` eager loads exist today; relationship traversal
is limited to the four above.

### 3.4 Row locks

`with_for_update()` serialises concurrent mutation. PostgreSQL applies RLS
`USING` to a locking read, so a `FOR UPDATE` that sees no row under the policy
lock cannot mutate a foreign row. This is the complete current list, grouped by
locked table:

| Table | Locations |
| --- | --- |
| `records` | `app/modules/records/queries.py` |
| `files` | `app/modules/files/service.py` |
| `jobs` | `app/modules/jobs/service.py` (attempt claim and dead-event settlement), `app/job_coordinator/reconciliation.py` (`skip_locked`) |
| `invitations` | `app/modules/invitations/queries.py`, `app/modules/invitations/service.py`, `app/modules/webhooks/service.py`, `app/modules/platform_admin/service.py` |
| `organisation_ai_settings` | `app/ai/persistence/queries.py` |
| `ai_attachment_references` | `app/ai/persistence/references.py` |
| `ai_scratch_uploads` | `app/ai/persistence/service.py` |
| `outbox_events` | `app/job_coordinator/loop.py`, `app/job_coordinator/reconciliation.py` |
| `maintenance_runs` | `app/modules/maintenance/service.py`, `app/job_coordinator/reconciliation.py` |
| `users` | `app/modules/platform_admin/service.py` (email and id lookup), `app/modules/webhooks/service.py` |

No locking reads exist on `record_revisions`, `job_attempts`,
`organisation_memberships`, `notifications` or `notification_deliveries`.

## 4. Connection and access-path register

Every process or tool that opens a database connection, the credential it uses
today, and the difference between the context it establishes **today** and the
PostgreSQL transaction-local context P2 proposes.

**Baseline fact (P2 starting point).** No PostgreSQL transaction-local context
is set anywhere today. `app/db/session.py` creates an ordinary engine and
session factory with no RLS GUC hook, and `app/api/dependencies.py:219` calls
`bind_identity_context`, which only binds structlog logging fields (plus
`request.state`) — it does not issue `set_config`. The API's "context" is the
validated membership it already holds in Python; the worker's is the durable row
it looks up. The "Proposed DB context" column is a design target for P2, not a
statement about current behaviour.

| Connection | Entry point | Credential today | Current context today | Proposed P2+ DB context | Notes |
| --- | --- | --- | --- | --- | --- |
| API | `app.main:app` (uvicorn) | `DATABASE_URL` (`app` role) | Application: validated `X-Org-Id` + active membership held in Python; structlog identity fields. **No DB GUC.** | `set_config('app.user_id', …, true)` after auth; `app.organisation_id` after membership validation | All tenant routes; also the metrics refresh loop. |
| Dramatiq worker | `dramatiq app.workers` | `DATABASE_URL` | Application: broker-supplied `job_id` is looked up as a durable row; no org context until the row is read. **No DB GUC.** | One-row `app.job_id` bootstrap read, then `app.organisation_id` from the validated row | Files, notifications, jobs, AI execution/persistence. See ADR-0022 decision 3 for the bootstrap. |
| Outbox coordinator | `python -m app.job_coordinator` | `DATABASE_URL` | Application: reads due/claimed global ledger rows; no tenant context. **No DB GUC.** | Dedicated non-bypass coordinator role/policies scoped to due/unclaimed ledger rows (ADR-0022 decision 3) | Outbox, jobs, maintenance runs. |
| Metrics refresh | inside the API process (`app.main`) | `DATABASE_URL` | Application: aggregate counts only. **No DB GUC.** | Unchanged (aggregate operational reads) | Reads `outbox_events`, `jobs`, `job_attempts`, `notification_deliveries`. |
| WorkOS webhook | `POST /api/v1/webhooks/workos` (API) | `DATABASE_URL` | Application: signature-verified identity only. **No DB GUC.** | Unchanged: identity/operational writes, no tenant context | Signature-gated; `webhook_events`, `users`, memberships, invitations. |
| Platform admin plane | `/api/v1/platform/*` (API) | `DATABASE_URL` | Application: platform permission resolution; **no DB GUC** | Validated platform context (P4 design), never `X-Org-Id` | Cross-tenant reads/writes. See ADR-0022 decision 4. |
| Authentication / bootstrap | login path (API) | `DATABASE_URL` | Application: provider session, then `user_id`; no org yet. **No DB GUC.** | `app.user_id` before membership lookup; `app.organisation_id` after | `users`, `organisation_memberships`, `bootstrap_states`. |
| Alembic migrations | `uv run alembic upgrade head` | `DATABASE_URL` (**same `app` role as runtime today**) | None | `app_owner` / separate migration credential; no tenant context | DDL plus seed data. **Gap: migration and runtime share one role; P2 must separate them.** |
| Support/recovery CLI | `scripts/provision_bootstrap_admin.py`, `scripts/recover_platform_admin.py`, `scripts/reconcile_jobs.py` | `DATABASE_URL` | Application: derived per operation; no tenant context | `app_operator` where the operation is cross-tenant (P4) | Operator tooling; audited for the platform recovery grant. |
| Backup / restore | managed provider PITR; `pg_dump -Fc` / `pg_restore --role=app` | provider-native or the `app` role | None | `app_operator`; documented RLS/`BYPASSRLS` handling | **Gap: no separate restore role and no documented handling on restore.** |
| Test suite (real DB) | `backend/tests/*_db.py` | `app` role on `app_template_test` | Seeded two-organisation world; no DB GUC | Prototype tests set context explicitly | Includes the mandatory isolation matrix. |
| Local infra check | `scripts/check_dev_infra.py` | none (Redis only) | n/a | n/a | Does not open a database connection. |

There is no cron/daemon database connection outside the coordinator; scheduled
maintenance is represented by `maintenance_runs` and claimed by the coordinator.

## 5. Gaps this register records for P2+

1. **Shared migration/runtime role.** Alembic and every runtime process use the
   same `DATABASE_URL`. P2 must add a non-owner, non-`BYPASSRLS` runtime role and
   a separate schema-owner/migration credential.
2. **No normalised tenant key on indirect tables.** `job_attempts`,
   `notification_deliveries` and `membership_roles` have no `organisation_id`,
   so they cannot take a generic tenant policy without a schema change or a
   parent-existence policy (see ADR-0022 decision 6).
3. **Nullable-tenant operational rows.** `audit_events` and `outbox_events`
   carry a nullable `organisation_id`; null must never be read as "all rows"
   (see ADR-0022 decision 7).
4. **Pre-tenant membership lookup.** Context is established by reading
   `organisation_memberships` before an organisation is known; this bootstrap
   read needs a user-keyed policy rather than an exemption (see ADR-0022
   decision 8).
5. **Platform plane.** The in-app platform administration routes are subject to
   the runtime role; their cross-tenant policy design is deferred to P4, not
   solved by granting the runtime role a bypass.
