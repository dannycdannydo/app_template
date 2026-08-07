# Template v0.4 full senior review

**Review date:** 2026-08-07  
**Review target:** `main` at `d7d57fd` (v0.4 release)  
**Decision:** **Do not treat v0.4 as release-ready until the MAJOR findings are resolved and independently re-reviewed.**

## Executive assessment

The implementation has a strong foundation. In particular, it keeps the platform authorisation plane separate from organisation authorisation, puts provider access behind adapters, keeps routers generally thin, uses explicit response schemas, and has unusually thorough route-matrix and lifecycle tests. The intent of the v0.4 scope is reflected consistently in the code and in the documentation.

However, the release has one material scope and operational-security gap: after the one-time bootstrap, there is no supported mechanism to add or remove platform administrators. The release therefore cannot be administered safely over its lifetime, and it does not satisfy the scope's explicit platform-admin grant/revoke audit requirement. There are also meaningful consistency risks at the WorkOS/database boundary and an invitation-creation race that the database does not prevent.

### Severity summary

| Severity | Count | Meaning |
| --- | ---: | --- |
| Critical | 0 | Immediate exploitable compromise or data loss found |
| Major | 2 | Blocks release approval or leaves an important security/control invariant incomplete |
| Moderate | 4 | Must be planned and fixed before scale or broader production use |
| Minor | 2 | Quality, maintainability, or future-safety improvement |

## What was reviewed

The review covered the complete v0.4 change set, from `9b4ae8b` through `d7d57fd` (130 files, approximately 16,400 added lines), against:

