# Identity and Application-Level Tenant-Isolation Hardening Plan

Status: Complete

Relates to: `Internal_Custom_Application_Starter_Architecture_v2.md` BP
§§8–13 and §§28–31; `plans/IDENTITY_AND_TENANT_SECURITY_HARDENING_PLAN.md`;
`SECURITY.md`; and `AGENTS.md`.

## Goal

Strengthen the starter's highest-value security boundaries without turning the
work into a general production-hardening programme. This plan covers only:

1. identity and session hardening; and
2. real-database tests of the organisation isolation contract.

The existing audit, revision, durable-job, object-storage and recovery controls
remain part of the baseline, but further expansion of them is deferred unless
a concrete product requirement or incident reopens the risk.

## Agreed scope

- **P1 — Identity and session hardening.** Represent validated identity claims
  in a bounded authenticated-session context, reject impersonated and
  over-long-lived sessions, record the accepted revocation window, revalidate
  the provider invitation (identity, organisation, email, state, expiry) before
  granting membership, and make duplicate webhook delivery a deterministic
  no-op through a persisted event-id ledger. Authentication and tenant-isolation
  changes require human review before application.
- **P2 — Organisation-isolation contract tests.** Prove the intended
  organisation boundary at the application layer with a reusable real-PostgreSQL
  fixture and a checked-in registry of tenant-owned resources, covering list,
  detail, create, update, delete, action, indirect and capability paths.

P1 and the fixture preparation for P2 may proceed independently. Both work
units require review before application where they change authentication or
tenant-isolation behaviour.

## Out of scope

These are acknowledged but are not active implementation work:

| Area | Current position | Reopen when |
| --- | --- | --- |
| Audit hardening | Existing append-only audit/revision controls are sufficient for the starter | a customer requires tenant audit export, legal hold, independent tamper evidence, or a coverage gap is found |
| Object/database consistency | Existing immutable promotion, durable jobs, versioning/backups and operator recovery are accepted | observed orphan/lost-object incidents, stricter durability SLA, or destructive automation is introduced |
| Malware scanning | Scanner adapter remains deployment/application policy | untrusted public uploads or a production security baseline requires scanning |
| Backup infrastructure | Deployment-specific controls remain documented outside this application plan | a concrete hosting profile is selected or RPO/RTO commitments are made |
| Organisation-admin membership UI/API | Platform-only administration is sufficient | customers require delegated self-service administration |
| Intra-organisation teams | Not part of the data-isolation model | a product requires partitions inside one subscriber organisation |

Intra-organisation teams, resource partitions and custom per-organisation role
definitions are not required. Membership, invitation and role administration may
remain platform-only. Groups needing hard data separation must be represented as
separate organisations. An actual defect in any deferred area should be fixed
and tested when found; deferral is not permission to ignore a known
vulnerability or data-loss path.

## Decisions and assumptions

### Settled organisation model

- An organisation is the hard tenant/subscriber boundary.
- No organisation membership or role grants access to another organisation.
- A user may belong to more than one organisation, but each membership has its
  own status and roles.
- Roles are organisation-wide user types, such as owner, administrator,
  manager, member and viewer. They are not separate data partitions.
- Business resources are visible organisation-wide when the selected
  membership has the required permission. User-private resources retain any
  additional user filter.
- Any future change to these decisions is a permission-model and
  tenant-isolation change requiring a new plan and human review.

### Baseline to preserve

- WorkOS JWT signature, issuer, audience/client, expiry and required-claim
  validation;
- enabled-user and active-membership checks;
- validated `X-Org-Id` context and default-deny permission checks;
- organisation-scoped resource queries, with foreign resources returned as not
  found;
- separate organisation and platform authorisation planes;
- per-membership frontend roles and permissions for the selected organisation;
- the mandatory protected-route security matrix; and
- existing append-only audit/revision and durable job/outbox controls.

### Approved P1 identity decisions (owner, in-session 2026-09-18)

1. Impersonation (`act` claim) is **always rejected**; no support workflow.
2. Maximum access-token lifetime **3600 s** (`WORKOS_JWT_MAX_LIFETIME_SECONDS`,
   must be positive).
