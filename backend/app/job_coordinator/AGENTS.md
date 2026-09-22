# Durable Jobs, Outbox & Coordinator — Agent Guide

Read this before changing durable execution anywhere: `app/modules/jobs/`,
`app/modules/outbox/`, `app/job_coordinator/`, `app/modules/maintenance/`,
`app/workers.py`, `app/broker.py`, or a domain module's `tasks.py`. It is a short
orientation; the design authorities are ADR-0019
(`docs/decisions/0019-harden-dramatiq-delivery-with-an-outbox.md`),
`ARCHITECTURE.md` ("Durable jobs, outbox and coordinator"), `docs/operations.md`,
and `app/db/AGENTS.md` for the RLS/coordinator-role side. Keep it current when a
lifecycle rule changes.

## The pieces

| Path | Role |
| --- | --- |
| `app/modules/jobs/models.py` | `Job`, `JobAttempt`, `JobStatus`, `JobAttemptStatus` |
| `app/modules/jobs/service.py` | `schedule_job`, `claim_dispatch`, `settle_retryable_failure`, `succeed`, `fail`, `verify_ownership`, `settle_after_retries_exhausted`, `retry_policy`, `register_exhaustion_hook` |
| `app/modules/jobs/execution.py` | `run_claimed`, `DurableJobContext`, ownership/deferral/stale handling |
| `app/modules/jobs/queries.py`, `router.py`, `schemas.py` | org-scoped list/detail (`GET /api/v1/jobs`); internal delivery fields are not exposed |
| `app/modules/outbox/` | `OutboxEvent` + statuses, `create_dispatch_event`/`create_maintenance_event`, closed Pydantic payload contracts, claim/retention SQL |
| `app/job_coordinator/loop.py` | `run_cycle`: reclaim stale claims, claim due events, publish, owner-checked settle |
| `app/job_coordinator/reconciliation.py` | queued/running recovery, maintenance scheduling/recovery, published-event cleanup |
| `app/job_coordinator/registry.py` | allow-listed `DispatchRegistry`, `DURABLE_JOB_TYPES`, `build_default_registry` |
| `app/modules/maintenance/` | `MaintenanceRun` lifecycle, advisory lock, heartbeat |
| `app/workers.py`, `app/broker.py` | Dramatiq entrypoint and broker/middleware factory |
| `app/db/session.py`, `app/db/role_checks.py` | coordinator engine and startup role gates |

Domain actors live with their feature module (`app/modules/files/tasks.py`,
`app/modules/notifications/tasks.py`, `app/ai/execution.py`); there is no
`app/workers/` package.

## Lifecycle

1. **Schedule** — `jobs_service.schedule_job` writes the `queued` job and a
   `job.dispatch_requested` outbox event in one transaction; the event id is the
   job's `dispatch_id`. Producers never call `Actor.send()`.
2. **Dispatch** — the coordinator claims due `pending` events with
   `FOR UPDATE SKIP LOCKED`, publishes outside the lock through the registry, then
   settles owner-checked with a conditional UPDATE.
3. **Broker** — messages are reference-only: `{"job_id": ...}` (or
   `maintenance_run_id`). Actor selection/queue is the checked-in registry, never
   a persisted string.
4. **Claim** — the task calls `run_claimed` → `claim_dispatch`; outcome is
   `CLAIMED`, `DEFERRED`, `EXHAUSTED` or `STALE`. Claim sets `running`, increments
   `attempt_count`, rotates `owner_token`, sets the lease, inserts a `JobAttempt`;
   an expired lease is taken over and the old attempt marked `abandoned`.
5. **Handler** — runs with `DurableJobContext`; all mutations go through
   owner-checked helpers.
6. **Settlement** — a retryable failure closes the attempt, writes a delayed
   retry dispatch and returns the job to `queued`; at the ceiling the job is
   `failed` with `job_retries_exhausted`, the exhaustion hook runs, and the lease
   is cleared. PostgreSQL owns all new retry decisions; the Dramatiq
   `on_retry_exhausted` actor is a rolling-deploy bridge only.

## Adding a durable job type

