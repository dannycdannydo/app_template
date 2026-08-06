# Platform Administration & Invitation Workflow — Implementation Plan

Status: Draft (awaiting review per `CONTRIBUTING.md`)
Relates to: `Internal_Custom_Application_Starter_Architecture_v2.md` (blueprint), `IMPLEMENTATION_GUIDE.md`, `TEMPLATE_V0_3_SCOPE.md`, ADR-0001

## 0. Context and goals

WorkOS remains the identity provider: it owns identities, authentication,
sessions and invitation delivery. The application remains the source of truth
for organisations, memberships, roles, permissions, feature flags, audit
history and all business data. Administrators never create users in the WorkOS
dashboard as part of normal operation; the WorkOS Management API key exists only
server-side.

This plan replaces the org-first bootstrap (`POST /api/v1/organisations` makes
the first authenticated user an owner) with a **bootstrap platform admin** and a
**Platform Admin Centre**, and makes **WorkOS invitations** the standard
onboarding flow. It follows the template's module pattern (`models.py`,
`schemas.py`, `service.py`, `router.py`, `queries.py`), keeps routers thin,
keeps provider SDKs behind adapters, and does **not** introduce a global
`is_admin` bypass of the existing permission system.

> ADR-0001 consequence: "A WorkOS Organization is not automatically an
> application organisation … must add and document an explicit mapping and
> synchronisation design first." This plan is that mapping design (§3.1, §4).

### 0.1 Scope summary

- One-time bootstrap of a Platform Admin from a configured WorkOS email, audited.
- A dedicated **platform authorisation plane** (separate from org roles).
- Platform Admin Centre: organisations, invitations, memberships, feature flags, audit.
- WorkOS Invitation API as the only onboarding path for new members.
- `organisations.workos_organisation_id` mapping; internal id stays the primary key.

---

## 1. Blueprint changes (`Internal_Custom_Application_Starter_Architecture_v2.md`)

| Blueprint section | Change |
| --- | --- |
| §8 Authentication with WorkOS | Add the **bootstrap-admin rule** to "Backend rules": a configured `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` grants the Platform Admin role once, on first verified login, audited. Document that profile identity fields (including `email_verified`) come from the WorkOS profile, never the client. |
| §9 Organisations, Teams and Permissions | Add `invitations` to the core-tables list. Add a **Platform Admin Centre** subsection defining the platform plane: platform roles are distinct from org roles; platform endpoints never consult org membership and vice versa; no global bypass flag. Add `workos_organisation_id` as an optional organisation field (WorkOS org mapping, ADR-0001). |
| §27 Feature Flags | Promote DB-backed organisation flags from optional to the default for platform-controlled flags; state that flag management endpoints are platform-gated and enforcement is backend-side. |
| §29 Audit Logging | Add platform lifecycle events to the example catalogue (`platform.bootstrap_granted`, `platform_admin.granted/revoked`, `organisation.created`, `invitation.sent/accepted/revoked`, `membership.role_changed`, `membership.suspended/reactivated`, `feature_flag.changed`). |
| §31 Testing Strategy | Mandatory security tests gain a **cross-plane** check: an org `owner` cannot call platform routes (403) and a platform admin with no org membership cannot call org routes. Platform routes join the `PROTECTED_ROUTES` matrix. |
| §33 / AGENTS.md | This change touches authentication, the permission model, tenant isolation, secret handling and the public API — all human-review items. Record reviews per the existing workflow. |

Also amend **ADR-0001** (or add a new ADR) to record the WorkOS Organization
adoption decision: WorkOS orgs are created 1:1 with internal organisations, only
as containers for invitations, with the mapping stored in
`organisations.workos_organisation_id`.

---

## 2. Database model changes

All tables follow the existing conventions: UUIDv7 primary keys, `TimestampMixin`,
Alembic migration per change, naming convention from `db/conventions.py`.

### 2.1 `organisations` (change)

- Add `workos_organisation_id: str | None` — nullable, **unique**. Nullable
  because existing organisations have no mapping; unique because 1:1 is
  enforced at the database. Internal `id` remains the application primary key
  used everywhere.

### 2.2 `platform_roles` / `platform_memberships` / `platform_role_permissions` (new)

Mirrors the org plane (`roles`, `membership_roles`, `role_permissions`) so the
platform plane is enforced by the same machinery, not a flag:

- `platform_roles`: `id`, `code` (unique, seeded: `platform_admin`), `name`.
- `platform_role_permissions`: `platform_role_id` FK, `permission_id` FK
  (reuses the existing `permissions` table with new `platform.*` codes), unique
  pair — future granularity (e.g. a read-only auditor) without schema churn.
- `platform_memberships`: `user_id` FK → `users`, `platform_role_id` FK,
  unique `(user_id, platform_role_id)`. A user is a platform admin iff a
  membership row exists.

### 2.3 `invitations` (new)

The application's own record of every invitation — the source of truth for the
invite lifecycle and audit, independent of WorkOS delivery:

- `id` UUIDv7 PK
- `organisation_id` FK → `organisations` (NOT NULL) — target organisation
- `email` String(320), indexed — invitee's email
- `role_code` String — intended organisation role (validated against `roles.code`)
- `workos_invitation_id` String, unique, nullable — WorkOS invitation id once sent
- `invited_by_user_id` FK → `users` (NOT NULL) — the actor
- `status` enum: `sent` / `accepted` / `revoked` / `expired`
- `expires_at` timestamptz — mirror of the WorkOS invitation expiry
- `created_at`, `updated_at`

No membership row is created at invite time. Membership is created **at
acceptance** (login-time linking, §4.2), so the pre-existing
`MembershipStatus.INVITED` value is retained for compatibility but no longer
produced by new flows.

### 2.4 `audit_events` (new, blueprint §29)

Pulled forward from the v0.5 backlog — the bootstrap and invitation flows are
required to be "fully audited":

- `id` UUIDv7 PK
- `organisation_id` FK, **nullable** (platform events have no org context)
- `actor_user_id` FK → `users`, nullable (system events)
- `action` String(120), indexed
- `resource_type` String(80), `resource_id` String(80)
- `metadata` JSONB
- `created_at` only (append-only: no update path, no update column, no
  delete endpoint; services never modify or remove rows)

### 2.5 `organisation_features` (new, blueprint §27)

Pulled forward (DB feature flags were post-v1 in the scope docs; this plan makes
platform-controlled flags a v-scope item):

- `organisation_id` FK, `feature_key` String, `enabled` bool,
  `configuration_json` JSONB, unique `(organisation_id, feature_key)`
- Backend enforcement helper (e.g. `is_feature_enabled(org_id, key)`) added in
  `core/`; frontend visibility remains cosmetic.

### 2.6 `bootstrap_state` (new)

Durable, single-row guard for the one-time bootstrap:

- `id` UUIDv7 PK (or single-row key `id = 1`)
- `platform_admin_email` String — the consumed bootstrap email
- `consumed_by_user_id` FK → `users`
- `consumed_at` timestamptz

`users` itself is unchanged (`is_active` already blocks disabled platform
admins globally in `get_current_user`).

---

## 3. Required backend services

New modules follow the existing layout (`backend/app/modules/`), plus provider
adapters in a new `backend/app/integrations/workos/` package (blueprint §5).

### 3.1 `integrations/workos/organizations.py` (new adapter)

- `ensure_workos_organisation(org)` — if `org.workos_organisation_id` is null,
  call the WorkOS Organizations API (`create_organization`, name from the
  internal org) and persist the returned id. Used eagerly at platform org
  creation and lazily as a backfill for pre-existing organisations at first
  invite. Document the compensation story: WorkOS org created but the DB commit
  fails → orphan WorkOS org, cleaned by reconciliation (§9).
- Confirmed against the SDK during implementation: whether
  `create_organization` requires `domains` for invite-only orgs (open question,
  §11).

### 3.2 `integrations/workos/invitations.py` (new adapter)

- `send_invitation(email, workos_organisation_id)` → returns WorkOS invitation
  id + expiry; `revoke_invitation(workos_invitation_id)`;
  `get_invitation(workos_invitation_id)` (defense-in-depth check at acceptance).
- The `WORKOS_API_KEY` stays inside this adapter layer (same rule as
  `WorkOSUserProfileClient` in `core/security.py`).

### 3.3 `modules/platform_admin/` (new module)

Services (all audit each action):

- `grant_bootstrap_platform_admin(user)` / platform admin lifecycle:
  `list_platform_admins`, `grant_platform_admin`, `revoke_platform_admin`.