3. **No online revocation denylist**; accepted revocation window = a revoked
   token's remaining lifetime, bounded by the maximum.
4. **No operation requires recent authentication** (default WorkOS access
   tokens carry no `auth_time` and there is no re-auth flow); `auth_time` is
   still represented in the session context.
5. WorkOS is **authoritative** for invitation acceptance; revalidate at grant
   and **fail closed** on outage/mismatch.

## Commands that must work

Focused commands for this plan:

```bash
cd backend && uv run pytest \
  tests/test_security.py \
  tests/test_auth.py \
  tests/test_invitations.py \
  tests/test_webhooks.py
```

```bash
cd backend && uv run pytest \
  tests/test_security_suite.py \
  tests/test_invitations_db.py \
  tests/test_webhooks_db.py \
  tests/test_identity_races_db.py
```

Existing final gates remain green:

```bash
make check
```

If a required local command (for example a targeted real-PostgreSQL run) is not
included in `make check`, it is run explicitly for the affected checkpoint.

## Acceptance criteria

1. Impersonation, token lifetime, revocation and recent-authentication
   behaviour are explicit, tested and documented.
2. A revoked, expired or mismatched provider invitation cannot grant local
   membership under the approved provider-availability policy.
3. A valid user cannot read, infer, mutate, download or enqueue work for an
   organisation resource outside the selected active membership.
4. Permissions held in one organisation do not affect another, including
   for a user who belongs to both.
5. Platform authority does not silently become tenant-data authority.
6. All affected lint, type, migration, generated-client, backend/frontend
   and mandatory security gates pass on the supported toolchain.
7. Each authentication or tenant-isolation work unit has recorded human
   review before apply-and-commit.

## Implementation checkpoints

### P1 — Identity and session hardening

Dependencies: none

Human review required before application: authentication and tenant isolation.

- [x] Decide whether WorkOS impersonation is always rejected. Default
      recommendation: reject it unless a separately designed, visible and
      audited support workflow is approved.
- [x] Choose a maximum accepted access-token lifetime in addition to normal
      expiry validation.
- [x] Choose the acceptable session-revocation delay and document whether a
      revocation denylist/check is justified by that target.
- [x] Identify the small set of genuinely high-risk platform actions, if any,
      that require recent authentication and choose the maximum authentication
      age.
- [x] Confirm that a provider invitation must still be valid when it creates a
      local membership, and define fail-closed behaviour when WorkOS cannot be
      checked.
- [x] Represent validated identity claims in a bounded authenticated-session
      context, including subject, session ID, issued-at, expiry,
      authentication time and optional impersonator identity where available.
- [x] Reject impersonated sessions according to the approved policy.
- [x] Reject tokens whose total lifetime exceeds the approved maximum.
- [x] Implement the approved revocation policy, or explicitly record the
      accepted revocation window if no online/denylist check is selected.
- [x] Apply recent-authentication checks only to the approved high-risk
      operations; do not add friction to ordinary tenant work without a stated
      reason.
- [x] Revalidate provider invitation identity, organisation, email, state and
      expiry before granting membership; never grant solely from a stale local
      `sent` row.
- [x] Persist provider webhook event IDs with a uniqueness constraint so
      duplicate delivery is a deterministic no-op.
- [x] Keep tokens, credentials and full provider payloads out of logs and
      audit metadata.
- [x] Add focused tests for normal, expired, excessive-lifetime,
      impersonated, revoked and stale-authentication sessions.
- [x] Add invitation tests for missed revocation, duplicate delivery, expiry,
      provider outage, mismatched email and cross-organisation provider IDs.
- [x] Document the identity decisions and residual revocation window in an ADR
      or the applicable versioned scope.
- [x] Pass all focused identity/invitation tests and the mandatory security
      suite.
- [x] Record human review approving the implementation before it is applied.

### P2 — Organisation-isolation contract tests

Dependencies: P1

Human review required before application: tenant isolation.

- [x] Create a reusable real-PostgreSQL fixture containing organisation A,
      organisation B, an A-only user, a B-only user, a user who is owner/admin
      in A but viewer in B, suspended membership, and a platform-only user.
- [x] Maintain a small checked-in registry of current organisation-owned and
      user-private tables/resources and their ownership columns.