- `TEMPLATE_V0_4_SCOPE.md`, especially acceptance criteria 1–11 and Scope §§6.1–6.10;
- `Internal_Custom_Application_Starter_Architecture_v2.md`, principally BP §§8–13, 27, 29–33 and 37;
- `AGENTS.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, `API_CONVENTIONS.md`, `SECURITY.md`, ADR-0013, and the existing module patterns;
- backend services, models, migrations, dependencies, WorkOS adapters, webhooks, tests, frontend query layer, router guards, views, and Playwright coverage.

This was a code and test review; it did not change product code.

## Findings

### MAJOR-01 — No supported platform-admin grant or revoke lifecycle

**Scope:** Scope acceptance criterion 1; Scope §§6.1, 6.2 and 6.10; BP §§9, 29 and 30.

`platform_memberships` and the seeded `platform_admin` role exist, and bootstrap can create the first membership. But there is no platform-membership administration service, API endpoint, UI action, or audit path for granting or revoking platform roles after bootstrap. The implemented membership routes only administer *organisation* memberships.

Evidence:

- [models.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/platform_admin/models.py) defines the platform tables, but no lifecycle service beyond `maybe_grant_bootstrap_platform_admin` exists.
- [router.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/platform_admin/router.py) exposes organisation and organisation-membership management only.
- The only platform-membership write in application code is the bootstrap insert in [service.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/platform_admin/service.py).

This leaves a single bootstrap administrator as a permanent operational dependency. If that person leaves, is compromised, or needs their privilege removed, recovery requires direct database intervention. It also prevents a controlled two-admin model and makes the documented audit trail incomplete.

**Required remediation:** add a narrowly scoped, platform-gated platform-role membership lifecycle: list current platform admins; grant `platform_admin`; revoke it; prevent removal of the final active platform admin (or define an explicit, audited break-glass recovery process); record `platform.admin_granted` and `platform.admin_revoked` events; add API/UI/security and concurrency tests. This is a permission-model change and requires the mandated human review before application.

### MAJOR-02 — Concurrent invite creation is not protected by a database invariant

**Scope:** Scope §6.5 and acceptance criterion 6; BP §§10–11.

`invite_user` checks for a pending invitation before calling WorkOS, but `invitations` has no partial unique index enforcing one `sent` invitation per normalised `(organisation_id, email)`. Two concurrent requests can both observe no pending row, both send remote WorkOS invitations, and both commit local rows.

Evidence:

- The check is application-level in [service.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/invitations/service.py).
- The migration [27f2b8d4a6c1_invitations.py](/home/daniel/Documents/Dev/app_template/backend/alembic/versions/27f2b8d4a6c1_invitations.py) only makes `workos_invitation_id` unique. It contains no unique/partial unique constraint for organisation, email, and pending state.

This violates the service's stated “one grantable invitation per email per org” invariant. It can produce duplicate delivery, duplicate acceptance audit events, and hard-to-explain invitation history. A pre-query is not a race guard.

**Required remediation:** add an Alembic migration with a PostgreSQL partial unique index on `(organisation_id, lower(email)) WHERE status = 'sent'`; catch the resulting integrity error and return the documented 409. Add a real-PostgreSQL concurrent invite test that verifies one local row and one provider send (use an idempotency key/outbox strategy if provider-side exactly-once delivery is required).

### MODERATE-01 — WorkOS side effects can succeed while the local transaction later fails

**Scope:** Scope §§6.3 and 6.5; BP §11; BP §30.

Platform organisation creation and invitation sending call WorkOS before the local transaction commits. If the subsequent audit insert, uniqueness check, database commit, process, or network path fails, WorkOS can contain an organisation or invitation that has no matching local durable state.

The organisation adapter documents an orphan-reconciliation procedure, but there is no equivalent reconciliation path for invitations, and neither path has an idempotency key, retry policy, compensation attempt, or durable delivery state.

Evidence:

- [platform_admin/service.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/platform_admin/service.py) creates the WorkOS organisation before `session.commit()`.
- [invitations/service.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/invitations/service.py) sends the WorkOS invitation before inserting/committing the local row.

This is not solved by database transactions: a transaction cannot roll back a completed third-party HTTP action. The most likely user effect is an email invitation that cannot be accepted because the application has no corresponding row; the operational effect is unreconciled provider state.

**Recommended remediation:** introduce a durable local intent/state model and idempotency keys before provider calls, then reconcile/retry safely. If a transactional outbox is deliberately deferred to v0.5, document the v0.4 bounded failure procedure for invitations as concretely as the organisation-orphan procedure, including how to identify, revoke, and re-create orphaned invitations. Add tests for a commit failure after a successful provider send.

### MODERATE-02 — Invitation linking misses users whose WorkOS email has changed

**Scope:** Scope §6.5 and acceptance criterion 6; BP §8.

Login-time linking first searches candidates using `user.email`, which is stored only during first provisioning. It then compares against a fresh WorkOS profile, but never refreshes the internal email. A verified user whose WorkOS email changes can be invited to their new email and will never find that invitation because the candidate query uses their old local email.

Evidence:

- [users/service.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/users/service.py) returns an existing user without refreshing profile fields.
- [invitations/service.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/invitations/service.py) explicitly acknowledges the stale-email assumption in `link_invitation_on_login`.

This is not a privilege escalation, but it breaks the promised authoritative login-time invitation flow for a normal identity-lifecycle event. The webhook handler deliberately ignores `user.updated`, so it does not close the gap.

**Recommended remediation:** centralise a validated-profile reconciliation step during authentication that updates mutable local profile fields (at least email and name) safely and audited where appropriate, or fetch the verified profile before the candidate invitation query. Add tests covering changed email, changed case, and an invitation to the new verified address.

### MODERATE-03 — Membership-list implementation has an avoidable N+1 query pattern

**Scope:** Scope §6.6 and §6.9; BP §§12 and 14.

`list_memberships` retrieves a page of memberships and then calls `membership_detail` for each row. Each detail can execute a user query, a membership-role query, and a role query. At the maximum page size of 100 this can create roughly 302 database round trips for one admin screen.

Evidence: [platform_admin/service.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/platform_admin/service.py), `list_memberships` and `membership_detail`.

This will make an otherwise routine platform-admin view slow and unnecessarily load the database. Since platform routes are cross-tenant and highly privileged, expensive listing endpoints are also attractive denial-of-service targets.

**Recommended remediation:** put a joined/batched read in `platform_admin/queries.py`: fetch membership/user rows in one query and all roles for the page in one additional query, then assemble the response in memory. Add a query-count or integration performance regression test for a 100-row page.

### MODERATE-04 — Audit history is vulnerable to cascade deletion by schema design

**Scope:** Scope §6.1 and acceptance criterion 1; BP §29.

`audit_events.organisation_id` uses `ON DELETE CASCADE`. Deleting an organisation directly, or adding an organisation delete route later without correcting this relation, would delete its audit history. `invitations.invited_by_user_id` likewise cascades invitation history when a user is removed. This conflicts with the practical expectation that audit evidence survives the lifecycle of the object it describes.

Evidence: [models.py](/home/daniel/Documents/Dev/app_template/backend/app/modules/audit/models.py) and the audit migration.

There is no organisation deletion endpoint in v0.4, so this is a forward-safety concern rather than an exposed v0.4 route. Still, audit retention should be enforced in the data model, not only by the absence of a current endpoint.

**Recommended remediation:** use `SET NULL` or `RESTRICT` for audit foreign keys according to the intended retention policy; preserve denormalised resource identifiers/metadata so events remain interpretable. Record an ADR-level retention decision and migration plan before adding destructive lifecycle endpoints.

### MINOR-01 — Provider calls have no explicit timeout/retry/circuit policy

**Scope:** BP §§8, 30 and 37.

The WorkOS adapters offload synchronous SDK calls with `asyncio.to_thread`, but they do not set application-level timeouts, bounded retries, or a circuit-breaker policy. A provider/network stall can tie up worker threads and keep privileged requests open until the SDK’s implicit timeout, which is not visible in this code.

**Recommended remediation:** configure explicit connection/read timeouts in the supported WorkOS client transport, use bounded retry only for demonstrably idempotent operations, and expose provider-failure metrics once observability lands. Ensure the invitation and organisation flows use idempotency keys before retrying writes.

### MINOR-02 — The full quality-gate result could not be conclusively observed in this environment

The lint and type-check stages of `make check` completed successfully:

- Ruff: passed.
- Frontend oxlint/ESLint: passed.
- Pyright: `0 errors, 0 warnings, 0 informations`.
- Vue type-check: completed successfully.

The backend suite began and progressed through the tests, including the targeted database audit tests (3 passed), but the execution host stopped the long-running command before a final pytest summary could be captured. This is an execution-environment limitation, not evidence of a test failure. The release claim that `make check` and `make e2e` are green must therefore be independently demonstrated from a clean, normal CI/local environment before sign-off.

## Positive controls verified

- **Separate platform plane:** `require_platform_permission` resolves only platform role permissions and does not use `X-Org-Id`. The platform/admin and organisation/member cross-plane denials are represented in the security suite.
- **Authentication boundaries:** platform and organisation routes use validated session dependencies; disabled users are rejected; identity assertions for bootstrap/invitation linking come from the WorkOS profile rather than frontend input.
- **Route completeness:** `test_security_suite.py` compares all registered `/api/v1` methods with `PROTECTED_ROUTES` plus the signature-gated webhook table. New v0.4 routes are included.
- **Webhook boundary:** the WorkOS webhook endpoint reads the bounded raw request body once, verifies HMAC before parsing, uses a 300-second replay tolerance, and remains non-authoritative for membership grants.
- **Tenant isolation:** organisation membership lookup is scoped by user and organisation; platform-admin operations use internal organisation identifiers from paths and do not turn a platform role into an organisation role.
- **API discipline:** reviewed endpoints declare response models; request schemas use `extra="forbid"`; routers primarily delegate transaction work to services.
- **Frontend state boundary:** direct imports of `src/api/client.ts` are confined to `src/queries/` in production code. The platform router guard and sidebar check `platform_roles` only for UX; backend enforcement remains authoritative.
- **Feature enforcement:** `records.deletion` is default-off and checked in the record service rather than the router or frontend.
- **Audit writes:** the reviewed v0.4 mutations use the shared insert-only `record_event` service and include a request ID in metadata.

## Scope-compliance matrix

| Area | Assessment | Notes |
| --- | --- | --- |
| Audit foundation | Partially compliant | Shape, list endpoint, and service are sound; retention cascade requires a policy correction. Platform-admin grant/revoke audit is absent because those operations are absent. |
| Platform authorisation plane | Partially compliant | Orthogonal dependency and seeded role are correct; ongoing platform-role lifecycle is missing. |
| WorkOS organisation mapping | Partially compliant | Nullable unique mapping and adapters exist; cross-system commit failure needs stronger reconciliation. |
| Bootstrap | Compliant in reviewed code | Email/verification checks, singleton race guard, transaction, and audit are present. |
| Invitations | Partially compliant | Login-time grant logic is careful, but duplicate invite race and provider/local split-brain remain. |
| Membership administration | Compliant with performance concern | Organisation membership lifecycle is present and audited; list query pattern needs batching. |
| Feature flags | Compliant | Closed catalogue, default off, service enforcement, platform-gated management. |
| Webhooks | Compliant for stated best-effort scope | Signature verification and non-authoritative consumer align with the contract. |
| Platform Admin Centre | Compliant in reviewed code | Generated client, query composables, route/nav gating, and v0.4 views are present. |
| Governance and release proof | Not yet sign-off ready | Human-review-sensitive changes need recorded approval; full check/e2e completion should be re-run and retained. |

## Required release plan

1. Resolve MAJOR-01 with a reviewed platform-admin lifecycle design, implementation, migration if needed, audit actions, security tests, and explicit final-admin protection.
2. Resolve MAJOR-02 with a database-level pending-invitation invariant and real concurrency coverage.
3. Decide and implement the v0.4 recovery contract for successful WorkOS writes followed by failed database commits; do not rely on undocumented manual repair for invitations.
4. Address the stale-email invitation path and batch membership-list queries.
5. Decide audit-retention semantics before destructive functionality is added; create a migration if retention must be durable.
6. Run and retain clean evidence for `make check`, `make e2e`, migration upgrade from v0.3, generated-client drift, and the mandatory security suite.
7. Obtain the required human reviews for the permission model, tenant isolation, authentication flow, secret handling, and public API additions before any corrective changes are committed.

## Suggested regression tests

- Two concurrent platform-admin invite requests for the same normalised email/org yield one local pending invitation and one provider send.
- A simulated database commit failure after WorkOS send leaves a discoverable/reconcilable intent and cannot silently produce an unusable invitation.
- An existing user whose verified WorkOS email changes can accept an invitation addressed to the new email; an old-email invitation does not grant.
- Platform-admin grant/revoke rejects non-admin callers, writes audit rows, is race-safe, and cannot revoke the final administrator without an explicit approved recovery procedure.
- A 100-row membership page stays within a small bounded query count.
- Deleting a related organisation/user preserves (or intentionally and explicitly retains) audit events according to the approved retention policy.

## Final conclusion

The v0.4 implementation is close to the intended architecture and demonstrates good security instincts. It should not, however, be declared fully implemented or released on the basis of the checked scope boxes alone. The missing platform-admin lifecycle is a material administrative-control gap, and the invitation and provider-transaction races need database-backed and operationally durable treatment. Once those items are corrected and the full release gate is independently green, the remaining design is a credible base for the next release.
