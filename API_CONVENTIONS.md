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
- Authenticated `/api/v1` requests additionally bind `user_id` and `organisation_id` to every log line (cleared per request); worker tasks bind `job_id` and `resource_id`; every line carries a consistent `event` name.
- The BP §28 never-log list (passwords, tokens, authorisation headers, signed URLs, full connection strings) is enforced by test.
- `GET /metrics` is public (like `/health` and `/ready`) and returns Prometheus text format — request counters/latency histograms plus job counters (enqueued/succeeded/failed).

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

### Platform plane (v0.4)

The platform administration plane is a separate authorisation plane (ADR-0013, v0.4 Scope §6.2) that administers organisations, memberships, invitations, feature flags and audit history across tenants:

- Platform routes live under `/api/v1/platform/*` and take **no** `X-Org-Id` header: the caller acts as a platform administrator, not as a member of the organisation they administer.
- Every platform route is gated by `require_platform_permission("platform.admin")` (v0.4 Scope §6.2), which resolves the caller through platform memberships and role bundles only; a caller with no granting platform membership is rejected with `403` and code `platform_admin_required`.
- The two planes never grant across each other: an organisation `owner` without a platform membership is `403` on platform routes, and a platform admin without an organisation membership is `403` (`not_a_member`) on organisation routes. There is no `is_admin`/superuser boolean anywhere.
- Organisations are identified by their internal id in the path (`/api/v1/platform/organisations/{organisation_id}`), never by the server-side `workos_organisation_id` mapping, which is never client-writable (`extra="forbid"`).
- `GET`/`POST /api/v1/platform/admins` and `DELETE /api/v1/platform/admins/{platform_membership_id}` administer explicit `platform_admin` memberships. All are platform-gated, audited, and revocation rejects removal of the final administrator.
- `GET /api/v1/platform/users` is platform-gated and returns only enabled users' internal IDs, names and emails for the administrator assignment picker; it accepts standard pagination and an optional bounded name/email search.
- Platform listing endpoints use the same pagination envelope as tenant-scoped listings (blueprint §12).

### Authorisation

- Default deny: a caller may act only through permissions granted to the roles on their memberships.
- Backend permissions are authoritative; frontend visibility is only a UX aid.
- Cross-organisation access must never leak: resources outside the caller's organisation are treated as not found where the resource model requires it.
- Every write to a tenant-scoped resource is permission-gated with `require_permission(...)` (v0.2 Scope §6.4), which resolves the caller's membership from `X-Org-Id` and checks the permission code against the role bundles; a code not granted to any of the caller's roles is denied with `403` and code `permission_denied`.

### Security regression suite

The mandatory reusable security tests in `backend/tests/test_security_suite.py` (blueprint §31, v0.2 Scope §6.6) parametrise the whole protected surface: unauthenticated rejected, invalid sessions rejected, cross-organisation access denied, viewer writes denied, disabled users denied, and stack traces not exposed. The suite's `PROTECTED_ROUTES` table lists every protected endpoint once, and a completeness guard fails when a new `/api/v1` route is registered without being added to it. Adding a route to that table is a mandatory part of adding the endpoint.

## Files and jobs (v0.5)

Files and jobs are org-scoped resources under `/api/v1`, gated by the existing `documents.*` permission codes (ADR-0014) through the same `require_permission` dependency as every other tenant-scoped route. No new permission codes or roles exist for them; a generic `jobs.*` permission is deferred until a second job producer appears.

### Files

The files API implements the direct upload flow (blueprint §17): the backend issues a signed PUT URL, the browser uploads straight to storage, and completion verifies the stored object. The client never supplies an object key or a storage provider — request schemas use `extra="forbid"` so those fields are rejected.

| Method & path | Permission | Purpose | Notes |
| --- | --- | --- | --- |
| `POST /api/v1/files` | `documents.upload` | Upload intent: validates declared filename/content-type/size against `STORAGE_ALLOWED_CONTENT_TYPES` / `STORAGE_MAX_UPLOAD_SIZE` (rejected before any URL is issued), creates a `pending` record, returns `{file_id, upload_url, expires_at}` | `201` |
| `POST /api/v1/files/{file_id}/complete` | `documents.upload` | Verify the stored object (exists, size matches, checksum when supplied); `uploaded` + enqueue the processing job; returns `FileDetail` plus `processing_job_id` | mismatch → `failed`/`quarantined` + audit |
| `GET /api/v1/files` | `documents.read` | Paginated list, standard envelope, optional `status` filter; deleted files excluded by default | `?page=1&page_size=50&status=ready` |
| `GET /api/v1/files/{file_id}` | `documents.read` | File detail | cross-org id → `404` |
| `GET /api/v1/files/{file_id}/download-url` | `documents.read` | Short-lived signed GET URL | |
| `DELETE /api/v1/files/{file_id}` | `documents.delete` | Soft delete (`deleted_at`) + object removed from storage + `document.deleted` audit | `204` |

File statuses: `pending`, `uploaded`, `processing`, `ready`, `failed`, `quarantined`, `deleted`.

### Jobs

Jobs are durable records the client polls while background work runs; the file-processing job (`job_type="file.processing"`) drives progress 0→100.

| Method & path | Permission | Purpose | Notes |
| --- | --- | --- | --- |
| `GET /api/v1/jobs` | `documents.read` | Paginated list of the caller's organisation's jobs, standard envelope, `status` / `job_type` filters | |
| `GET /api/v1/jobs/{job_id}` | `documents.read` | Job detail: status + progress (0–100) | cross-org id → `404`; terminal states never re-run |

Job statuses: `queued`, `running`, `succeeded`, `failed`, `cancelled`.

## Notifications (v0.6)

Notifications are org-scoped resources under `/api/v1` (ADR-0016, blueprint §20), gated by the `notifications.read` / `notifications.manage` permission codes added to the catalogue in v0.6 (owner/administrator/manager: both; member: `read`; viewer: none — default-deny). All four routes are in the security suite's `PROTECTED_ROUTES` table.

| Method & path | Permission | Purpose | Notes |
| --- | --- | --- | --- |
| `GET /api/v1/notifications` | `notifications.read` | Paginated list of the **caller's own** notifications in the caller's organisation; standard envelope plus `unread_count`; optional `type` filter | `?page=1&page_size=50&type=file.ready` |
| `GET /api/v1/notifications/unread-count` | `notifications.read` | The caller's unread count (feeds the bell badge; poll-friendly) | |
| `PATCH /api/v1/notifications/{notification_id}/read` | `notifications.read` | Marks the notification read (`read_at`); idempotent | foreign or other-user id → `404` |
| `POST /api/v1/notifications/test` | `notifications.manage` | Creates an in-app notification for the caller and enqueues the email delivery job — the demonstrable "send a test notification" | `201`; audited |

Notification types: `file.ready`, `file.failed`, `notification.test_sent`. Deliveries run as durable jobs (`job_type="notification.email"`) — email is never sent from an HTTP handler. The unread count is also carried in the list envelope, so the bell can use either the count endpoint or the list response.