- [x] Test list, detail, create, update, delete and action routes, as applicable,
      with valid foreign resource IDs.
- [x] Test indirect access paths that currently exist, including files/jobs,
      notifications/deliveries and AI requests/references.
- [x] Prove pagination totals, filters, downloads and generated capabilities do
      not reveal foreign-row existence.
- [x] Prove roles in organisation A grant no API or visible UI capability in B,
      including for the same multi-membership user.
- [x] Prove platform authority alone does not grant tenant-data access and
      organisation roles do not grant platform access.
- [x] Keep `PROTECTED_ROUTES` and its cross-organisation/viewer-write checks
      complete for every protected route.
- [x] Add a lightweight structural check or review checklist requiring every
      new tenant-owned model to state its isolation strategy.
- [x] Prove the registry covers every current tenant-owned table.
- [x] Pass the full two-organisation role matrix against real PostgreSQL.
- [x] Demonstrate failure when a representative organisation predicate is
      deliberately omitted in test-only code.

## Reference map

| Checkpoint | Governing sources | What to extract |
| --- | --- | --- |
| P1 | BP §8–§13 and §§28–§31; `SECURITY.md`; `docs/decisions/0021-identity-session-hardening.md`; `backend/app/core/security.py`; `backend/app/api/dependencies.py`; `backend/app/modules/invitations/service.py`; `backend/app/modules/webhooks/service.py` | Centralised session validation, bounded identity context, impersonation/lifetime policy, authoritative invitation revalidation, webhook dedup and audit/logging constraints |
| P2 | BP §7–§14 and §31; `SECURITY.md`; `backend/tests/test_security_suite.py`; organisation-scoped queries across `backend/app/modules/` | Tenant boundary, default-deny permissions, organisation-scoped query shape, real-PostgreSQL fixture and protected-route inventory |

## API, data and security impact

- **API/generated types:** P1 adds no route and no request/response schema. The
  `/api/v1/me` and `POST /api/v1/webhooks/workos` surfaces keep their explicit
  response models; `PROTECTED_ROUTES` and the generated frontend client stay
  unchanged.
- **Data:** P1 adds `webhook_events` (unique `event_id`) and a nullable
  `invitations.workos_organisation_id` through an additive Alembic migration.
  Pre-existing invitation rows stay valid and fail closed at acceptance until
  re-invited.
- **Authentication:** impersonated sessions are rejected centrally; a maximum
  token lifetime bounds the offline revocation window; disabled users remain
  blocked; raw token claims are never retained or logged.
- **Tenant isolation:** the organisation comes only from validated context; the
  provider invitation is revalidated at grant so a revoked, expired, mismatched
  or cross-tenant provider invitation cannot create a membership.
- **Secrets:** provider SDKs stay behind adapters; no key, token, credential or
  full provider payload is logged or written to audit metadata.

## Validation plan

- **Pure/unit tests:** bounded session context, issuer/audience/required-claim
  validation, malformed timestamps and `org_id`, excessive-lifetime rejection,
  `auth_time` representation, impersonation rejection and webhook payload
  parsing and signature acceptance.
- **Request-flow tests:** invite/revoke/list endpoints, login-time linking,
  never-grant cases (revoked, expired, mismatched, unverified, cross-tenant,
  provider outage, mismatched provider id), webhook refreshes and no-op
  duplicate delivery.
- **PostgreSQL integration tests:** the additive migration applies, the
  migrated table shapes and constraints hold, the invite→accept journey
  round-trips, the webhook event ledger deduplicates a redelivery, and the
  identity races hold under two real sessions.
- **Mandatory gates:** `make check` (backend/frontend lint, typecheck, full
  tests, AI-registry and execution-contract validation, generated-client
  drift) plus the mandatory protected-route security suite.

## Review and delivery

- P1 and P2 follow `implement → review → apply-and-commit`; no unreviewed
  commit.
- Authentication and tenant-isolation changes require recorded human review
  before application (see `AGENTS.md` and `CONTRIBUTING.md`).
- The active contract records the completed checkpoint checkboxes; the plan is
  marked `Status: Complete` only when every checkpoint item is checked.