1. Define a stable `JOB_TYPE_*` constant in the owning task module.
2. Register the actor in `build_default_registry` **and** add the type to
   `DURABLE_JOB_TYPES` (`app/job_coordinator/registry.py`). Declare the actor with
   `**jobs_service.retry_policy()`; registry validation rejects a missing/unknown
   entry.
3. Schedule only via `jobs_service.schedule_job`; `tests/test_dispatch_registry.py`
   scans producers for direct `Actor.send()`.
4. Task signature accepts only `job_id`; do domain work through `run_claimed` and
   `DurableJobContext`; for permanent errors fail the durable row then raise
   `JobPermanentError`.
5. Guard replay/idempotency (terminal no-op, `find_job_by_input_reference`
   completion replay, delivery dedup, wrong-job-type rejection).
6. Optionally register an exhaustion hook with
   `jobs_service.register_exhaustion_hook`.

## Tenant context, the coordinator role and no bypass

- The broker carries only an opaque `job_id`. The worker binds transaction-local
  `app.job_id`, reads its one `jobs` row (single-row `SELECT` policy),
  **clears `app.job_id`**, then binds `app.organisation_id` from the durable row —
  never from the broker. Because context is transaction-local, rebind after every
  commit.
- `app_coordinator` (`DATABASE_COORDINATOR_URL`) is a non-owner, `NOBYPASSRLS`,
  `NOINHERIT` role whose policies are scoped to dispatch state, not a tenant, with
  **column-level** UPDATE grants on `jobs`/`job_attempts`/`outbox_events`. It can
  settle but cannot move a tenant key or rewrite payloads/identity/progress.
- `app_operator` (the only `BYPASSRLS` role) is never loaded by a worker or the
  coordinator. Production startup gates reject owner/superuser/`BYPASSRLS`
  credentials (`app/db/role_checks.py`).

## Testing

- Unit (no DB/Redis): `test_jobs.py`, `test_outbox.py`,
  `test_dispatch_registry.py`, `test_reconcile_jobs_cli.py`.
- Real PostgreSQL: `test_jobs_db.py`, `test_outbox_db.py`,
  `test_maintenance_db.py`, `test_jobs_api.py`, `test_rls_jobs_enablement_db.py`,
  `test_rls_operational_ledgers_enablement_db.py`.
- Real Redis/worker: `test_jobs_broker.py`, `test_maintenance_broker.py`,
  `test_job_coordinator.py`.
- All skip when their service is unreachable; CI provides PostgreSQL 17 and Redis.

## Gotchas

- **At-least-once, not exactly-once:** a crash between publish and settle
  duplicates a message; leases/ownership make the loser a no-op. Dead outbox
  events are never auto-replayed.
- **`SELECT ... FOR UPDATE` is also governed by the UPDATE policy**, which is why
  the worker bootstrap is `FOR SELECT` only and the row lock runs under tenant
  context.
- **The coordinator may not be granted a bypass.** New cross-tenant coordinator
  work needs a dispatch-state policy or a narrow aggregate, not `BYPASSRLS`.
- The notification-exhaustion hook finalizes a user-private delivery row from the
  coordinator's settlement transaction; it binds the durable row's organisation
  and recipient and relies on the existing user-private policies.
- `outbox_events.available_at`/`created_at` use Python-side UTC defaults
  (`INSERT ... RETURNING` would need a denied SELECT); hosts and DB must be
  NTP-synchronised.
- Never clear Redis to fix a backlog — the PostgreSQL outbox intent is durable and
  the coordinator recovers stranded work. Prefer
  `python -m scripts.reconcile_jobs` (read-only; `--apply` needs
  `CONFIRM_RECONCILE=1`).
- The broker must be installed before `build_default_registry`; the
  `app.job_coordinator` package import is side-effect free.

## Key files

- `app/modules/jobs/service.py`, `execution.py`, `models.py`
- `app/job_coordinator/loop.py`, `reconciliation.py`, `registry.py`
- `app/modules/outbox/contracts.py`, `app/modules/maintenance/execution.py`
- `app/workers.py`, `app/broker.py`, `scripts/reconcile_jobs.py`
- `ARCHITECTURE.md` §"Durable jobs, outbox and coordinator"; `docs/operations.md`
