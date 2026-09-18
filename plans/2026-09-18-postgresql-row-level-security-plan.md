# PostgreSQL Row-Level Security Evaluation and Rollout Plan

Status: Proposed (plan only; no implementation started)

Relates to: `Internal_Custom_Application_Starter_Architecture_v2.md` BP
§§8–13, §§28–31 and §§37–39; `SECURITY.md`; `docs/operations.md`;
`docs/backup-and-recovery.md`; and `AGENTS.md`.

## 1. Goal

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

## 2. Fixed boundaries

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

## 3. Current scope to classify

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

## 4. Governance and delivery rules

RLS affects tenant isolation, permissions, migrations, database roles and
operations. Human review is required before changes are applied.

- [ ] Assign the work to an applicable versioned scope and use version-prefixed
      citations in new code and documentation.
- [ ] Deliver each work unit through `implement -> review -> apply-and-commit`.
- [ ] Keep migrations additive and reversible during the prototype and staged
      rollout.
- [ ] Separate prototype migrations from any production enablement migration.
- [ ] Define rollback and mixed-version behaviour before enabling enforcement.
- [ ] Run policy and connection-pool tests against real PostgreSQL; mocked
      session tests are not sufficient evidence.
- [ ] Do not apply a production-wide policy rollout as part of approving the
      prototype.

## 5. Work unit R1 — ADR, inventory and access-path design

### Inventory

- [ ] Create a checked-in table inventory classifying every table as global,
      organisation-owned, user-private, indirectly organisation-owned,
      platform-only or operational.
- [ ] Record the tenant key, parent ownership path, legitimate readers/writers
      and current application access paths for each non-global table.
- [ ] Identify raw SQL, bulk updates/deletes and relationship loads that touch
      classified tables.
- [ ] Identify API, Dramatiq, scheduler, webhook, platform, migration, support,
      backup and recovery connections.

### ADR decisions

- [ ] Compare continued application-only enforcement with RLS defence in depth.
- [ ] Define separate database roles for schema ownership/migrations and normal
      application runtime.
- [ ] Decide whether workers share the restricted runtime role or use a second
      non-bypass role with equally explicit context.
- [ ] Define the narrow platform/maintenance access mechanism. Reject an
      implicit or request-selectable universal bypass.
- [ ] Decide whether user-private policies also require a transaction-local
      user ID.
- [ ] Decide how indirectly owned tables are protected: copied tenant key,
      parent-existence policy, restricted parent-only access, or documented
      exclusion.
- [ ] Define treatment for nullable-tenant audit/outbox rows and global events.
- [ ] Define how authentication and membership lookup occur before trusted
      tenant context exists.
- [ ] Record the threat model, operational cost, residual risks and objective
      prototype success criteria.

R1 completion evidence:

- [ ] Every current table and connection type appears in the inventory.
- [ ] The ADR contains no unresolved universal-bypass or pre-authentication
      context assumption.
- [ ] Human review approves proceeding to the prototype.

## 6. Work unit R2 — `records` prototype

Expected engineering effort: approximately 2–4 days, excluding review.

### Database roles and policy

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

### Context propagation

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

### Prototype tests

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

R2 completion evidence:

- [ ] Real-PostgreSQL tests demonstrate default denial and safe pool reuse.
- [ ] Migration upgrade, downgrade and re-upgrade pass.
- [ ] Existing records API behaviour and security tests remain green.
- [ ] The prototype records its performance and operational findings.

## 7. Adoption gate

After R2, update the ADR with one reviewed decision.

If RLS is deferred or rejected:

- [ ] Record the specific complexity or risk that prevents adoption.
- [ ] Record the residual risk of a missed application query predicate.
- [ ] Remove or disable prototype-only production configuration cleanly.
- [ ] Retain the real-database cross-organisation test suite as a release gate.
- [ ] Close this plan without starting R3 or R4.

If RLS is adopted:

- [ ] Approve a table-group rollout order and rollback procedure.
- [ ] Confirm deployment environments can provide separate migration and
      runtime roles.
- [ ] Proceed to R3 and R4 as separately reviewed work units.

## 8. Work unit R3 — Direct tenant-data rollout

Expected engineering effort after a successful prototype: approximately 5–8
days for core user-facing data, excluding review.

- [ ] Roll out policies in bounded migrations, beginning with files,
      notifications and AI data, then jobs and organisation settings.
- [ ] Add both read/write policies and cross-organisation tests for every table
      group before enabling enforcement.
- [ ] Require user context as well as organisation context for user-private
      notification rows where appropriate.
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

R3 completion evidence:

- [ ] Every enabled table has real select/insert/update/delete policy tests.
- [ ] API, worker and generated-capability isolation tests remain green.
- [ ] Rollback is demonstrated for every deployed table group.

## 9. Work unit R4 — Identity, control-plane and indirect tables

Expected engineering effort: approximately 3–7 days, depending on the R1
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

R4 completion evidence:

- [ ] The table inventory records a final policy or reviewed exclusion for
      every table.
- [ ] No normal API or worker path uses owner, superuser or `BYPASSRLS`
      credentials.
- [ ] Platform and recovery procedures work without creating a hidden tenant
      bypass in the ordinary application.

## 10. Release verification and documentation

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

## 11. Overall acceptance criteria

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

## 12. Recommended sequence and expected size

```text
R1 inventory and ADR
        |
        v
R2 records prototype (2–4 days)
        |
        v
 adoption gate -----> defer and record residual risk
        |
        v
R3 direct tenant data (5–8 days)
        |
        v
R4 control/indirect tables (3–7 days)
        |
        v
release verification and documentation
```

A safe comprehensive implementation is expected to require roughly 10–15
engineering days plus human review and deployment coordination. The prototype
is intentionally valuable on its own: it must be possible to stop after R2
without implicitly committing the project to a full rollout.