- Organisation administration: `create_organisation` (internal org + WorkOS org
  + mapping, transactional), `update_organisation`, `list_organisations`,
  `get_organisation` (cross-org: platform view, not tenant-scoped).
- Invitations: `invite_user(org, email, role_code)` (validate platform permission
  → ensure WorkOS org → WorkOS Invitation API → insert `invitations` row →
  audit `invitation.sent`), `revoke_invitation`, `list_invitations`.
- Membership administration: `assign_organisation_role`, `remove_membership`,
  `set_membership_status` (suspend/reactivate).
- Feature flags: `list_feature_flags`, `set_feature_flag`.
- Audit: `list_audit_events` (filter by org/actor/action, paginated).

### 3.4 `modules/invitations/` linking service

- `link_invitation_on_login(session, user)` — called from the provisioning
  path (§4.2); finds `sent` invitations for the user's email, verifies the
  WorkOS identity (`email_verified`), creates an active membership with the
  intended role, marks the invitation `accepted`, audits `invitation.accepted`
  and `membership.role_changed`. Idempotent and race-safe (unique constraints +
  IntegrityError rollback pattern, as in `users/service.py`).

### 3.5 `modules/audit/` (new module)

- `record_event(...)` service (append-only insert) + router for platform
  consumption. All existing modules that mutate org/membership state gain audit
  calls as part of this change.

### 3.6 `core/feature_flags.py` (new)

- `is_feature_enabled(session, org_id, key)` with a cache-friendly lookup and
  default-off behaviour; used by services, not by routers.

---

## 4. Required WorkOS integration

### 4.1 Invitation delivery

- WorkOS Invitation API (`invitations.send_invitation`) sends the email; the
  app stores `workos_invitation_id` and `expires_at`. Invitations reference a
  **WorkOS Organization**, which is why every internal organisation that
  invites must carry a `workos_organisation_id` (§3.1).
- Revocation goes through the same adapter so the app and WorkOS stay in sync.

### 4.2 Linking on first authentication

The authoritative acceptance point is **login time**, not a webhook:

1. User accepts the WorkOS invitation email.
2. User signs in; existing `get_current_user` → `get_or_provision_user`
   provisions/loads the internal user by `workos_user_id`.
3. Immediately after provisioning, `link_invitation_on_login` matches
   `invitations` rows by email (case-insensitive) with status `sent` and not
   expired; the authenticated WorkOS email must equal the invitation email.
4. Membership (`status=active`) + intended role created; invitation marked
   `accepted`; audit events written.

The existing `get_or_provision_user`/`get_me_payload` shape is preserved; the
link step is an additional service call in the dependency chain. The `org_id`
claim already parsed in `ValidatedSession` stays unused (org context continues
to come from `X-Org-Id`).

### 4.3 Webhooks (pulled forward from v0.5 backlog)

- Add `POST /api/v1/webhooks/workos` gated by the existing
  `verify_webhook_signature` (HMAC-SHA256, 300s tolerance,
  `core/security.py`), for invitation and user-lifecycle events
  (revoked/expired invitations, deactivated users).
- **Best-effort only**: webhook processing refreshes local `invitations`
  status; login-time reconciliation (§4.2) remains authoritative so the app
  never depends on webhook delivery for correctness.

### 4.4 Config

- `WORKOS_API_KEY` already exists (`core/config.py`). New: the webhook signing
  secret (`WORKOS_WEBHOOK_SECRET`) and `BOOTSTRAP_PLATFORM_ADMIN_EMAIL` (typed
  `pydantic-settings`, production fail-fast validation). Management key and
  webhook secret are backend-only; nothing new is added to `.env.example` for
  the frontend.

---

## 5. Required API endpoints

