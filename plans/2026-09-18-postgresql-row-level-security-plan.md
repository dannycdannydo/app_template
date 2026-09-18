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

- [ ] Add a dedicated non-owner, non-superuser, non-`BYPASSRLS` prototype
      runtime role.
- [ ] Ensure migration/schema-owner credentials are not used by the normal
      application path.
- [ ] Add reversible migrations enabling and forcing RLS on `records` and
      `record_revisions`.
- [ ] Use default-deny policies based on transaction-local organisation
      context.
- [ ] Make absent, empty or malformed context return no tenant rows or fail
      safely; it must never mean unrestricted access.
- [ ] Apply equivalent `USING` and `WITH CHECK` constraints so inserts and
      updates cannot create or move rows into another organisation.

Context propagation:

- [ ] Set context only after the active membership and permission path has
      validated the selected organisation.
- [ ] Set context with a parameterised transaction-local operation; do not
      interpolate request values into SQL.
- [ ] Ensure the context and protected query execute in the same transaction.
- [ ] Clear context automatically on commit and rollback.
- [ ] Keep platform, health, authentication and public routes functional
      without fabricating tenant context.
- [ ] Keep existing application-level `organisation_id` predicates and
      foreign-resource `404` behaviour.

Prototype tests:

- [ ] Prove organisation A can access its rows and cannot select, insert,
      update or delete organisation B rows.
- [ ] Prove an unscoped query still returns only the authorised organisation's
      rows.
- [ ] Prove missing context cannot access either organisation.
- [ ] Prove `WITH CHECK` rejects a mismatched insert and tenant-key update.
- [ ] Prove context cannot survive commit, rollback, exception, cancellation,
      timeout or pooled-connection reuse.
- [ ] Prove a user who is owner in A and viewer in B receives the correct
      context and application permissions in each request.
- [ ] Prove ordinary runtime credentials cannot disable policies, alter the
      schema or assume the owner role.
- [ ] Measure query plans and latency for representative list/detail/write
      operations.

P2 completion evidence:

- [ ] Real-PostgreSQL tests demonstrate default denial and safe pool reuse.
- [ ] Migration upgrade, downgrade and re-upgrade pass.
- [ ] Existing records API behaviour and security tests remain green.
- [ ] The prototype records its performance and operational findings.

Adoption gate (one reviewed decision after P2):

If RLS is deferred or rejected:

- [ ] Record the specific complexity or risk that prevents adoption.
- [ ] Record the residual risk of a missed application query predicate.
- [ ] Remove or disable prototype-only production configuration cleanly.
- [ ] Retain the real-database cross-organisation test suite as a release gate.
- [ ] Close this plan without starting P3 or P4.

If RLS is adopted:

- [ ] Approve a table-group rollout order and rollback procedure.
- [ ] Confirm deployment environments can provide separate migration and
      runtime roles.
- [ ] Proceed to P3 and P4 as separately reviewed work units.

### P3 — Direct tenant-data rollout

Dependencies: P2 and an adopted decision at the gate.

Human review required before application: tenant isolation, migrations and
worker context.

Expected engineering effort after a successful prototype: approximately 5–8
days for core user-facing data, excluding review.

- [ ] Roll out policies in bounded migrations, beginning with files,
      notifications and AI data, then jobs and organisation settings.
- [ ] Add both read/write policies and cross-organisation tests for every table
      group before enabling enforcement.
- [ ] Require user context as well as organisation context for user-private
      notification rows.
- [ ] Ensure signed downloads and AI attachment resolution remain scoped by
      the protected database row.
- [ ] Ensure workers derive organisation context from validated durable rows,
      not broker arguments alone.
- [ ] Prove retries, leases, reconciliation and outbox dispatch work without a
      bypass role.
- [ ] Verify application errors do not disclose whether RLS hid a foreign row.
- [ ] Check representative query plans and indexes after each table group.
- [ ] Stop the rollout if a table requires an unexplained bypass; return it to
      design review instead.

P3 completion evidence:

- [ ] Every enabled table has real select/insert/update/delete policy tests.
- [ ] API, worker and generated-capability isolation tests remain green.
- [ ] Rollback is demonstrated for every deployed table group.

### P4 — Identity, control-plane and indirect tables

Dependencies: P3.

Human review required before application: identity, permission-model,
control-plane and backup/recovery changes.

Expected engineering effort: approximately 3–7 days, depending on the P1
classification and whether schema changes are required.

- [ ] Implement the approved pre-tenant membership lookup without accepting
      arbitrary tenant context.
- [ ] Protect membership, invitation and membership-role access according to
      their identity/control-plane classification.
- [ ] Protect job attempts, notification deliveries and other indirect rows
      using the approved parent or denormalised-key strategy.
- [ ] Handle global versus tenant audit/outbox events without treating a null
      tenant key as unrestricted access.
- [ ] Give platform operations an explicit, narrowly scoped path and test that
      platform status alone cannot read ordinary tenant data.
- [ ] Document and test migration, support, backup, restore and emergency
      access roles.
- [ ] Audit use of any privileged operational path without placing secrets or
      row contents in the audit event.
- [ ] Add startup or deployment checks proving runtime roles do not own
      protected tables and lack `BYPASSRLS`.

P4 completion evidence:

- [ ] The table inventory records a final policy or reviewed exclusion for
      every table.
- [ ] No normal API or worker path uses owner, superuser or `BYPASSRLS`
      credentials.
- [ ] Platform and recovery procedures work without creating a hidden tenant
      bypass in the ordinary application.

## Reference map

| Checkpoint | Governing sources | What to extract |
| --- | --- | --- |
| P1 | BP §§8–13, §§28–31 and §§37–39; `SECURITY.md`; `docs/operations.md`; `docs/backup-and-recovery.md`; `AGENTS.md`; `docs/rls-table-inventory.md`; `backend/tests/tenant_isolation_registry.py`; ORM models under `backend/app/modules/` and `backend/app/ai/persistence/` | Table classification, tenant keys and ownership paths, readers/writers and access paths, raw/bulk/relationship SQL, connection types, role and control-plane design, threat model and prototype criteria |
| P2 | BP §§9–11, §30 and §31; `docs/decisions/0022-postgresql-row-level-security.md`; `backend/app/db/session.py`; `backend/app/api/dependencies.py`; `backend/tests/test_org_isolation_matrix_db.py`; `backend/alembic/` | Separate runtime/owner roles, transaction-local context propagation, default-deny `USING`/`WITH CHECK` policies, pool-reuse safety, real-PostgreSQL tests, migration reversibility and performance measurement |
| P3 | BP §§9, 11, §§17–20 and §§28–31; `docs/decisions/0022-postgresql-row-level-security.md`; `backend/app/modules/` services and `queries.py`; `backend/tests/org_isolation_helpers.py` | Bounded table-group rollout, user-private policies, worker context from durable rows, outbox/retry/reconciliation without bypass, index and plan review, error non-disclosure |
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
      is assigned at the adoption gate before any production enablement. New
      code and documentation use version-prefixed citations from the point the
      release scope exists.
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
