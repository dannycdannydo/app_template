# ADR 0013: WorkOS Organizations for Invitations behind a Platform Authorisation Plane

Status: Accepted

## Context

v0.2 established WorkOS as the identity provider: WorkOS owns login, sessions, credentials and SSO; the application owns users, organisations, memberships, roles and permissions. ADR-0001 explicitly warned that a WorkOS Organization is not automatically an application organisation and that a product deliberately adopting WorkOS Organizations must first add and document an explicit mapping and synchronisation design.

The v0.4 release (platform administration) needs an onboarding flow for users into existing organisations. WorkOS offers an Invitation API for exactly this: an invitation is delivered and managed by WorkOS (sent, expires, revokes), while the application decides what membership the accepting user gains. Two questions had to be resolved before building the flow:

1. Do we create a WorkOS Organization per application organisation, or invite users without any WorkOS org context?
2. Where does the administration of organisations, memberships, invitations and feature flags live, given that it is cross-tenant by nature and must never become a global bypass of the organisation permission system?

## Options considered

### 1. WorkOS org mapping

- **WorkOS Organization per application organisation (adopted)**: each internal organisation optionally carries `workos_organisation_id`; the WorkOS org is created eagerly at platform org creation and lazily backfilled at first invite. Invitations are sent into a WorkOS org that mirrors the internal one. The internal id remains the primary key everywhere.
- **No WorkOS org mapping**: invitations are sent without a WorkOS organisation context. Simpler initially, but WorkOS invitations then carry no org identity, and any future WorkOS-side org features (directory sync, SSO per org) would require a migration later.

### 2. Platform administration plane

- **Dedicated platform plane (adopted)**: a second, orthogonal authorisation plane — `platform_roles`, `platform_role_permissions`, `platform_memberships` — with a seeded `platform_admin` role carrying the `platform.admin` permission code. A `require_platform_permission(code)` dependency validates the Bearer token and platform membership only, and never consults `X-Org-Id`. Cross-tenant administration routes live under `/api/v1/platform/*`.
- **Reuse the organisation plane with a superuser flag**: an `is_admin` boolean on the user record would be simpler but creates exactly the hidden global bypass the blueprint forbids; it would also make every org-scoped permission check have to reason about a user with no membership.
- **WorkOS dashboard for administration**: keeping user and organisation administration in the WorkOS dashboard conflicts with the v0.4 goal (the application is the source of truth for organisations, memberships, roles, permissions, feature flags and audit history) and would split administration across two systems.

## Decision

**Create one WorkOS Organization per application organisation and store the mapping in `organisations.workos_organisation_id`** (nullable, unique; ADR-0001's mapping requirement, satisfied by the design in `PLATFORM_ADMIN_WORKFLOW_PLAN.md`). The mapping is server-side only: it is never client-writable (request schemas use `extra="forbid"`). WorkOS orgs are created eagerly when a platform admin creates an organisation, and lazily backfilled for pre-existing organisations at first invite.

**Administer the application through a dedicated platform authorisation plane** — separate tables, a seeded `platform_admin` role with the `platform.admin` permission code, and a `require_platform_permission(code)` dependency that resolves the caller through platform memberships alone. The platform plane is a separate plane, never a bypass: it grants cross-tenant administration to explicitly-configured platform admins, while organisation routes keep enforcing their own membership and permission rules; an organisation `owner` without a platform membership gets `403 platform_admin_required` on platform routes, and a platform admin without an organisation membership gets `403 not_a_member` on organisation routes.

## Consequences

- One WorkOS Organization exists per application organisation that has ever been invited into; the mapping is stored and reconciled lazily. WorkOS org count grows with application org count, which must be confirmed against the target plan (open question in the workflow plan §11).
- The platform plane is the only way to administer organisations, memberships, invitations, feature flags and audit history. There is no `is_admin`/superuser boolean anywhere in the model or services.
- Every platform lifecycle action is audited (bootstrap grant, organisation create/update, invitation sent/accepted/revoked, membership role change/suspend/reactivate/remove, feature-flag change) through the shared append-only `record_event` service (blueprint §29).
- The WorkOS Invitation API is the standard onboarding path: the application stores the `invitations` row (status, expiry, `workos_invitation_id`) and creates the membership at **login-time linking** — acceptance is authoritative, webhook delivery is best-effort.
- The WorkOS Management API key and webhook secret exist server-side only; the frontend never sees them and never submits identity fields.
- The platform plane, invitation flow, and request flow for `/api/v1/platform/*` are documented in `ARCHITECTURE.md`; the blueprint sections §8, §9, §27, §29, §31 and §33 were amended to match (see the v0.4 scope §6.10).
- Permission-model, tenant-isolation, auth-flow and secret-handling changes are all in the human-review list (see `AGENTS.md` and `CONTRIBUTING.md`); the v0.4 release records those reviews per the existing workflow.

---

## Amendments to ADR 0001

The ADR-0001 consequence ("A WorkOS Organization is not automatically an application organisation … must add and document an explicit mapping and synchronisation design first") is satisfied by this ADR and by `PLATFORM_ADMIN_WORKFLOW_PLAN.md` (§3.1, §4): the mapping is 1:1, stored in `organisations.workos_organisation_id`, created eagerly and backfilled lazily, and never client-writable.
