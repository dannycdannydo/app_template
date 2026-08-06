# ADR 0001: Use WorkOS for Authentication

Status: Accepted

## Context

Applications built from this template need authentication with social login, enterprise SSO (SAML/OIDC), and organisation management, without every project building and maintaining its own auth stack. Self-hosting an identity provider is operationally heavy, and rolling our own password auth is a security liability.

## Options considered

- **WorkOS**: managed identity platform providing email/password (Directory Sync), social login, and SSO; owns the identity provider, session, and directory-sync mechanics.
- **Auth0 / Cognito / Okta**: mature managed alternatives, but heavier vendor lock-in, different per-tenant cost profiles, and no single obvious default across deployment profiles.
- **Self-hosted (Keycloak, Authentik)**: full control but significant operational burden (upgrades, hardening, backup) for every application.
- **In-house password auth**: maximum control, maximum security and maintenance liability; no SSO or directory sync without additional work.

## Decision

Use **WorkOS** for authentication and identity-provider integrations. Sessions are
validated against WorkOS; the template's own user, organisation, membership,
role and permission records remain in the application database. WorkOS is the
source of truth for credentials and SSO, while the application is the source of
truth for its tenant boundaries and shared-data access rules.

## Consequences

- Applications avoid owning credentials, password resets, SSO configuration, and directory sync.
- A WorkOS Organization is not automatically an application organisation. A
  product that deliberately adopts WorkOS Organizations must add and document
  an explicit mapping and synchronisation design first.
- WorkOS is an external dependency for authentication; the integration must be kept behind an adapter so the auth surface is testable and swappable.
- Cost depends on active users, which is acceptable for the target deployments.
- Authentication changes remain in the human-review list (see `AGENTS.md`).

---
