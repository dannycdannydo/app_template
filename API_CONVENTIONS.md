# API Conventions

REST, JSON, OpenAPI, versioned under `/api/v1`. GraphQL is not part of the default architecture.

## Style

- REST over JSON.
- OpenAPI is the source of truth for the HTTP surface; the frontend client is generated from it (see `frontend/src/api/`).
- Endpoints are grouped by domain under `/api/v1`.

## Versioning

The bundled frontend and backend may evolve together.

Backward compatibility is required for:

- external clients;
- published APIs;
- integrations;
- webhooks;
- retained domain events.

New API versions are introduced only for genuine breaking contracts that must coexist. There is no per-endpoint ad-hoc versioning.

## Resources and verbs

- Collection endpoints use the plural noun: `GET /api/v1/properties`, `POST /api/v1/properties`.
- Item endpoints use the singular with an identifier: `GET /api/v1/properties/{id}`, `PATCH /api/v1/properties/{id}`, `DELETE /api/v1/properties/{id}`.
- Nested resources are modelled explicitly where the hierarchy is real, not for convenience.
- Actions that do not fit resource verbs use explicit sub-resources or read-only search endpoints.

## Pagination

Default format:

```text
?page=1&page_size=50
```

Response:

```json
{
  "items": [],
  "page": 1,
  "page_size": 50,
  "total": 0
}
```

Use cursor pagination only where justified (e.g. high-volume append-only streams).

## Filtering and sorting

```text
?search=manchester
&status=active
&sort=-created_at
&page=1
&page_size=50
```

- Only approved filter and sort fields are allowed; unknown fields are rejected.
- Prefix a field with `-` to sort descending.
- `search` is a domain-defined text search.

## Search endpoints

Use domain-specific endpoints. For large structured searches, use read-only POST endpoints:

```text
POST /api/v1/properties/search
```

## Errors

One structured error format for the whole API:

```json
{
  "code": "property_not_found",
  "message": "The property could not be found.",
  "details": null,
  "request_id": "..."
}
```

Validation example:

```json
{
  "code": "validation_error",
  "message": "The request contains invalid data.",
  "details": [
    {
      "field": "asking_price",
      "message": "Value must be greater than or equal to zero."
    }
  ],
  "request_id": "..."
}
```

### Exception rules

Services raise domain exceptions; central FastAPI exception handlers translate them to HTTP responses. Typical mappings:

| Exception | HTTP status |
| --- | --- |
| `NotFoundError` | 404 |
| `PermissionDenied` | 403 |
| `ConflictError` | 409 |
| `ValidationError` | 422 |
| `RateLimitExceeded` | 429 |

- Error messages must not leak internals, stack traces, or secrets.
- Every error response includes the `request_id` from the request context to correlate with logs.

## Request IDs and logging

- Every request carries a `request_id`, generated at the edge and propagated through logs and error responses.
- Structured JSON logging is used; see `ARCHITECTURE.md` and the blueprint §28.

## Response schemas

- Every endpoint declares an explicit response schema; nothing is returned as an ad-hoc dict.
- ORM models are never API request models (blueprint §7).

## Authn/authz

WorkOS owns login and sessions; the application resolves a validated identity to an internal user and an organisation context (v0.2). Conventions:

### Authentication

- Every `/api/v1` route except `/health` and `/ready` requires a Bearer token in the `Authorization` header: `Authorization: Bearer <session-token>`.
- Missing or malformed token → `401` with code `invalid_token`; a token that fails signature/issuer/audience/expiry validation → `401` with code `invalid_session`.
- A disabled user is rejected with `403` and code `user_disabled` even with an otherwise valid session.
- Identity fields are never trusted from the client: email/name come from the validated WorkOS profile, and request bodies that attempt to smuggle identity fields are rejected outright (`extra` fields are forbidden on request schemas).

### Organisation context

- Tenant-scoped endpoints additionally require the `X-Org-Id` header, which carries the organisation the caller acts within.
- Missing `X-Org-Id` → `400` with code `org_context_required`; malformed (not a valid UUID) → `400` with code `invalid_org_id`.
- An organisation the caller is not an active member of → `403` with code `not_a_member`.
- The organisation id is always derived from this validated header context, never from a request body.
- Two bootstrap endpoints are intentionally identity-scoped and require only the Bearer token, because the caller cannot yet be a member of any organisation:
  - `GET /api/v1/me` — returns the current user and their memberships (used to pick an organisation).
  - `POST /api/v1/organisations` — creates an organisation; the creator's membership is assigned the `owner` role.

### Authorisation

- Default deny: a caller may act only through permissions granted to the roles on their memberships.
- Backend permissions are authoritative; frontend visibility is only a UX aid.
- Cross-organisation access must never leak: resources outside the caller's organisation are treated as not found where the resource model requires it.
- Every write to a tenant-scoped resource is permission-gated with `require_permission(...)` (v0.2 Scope §6.4), which resolves the caller's membership from `X-Org-Id` and checks the permission code against the role bundles; a code not granted to any of the caller's roles is denied with `403` and code `permission_denied`.

### Security regression suite

The mandatory reusable security tests in `backend/tests/test_security_suite.py` (blueprint §31, v0.2 Scope §6.6) parametrise the whole protected surface: unauthenticated rejected, invalid sessions rejected, cross-organisation access denied, viewer writes denied, disabled users denied, and stack traces not exposed. The suite's `PROTECTED_ROUTES` table lists every protected endpoint once, and a completeness guard fails when a new `/api/v1` route is registered without being added to it. Adding a route to that table is a mandatory part of adding the endpoint.
