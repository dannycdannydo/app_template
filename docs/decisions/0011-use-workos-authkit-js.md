# ADR 0011: Use the WorkOS AuthKit Browser SDK for the Frontend Login Flow

Status: Accepted

## Context

v0.3 Scope §6.2 builds the browser login flow: a "Continue with WorkOS" button
that redirects to WorkOS, an auth-callback route that completes the
authorization-code exchange, and session restoration on app boot. ADR 0001
decided WorkOS owns login and session management end-to-end (blueprint §8),
and the repo rule is that provider SDKs stay behind adapters. The frontend
therefore needs the WorkOS browser SDK to drive that flow, confined to a
single adapter module (`src/features/auth/workos.ts`) exposing only
`startLogin`, `completeLogin`, `signOut` and `getSession` (v0.3 Scope §6.2, §6.8).

## Options considered

- **@workos-inc/authkit-js**: WorkOS's official browser SDK. It builds the
  authorization URL with the configured client id and callback URL, detects
  the authorization `code` in the callback URL, exchanges it for a session,
  persists the session, and restores it on boot. It is the reference client
  for the exact flow the template needs and keeps the PKCE/state mechanics
  inside a library that WorkOS tests and maintains.
- **Hand-rolled OAuth 2.1 + PKCE against the WorkOS authorize/token
  endpoints**: no extra dependency, but every project would re-implement
  PKCE, state handling, refresh-token rotation and session persistence, which
  is exactly the "do not build custom auth logic without need" rule and a
  standing security liability.
- **A generic OAuth client (e.g. oidc-client-ts)**: possible, but it is not
  WorkOS-specific and adds its own abstraction; the official SDK is the
  smallest surface that matches the documented flow.

## Decision

Use **@workos-inc/authkit-js** as the browser-side WorkOS SDK, imported in
exactly one module (`frontend/src/features/auth/workos.ts`). The module is
the adapter: the rest of the application calls `startLogin`, `completeLogin`,
`signOut` and `getSession` and never imports the SDK. Session state held by
the SDK (the WorkOS refresh token in local/session storage) is not treated as
application state; the session store in Pinia holds only what the shell needs
for the router guard and the Bearer-token injection (v0.3 Scope §6.2, blueprint
§14 client-state boundary).

## Consequences

- The login, callback and boot-restore flows ship as WorkOS-maintained SDK
  behaviour instead of hand-rolled OAuth code, and the SDK is testable behind
  the adapter with a stubbed implementation.
- A new runtime dependency is introduced; it is the provider's own SDK and
  stays confined to the adapter, so swapping providers later changes one file.
- The SDK persists its own session artifacts in browser storage; the backend
  still validates every presented session token exactly as v0.2 established
  (ADR 0001), so the frontend never bypasses the enforcement point.
- The dependency is justified here per the repo rule (AGENTS.md; ADR 0005,
  0009 and 0010 set the precedent for documented frontend dependencies).
