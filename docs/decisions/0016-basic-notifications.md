# ADR 0016: Basic Notifications — Durable Delivery Jobs, Delivery Tracking, and `notifications.*` Permissions

Status: Accepted

## Context

The v0.6 release goal (Scope §1) includes "basic notifications" and a
demonstrable "send a test notification" (blueprint §45). The blueprint §20
defines `notifications` / `notification_deliveries` table shapes and says
email is sent through background jobs, but leaves the module shape, the
delivery-tracking contract and the permission model open.

Three questions had to be answered:

1. How does a notification reach the user — synchronous HTTP sends, or the
   durable job pipeline the template already ships?
2. How is per-channel delivery tracked so a retry cannot double-send?
3. Which permission codes gate the notifications endpoints — the existing
   `documents.*` set, a generic membership check, or new codes?

## Options considered

### 1. Delivery mechanism

- **Durable Dramatiq job per delivery (adopted)**: each email delivery runs
  as a `notification.email` job through the v0.5 job service with the durable
  record-then-enqueue lifecycle, bounded retries and idempotency that
  pipeline already owns (blueprint §18). In-app notifications need no job —
  they are written in the request transaction.
- **Send from the HTTP handler**: violates blueprint §20 ("email sent through
  background jobs") and ADR-0004; a slow or failing relay would block the
  request and lose the send on crash.

### 2. Delivery tracking

- **`notification_deliveries` row per notification/channel (adopted)**:
  queued → running → succeeded/failed with `provider_message_id`,
  `attempt_count`, `sent_at`, per the blueprint §20 shape plus the §10
  conventions (UUIDv7 ids, timestamps, naming, indexes). The task is
  idempotent on retry: it checks status/attempt before sending, so a retried
  job never double-sends.
- **Derive state from the provider's message id only**: loses the durable
  per-channel record and the audit trail the release contract requires.

### 3. Permission model

- **New `notifications.read` / `notifications.manage` codes (adopted)**: the
  permission catalogue is default-deny and every org-scoped endpoint is
  gated by `require_permission`; the security suite's viewer-write case
  requires an explicit code the viewer lacks. Role bundles: owner,
  administrator and manager get both codes; member gets `notifications.read`;
  viewer gets none (data migration updating `ROLE_PERMISSION_MAP`).
- **Gate "own notifications" on active membership alone**: every member can
  read their own notifications in their organisation, but then the
  test-send action has no permission seam, and the viewer-write case (viewer
  must be denied) becomes an implicit exception to the default-deny model
  rather than a catalogue fact.
- **Reuse `documents.*`**: semantically wrong — documents and notifications
  are different domains with different audiences.

## Decision

**Ship an org-scoped `modules/notifications/` module** (models / queries /
service / schemas / router / tasks, following the existing module pattern)
with:

- `notifications` and `notification_deliveries` tables via Alembic migration
  (blueprint §20 shape + §10 conventions), indexes on
  organisation/user/read_at.
- **Permission codes `notifications.read` and `notifications.manage`** added
  to the catalogue with a role-bundle data migration (owner/administrator/
  manager: both; member: read; viewer: none — default-deny unchanged). This
  is a permission-model change and is human-reviewed (blueprint §33,
  AGENTS.md).
- **API** (all org-scoped, all in the mandatory security suite's
  `PROTECTED_ROUTES`): `GET /api/v1/notifications` (own notifications in the
  caller's organisation, paginated, standard envelope carrying `unread_count`,
  type filter), `GET /api/v1/notifications/unread-count`,
  `PATCH /api/v1/notifications/{notification_id}/read` (sets `read_at`;
  foreign/other-user id → 404), and `POST /api/v1/notifications/test`
  (`notifications.manage`; creates an in-app notification for the caller and
  enqueues the email delivery — the demonstrable "send a test notification").
- **Production loop**: email deliveries run as durable jobs
  (`job_type="notification.email"`, `input_reference` = the delivery id)
  through the v0.5 job service; the task drives delivery
  queued → running → succeeded/failed, records `provider_message_id`, is
  idempotent on retry, and audits failures. The v0.5 `process_file` task is
  extended so a completed file (ready or failed) creates a `file.ready` /
  `file.failed` notification for the uploader and enqueues its email delivery
  — the files ↔ jobs ↔ notifications loop the release exists to demonstrate.

## Consequences

- Notifications behave like every other org-scoped resource: cross-
  organisation ids resolve to 404, viewer writes are denied, the whole
  surface is covered by the mandatory security suite, and test-send and
  delivery failures are audited.
- Email is sent only from worker tasks (ADR-0015, ADR-0004); a relay outage
  marks the delivery `failed` and the bounded retry policy governs retries
  without double-sends.
- The new permission codes are a deliberate, reviewed extension of the role
  model; they are seeded by a data migration so existing organisations
  inherit the bundles on upgrade.
- Real-time delivery (SSE), per-channel preferences and a transactional
  outbox remain deferred post-v1 (Scope §3) — the durable job pipeline is the
  delivery mechanism until then.

---