New **platform namespace** `/api/v1/platform/*` — every route requires the
dedicated `require_platform_permission(...)` dependency (Bearer token +
platform membership; no `X-Org-Id`, platform admins operate across orgs).
Response schemas are explicit; request schemas use `extra="forbid"`.

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/platform/organisations` | `platform.admin` | List organisations (paginated) |
| POST | `/api/v1/platform/organisations` | `platform.admin` | Create organisation (+ WorkOS org, mapping) |
| GET | `/api/v1/platform/organisations/{organisation_id}` | `platform.admin` | View organisation |
| PATCH | `/api/v1/platform/organisations/{organisation_id}` | `platform.admin` | Edit organisation |
| POST | `/api/v1/platform/organisations/{organisation_id}/invitations` | `platform.admin` | Invite user (email + role) → WorkOS Invitation API |
| GET | `/api/v1/platform/organisations/{organisation_id}/invitations` | `platform.admin` | List invitations |
| DELETE | `/api/v1/platform/organisations/{organisation_id}/invitations/{invitation_id}` | `platform.admin` | Revoke invitation |
| GET | `/api/v1/platform/organisations/{organisation_id}/memberships` | `platform.admin` | List memberships |
| POST | `/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}/roles` | `platform.admin` | Assign organisation role |
| DELETE | `/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}/roles/{role_code}` | `platform.admin` | Remove organisation role |
| PATCH | `/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}/status` | `platform.admin` | Suspend / reactivate membership |
| DELETE | `/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}` | `platform.admin` | Remove membership |
| GET | `/api/v1/platform/feature-flags` | `platform.admin` | List feature-flag catalogue + org overrides |
| PUT | `/api/v1/platform/feature-flags/{feature_key}` | `platform.admin` | Set org-level flag state |
| GET | `/api/v1/platform/audit-events` | `platform.admin` | Audit history (filterable, paginated) |
| GET | `/api/v1/platform/admins` | `platform.admin` | List platform admins |
| POST | `/api/v1/platform/admins/{user_id}` | `platform.admin` | Grant platform admin |
| DELETE | `/api/v1/platform/admins/{user_id}` | `platform.admin` | Revoke platform admin |
| POST | `/api/v1/webhooks/workos` | webhook secret | WorkOS event consumer (best-effort sync) |

Modified existing surface:

- `GET /api/v1/me` — response gains `platform_roles: list[str]` (empty for
  non-admins) so the shell can show/hide the Platform Admin Centre (UI only;
  backend enforces). Also drives the invitation-link step on first login.
- `POST /api/v1/organisations` (unprivileged org-first bootstrap) — **keep for
  backwards compatibility in this change**, but recommend gating org creation
  behind the platform plane in a follow-up release (breaking change, human
  review required; see §9 and §11).

Every new `/api/v1` route is added to `PROTECTED_ROUTES` in
`backend/tests/test_security_suite.py` (the completeness guard fails otherwise),
and the suite gains platform-specific cases: unauthenticated → 401, invalid
session → 401, disabled user → 403, non-platform-admin → 403
`platform_admin_required`, and **cross-plane denial** (org `owner` → 403 on
platform routes; platform admin with no org membership → 403 on org routes).
Platform routes take no `X-Org-Id`, so the org-context rows of the matrix do
not apply to them.

---

## 6. Required Vue admin pages

All inside the existing `AppShellLayout`; gated in the router and sidebar by
`platform_roles` from `useMeQuery` (UI-level only — backend enforcement is
authoritative, per blueprint §9).

| Route | View | Contents |
| --- | --- | --- |
| `/platform` | `PlatformDashboardView` | Summary: organisations, pending invitations, recent audit events |
| `/platform/organisations` | `PlatformOrganisationsView` | `DataTable` of organisations (pagination envelope), create action |
| `/platform/organisations/new` | `PlatformOrganisationFormView` | Standard form (name) → POST; on success redirects to detail |
| `/platform/organisations/:id` | `PlatformOrganisationDetailView` | Edit form; memberships `DataTable` (role select, suspend/reactivate, remove); feature-flag toggles; audit events for the org |
| `/platform/organisations/:id/invite` | `PlatformInviteUserView` | Invite form: email + organisation-role select; toast on success/error |
| `/platform/feature-flags` | `PlatformFeatureFlagsView` | Flag catalogue with org overrides |
| `/platform/audit` | `PlatformAuditView` | Audit `DataTable`, filterable by actor/action/organisation |

Frontend plumbing follows v0.3 conventions exactly:

- `make generate-client` regenerates `openapi.d.ts` (drift gate in `make check`).
- New query composables in `src/queries/platform.ts` keyed
  `['platform', ...]` (cross-org server state; org-scoped keys stay as-is).
- No component or store imports `src/api/client.ts` directly; toasts via the
  standard error envelope.
- `SidebarNav` gains the Platform Admin Centre entry only when the user is a
  platform admin; `router/index.ts` adds the routes with a `requiresPlatformAdmin`
  guard.
- Vitest coverage for the new views and the nav gate; a Playwright journey for
  the platform-admin invite flow using the test-profile session (v0.3 §6.7 pattern).

---

## 7. Permission model changes

### 7.1 A second, orthogonal plane — not a bypass

The existing plane is **org-scoped**: membership (`X-Org-Id`) → role bundles →
`permission_codes_for_membership` (`require_permission`). The new plane is
**platform-scoped**: user → `platform_memberships` → `platform_role_permissions`
via a new `require_platform_permission(code)` dependency that mirrors
`require_permission` but never touches organisation context.

Invariants (enforced by tests, §5):

- Platform routes require a platform permission; org role/ownership grants
  **no** platform access (an org `owner` gets 403).
- Org routes require org membership + org permission; platform membership
  grants **no** org access.
- No `is_admin`/superuser boolean is introduced anywhere.

### 7.2 New permission codes

- `platform.admin` (full platform administration) — new code in
  `permissions/constants.py`, granted to the seeded `platform_admin` platform
  role via a data migration. Future codes (e.g. `platform.audit.read`) slot in
  without model changes.
- Existing org codes `users.invite` / `users.manage_roles` /
  `organisation.manage` remain org-scoped. Org-level invitation (a member with
  `users.invite` inviting into their own organisation) reuses the same
  invitation service and WorkOS adapter, gated by the existing
  `require_permission("users.invite")` — the standard flow in this plan is
  platform-admin driven, but the org-plane path shares the implementation.

### 7.3 `/me` and UI awareness

`MeResponse` gains `platform_roles`; the frontend mirror (`src/lib/permissions.ts`)
gains a `usePlatformPermissions()` for nav/action visibility only. Enforcement
stays server-side.

---

## 8. Bootstrap implementation

1. **Config**: `BOOTSTRAP_PLATFORM_ADMIN_EMAIL: str | None` in
   `core/config.py` with the existing production fail-fast validation
   (documented in `.env.example`).
2. **Hook**: inside the `get_current_user` dependency chain, after
   `get_or_provision_user` returns the internal user: if
   `bootstrap_state` has no row **and** `user.email` matches the configured
   email (case-insensitive):
   - verify the WorkOS profile `email_verified` is true (extend `UserProfile`
     with the flag from `user_management.get_user`; never trust client input);
   - insert `bootstrap_state` + `platform_memberships(platform_admin)` in one
     transaction — the unique/single-row constraint makes a concurrent double
     grant impossible (IntegrityError → already consumed, same race pattern as
     `get_or_provision_user`);
   - write `audit_events` row `platform.bootstrap_granted` with the user and
     email.
3. **Once-only guarantees**: (a) `bootstrap_state` single-row insert is
   atomic; (b) audit trail records the grant; (c) once consumed, the check
   short-circuits on every later login. Optionally note in ops docs that the
   env var can be removed after bootstrap for defence in depth.
4. **Failure modes**: unverified email → no grant, warning log; email mismatch
   → no grant; the bootstrap email is **never** granted from a client-submitted
   value, only from the validated WorkOS profile.

---

## 9. Security implications and potential issues

1. **WorkOS Management API key**: server-side only, inside adapters; never in
   `VITE_*` variables or responses. Use least-privilege WorkOS API keys
   (invitations + organizations only) where the platform allows; log+monitor
   usage; rotation procedure documented. A leaked key could create orgs and
   send invitations — audit and immediate rotation are the mitigations.
2. **Two planes, tested cross-denial**: the core risk is accidentally treating
   platform membership as an org-permission bypass. The mandatory security
   suite gains cross-plane cases (§5) so the invariant cannot regress.
3. **Bootstrap abuse**: grant only for the exact configured email and only
   when the WorkOS profile reports `email_verified`; consume-once is atomic and
   audited. An attacker who controls the bootstrap email controls the platform
   — this is by design (the operator chooses the email) and must be documented
   as a first-login, never-auto-recreate property.
4. **Invitation hijacking**: the WorkOS invitation is bound to the invited
   email, and the app re-checks at login that the authenticated WorkOS email
   equals the invitation email before creating the membership. Revoked/expired
   invitations are checked locally (status + `expires_at`) and, for
   defence-in-depth, against WorkOS before grant.
5. **Suspension semantics**: suspended members are already blocked from org
   routes by `get_current_membership` (status must be `active`); a suspended
   user's session still works for `/me` — acceptable, document it. Suspension
   must also revoke pending invitations for that user.
6. **`workos_organisation_id` integrity**: unique constraint; eager creation +
   lazy backfill; orphan WorkOS orgs from failed transactions are reconciled
   (documented script or job). Ensure the mapping is never client-writable
   (`extra="forbid"`).
7. **Feature flags**: enforcement is backend-side (`core/feature_flags.py`,
   default off); platform flag endpoints are platform-gated; UI toggles are
   cosmetic.
8. **Audit integrity**: append-only by construction; no update/delete API;
   `metadata` JSONB for request context (`request_id`, ip at minimum); consider
   restricting UPDATE/DELETE privileges on `audit_events` at the DB role level.
9. **Webhooks**: signature-verified (`verify_webhook_signature`, 300s
   tolerance, constant-time compare); webhook input never mutates
   authoritative state directly — it only refreshes best-effort state, and the
   login-time reconciliation is authoritative (avoids webhook-replay and
   spoofing impact).
10. **Rate limiting / abuse**: the existing `/api/v1` coarse limiter covers the
    platform namespace; add a stricter limit on invitation-sending endpoints
    (mass-invite abuse) and audit every send.
11. **Legacy `POST /api/v1/organisations`**: stays unprivileged in this change;
    any authenticated user can still create an org and become owner. If the
    product wants platform-controlled org creation, this becomes a breaking
    change (move into the platform namespace) and needs human review before
    removal.
12. **WorkOS org count / billing**: one WorkOS Organization per internal org is
    a new WorkOS resource type; confirm limits and pricing in the target
    deployment profile.
13. **Email changes in WorkOS**: link by `workos_user_id`, never email, for
    existing users; an email that changed between invite and acceptance will
    not link — the invite must be re-sent (documented behaviour).
14. **Human review**: authentication, permission-model, tenant-isolation,
    secret-handling and public-API changes are all in the AGENTS.md review
    list; each work unit goes through implement → review → apply-and-commit.

---

## 10. Proposed sequencing

Dependencies: audit first (bootstrap/invitations are audited), then the
platform plane, then bootstrap, then invitations, then the rest. Each work unit
is a reviewable unit per `CONTRIBUTING.md`; scope/acceptance details land in a
new `TEMPLATE_V0_6_SCOPE.md` (or amended `IMPLEMENTATION_GUIDE.md` entry).

- [ ] WU1 Audit foundation — `audit_events` table, `record_event`, list endpoint, append-only tests
- [ ] WU2 Platform plane — tables, seed (`platform_admin`, `platform.admin`), `require_platform_permission`, `/me` `platform_roles`, security-suite cross-plane cases
- [ ] WU3 WorkOS org mapping — `workos_organisation_id`, adapter, eager create + lazy backfill
- [ ] WU4 Bootstrap — `BOOTSTRAP_PLATFORM_ADMIN_EMAIL`, `bootstrap_state`, provisioning hook, audit
- [ ] WU5 Invitations — WorkOS adapter, `invitations` table, invite/revoke/list endpoints, login-time linking
- [ ] WU6 Membership administration — role assign/remove, suspend/reactivate, remove
- [ ] WU7 Feature flags — `organisation_features`, enforcement helper, platform endpoints
- [ ] WU8 Webhooks — `POST /api/v1/webhooks/workos`, best-effort consumer
- [ ] WU9 Platform Admin Centre UI — routes, views, queries, nav gating, Vitest + Playwright
- [ ] WU10 Docs & governance — blueprint amendments, ADR, scope doc, `ARCHITECTURE.md`/`AGENTS.md` updates, human reviews

---

## 11. Open questions

- Does `create_organization` require `domains` in the target WorkOS plan for
  invite-only organisations? Determines the org-create payload and whether the
  platform org form needs a domain field.
- Keep or deprecate the unprivileged `POST /api/v1/organisations` (see §9.11)?
- Should org-level `users.invite` invite users into the org through the same
  WorkOS flow in this release, or platform-admin-only invites first?
- Feature-flag scope for v-scope: platform-only flags, or also the
  organisation-level override UI in §6?
