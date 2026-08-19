# Durable Job Delivery Hardening Plan

Status: Active

## Goal

Harden the existing PostgreSQL + Redis + Dramatiq background-job system so an
accepted business job is durably scheduled, eventually published after a
temporary broker failure, protected against concurrent duplicate execution,
and automatically recovered when it remains queued without useful worker
progress. Retain the existing dedicated Python worker, job polling API,
organisation isolation, provider adapters and reference-only message boundary.

The resulting contract is at-least-once delivery with idempotent execution,
not an exactly-once claim. PostgreSQL is the source of truth for both the job
and the intent to publish it; Redis remains the transient execution broker.

## Agreed scope

- Retain Dramatiq, Redis, PostgreSQL and the dedicated Python worker. Do not
  introduce Supabase or replace the broker/worker stack.
- Add a generic `outbox_events` PostgreSQL table following blueprint §19. A
  durable job row and its `job.dispatch_requested` outbox event are written in
  one transaction; API request paths never publish durable jobs directly to
  Redis after the cutover.
- Keep broker messages reference-only. A durable job actor receives only
  `job_id`; actor selection and queue routing come from an internal,
  allow-listed registry keyed by the durable `job_type`, never from an
  importable function name or arbitrary payload stored in the database.
- Add a standalone coordinator process, using the existing backend image. It
  claims outbox rows in bounded batches, publishes to Dramatiq, settles
  publication state, retries temporary failures with capped exponential
  backoff, schedules maintenance events and reconciles stranded `queued` jobs.
- Make coordinator concurrency safe with short PostgreSQL transactions,
  `FOR UPDATE SKIP LOCKED`, claim tokens and expiring publication leases.
  Multiple coordinator replicas may run without deliberately publishing the
  same row; a crash after broker publication may still cause a duplicate.
- Add a per-job dispatch identity and bounded execution lease. Worker state
  transitions atomically claim one dispatch; a concurrent duplicate waits and
  retries rather than executing business work. Success, progress, permanent
  failure and retry release verify ownership so an expired/stale attempt cannot
  overwrite a newer attempt.
- Preserve the one-argument Dramatiq message contract (`job_id` only). New
  delivery identity remains in PostgreSQL, so existing queued messages and a
  rolling worker deployment remain compatible.
- Configure a standard Dramatiq task time limit and validate that the execution
  lease exceeds it by at least 60 seconds. Default values are a 600-second task
  limit and a 900-second execution lease.
- Reconcile only non-terminal `queued` jobs in this maintenance effort. A job
  still queued 900 seconds after its last published dispatch receives one new
  outbox event per 900-second cooldown. Atomic worker claims make the possible
  duplicate harmless and prevent concurrent execution.
- Schedule the existing `ai.retention` sweep every 24 hours and the existing
  provider-file reconciliation sweep every hour through deduplicated outbox
  events. Each maintenance handler takes a PostgreSQL advisory lock so
  duplicate publication cannot run the same sweep concurrently.
- Split email failures into retryable and permanent categories. Network,
  timeout, disconnect and SMTP 4xx failures retry; authentication failures,
  invalid/rejected recipients with only 5xx responses, and other SMTP 5xx
  failures fail permanently. Exhaustion leaves both the durable job and the
  notification-delivery row consistently failed.
- Add coordinator/job-delivery configuration, structured logs, database-backed
  backlog/age metrics, alerts, a guarded operator reconciliation command and
  failure-injection coverage using real PostgreSQL and Redis.
- Retain published outbox rows for 30 days for diagnosis, then delete them in
  bounded batches. Pending, publishing and dead rows are never automatically
  deleted. Permanently invalid events move to `dead` and require operator
  investigation; temporary infrastructure failures retry without a fixed
  attempt ceiling.
- Update the canonical blueprint, application architecture, ADRs, security,
  operations, backup/recovery, environment examples, README and task-writing
  guidance so the documented guarantees and limitations match the code.

## Findings and evidence

- `backend/app/modules/jobs/service.py:188-229` currently flushes a `queued`
  job, calls `Actor.send()` and then commits PostgreSQL. Redis publication and
  the database transaction cannot commit atomically, and the worker may see a
  message before its job row is visible.
- `docs/operations.md:153-176` and
  `docs/backup-and-recovery.md:371-391` explicitly state that wiping Redis
  loses queued messages while job rows survive and must be re-enqueued for
  continuity.
- `backend/app/modules/jobs/service.py:232-253` permits an already-running job
  to enter another attempt and has no execution owner or lease. That is useful
  for sequential retries but does not prevent concurrent duplicate delivery.
- `backend/app/modules/jobs/tasks.py:41-84` extracts only `job_id` from retry
  metadata and marks the row failed after exhaustion without verifying that it
  still owns the active attempt.
- `backend/app/modules/notifications/tasks.py:116-139` treats every
  `EmailSendError` as permanent. `backend/app/email/smtp.py:73-81` collapses
  network, authentication, 4xx and 5xx SMTP failures into that single type.
- `backend/app/ai/persistence/tasks.py` declares the retention and provider-file
  reconciliation actors, but no application-owned scheduler enqueues them.
- `backend/app/workers.py:38-54` registers every actor in one worker process;
  the current Makefile and Compose commands use one shared thread pool for all
  named queues.
- `Internal_Custom_Application_Starter_Architecture_v2.md:1224-1245` already
  requires a transactional outbox where missed delivery matters and states the
  intended boundary: Redis executes; PostgreSQL provides durability.
- Existing file, notification and AI actors already skip terminal jobs and
  contain domain-specific idempotency protections. Those behaviours are useful
  but do not replace atomic execution ownership.

## Out of scope

| Capability | Boundary |
| --- | --- |
| Supabase Queues, Supabase Cron or Edge Functions | explicitly excluded |
| Replacing Redis or Dramatiq | retain the accepted ADR-0004 stack |
| Separate worker pools per queue | defer until production load/backlog evidence justifies the operational cost |
| A general scanner that replays arbitrary stale `running` jobs | deferred; this plan only adds the execution lease required for message redelivery and duplicate exclusion |
| Public replay/cancel/admin endpoints or frontend job controls | operator CLI and runbook only; no API or permission surface changes |
| Exactly-once execution | impossible across database, broker and external providers; guarantee at-least-once delivery plus ownership and idempotency |
| DAGs, priorities, workflow orchestration or a worker dashboard | remain post-v1 concerns |
| New document-processing features | this plan hardens delivery of existing file, email, AI and maintenance work |
| Changes to authentication, roles or tenant permissions | existing job/file/notification/AI gates remain unchanged |
| Rewriting historical release contracts | `TEMPLATE_V0_5_SCOPE.md` and later scope files remain historical records; the new plan, blueprint amendment and ADR record the change |
| General Prometheus multiprocess aggregation | database-backed coordinator gauges are added to the API metrics surface; broader worker-counter aggregation remains separate work |

## Decisions and assumptions

- The architectural decision is settled: use a PostgreSQL transactional outbox
  in front of the existing Redis/Dramatiq worker system. A new ADR
  (`docs/decisions/0019-harden-dramatiq-delivery-with-an-outbox.md`) amends
  ADR-0004 rather than replacing Dramatiq.
- The runtime process is named `coordinator`. Its native command is
  `uv run python -m app.job_coordinator`, its Make target is `make coordinator`,
  and both deployment profiles use a `coordinator` service from the same
  backend image as the API and worker.
- `outbox_events` is internal infrastructure, not an API resource. Its schema
  contains UUIDv7 `id`, nullable `organisation_id`, `event_type`,
  `event_version`, `aggregate_type`, nullable `aggregate_id`, JSON payload,
  unique `deduplication_key`, status (`pending`, `publishing`, `published`,
  `dead`), `available_at`, `claimed_at`, nullable claim token, `processed_at`,
  `attempt_count`, bounded `last_error` and `created_at`, plus constraints and
  indexes for claim, reconciliation and retention queries.
- Job-dispatch outbox payloads contain only `job_id`; the event aggregate is the
  job and the event id becomes the job's current dispatch identity. Maintenance
  payloads contain no tenant data, object references, URLs, provider ids,
  prompts, document content or credentials.
- `jobs` gains nullable internal `dispatch_id` and
  `execution_lease_expires_at` columns. They are not exposed by `JobDetail` or
  list schemas. Existing non-terminal rows without a dispatch id are assigned
  one atomically when first claimed, preserving compatibility with broker
  messages present during deployment.
- Worker ownership is database-enforced. Claiming transitions `queued` to
  `running`, records/retains the current dispatch id, increments
  `attempt_count`, and sets the lease. A non-expired running lease causes the
  duplicate to retry after the remaining lease; an expired lease may be taken
  over. Every mutation verifies the captured dispatch id. Transient failures
  release the owned attempt back to `queued`; terminal settlement clears the
  lease. The retry-exhausted finalizer settles only the dispatch named by the
  current job row and treats superseded/terminal messages as stale no-ops.
- The standard actor time limit is 600,000 ms and the job execution lease is
  900 seconds. Startup validation rejects a lease shorter than task time limit
  plus 60 seconds. Progress updates renew the lease to protect active document
  work; non-progressing actors remain bounded by the task time limit.
- Coordinator defaults are: 50 rows per publication batch, 500 ms idle poll,
  60-second publisher claim lease, 1-second initial publication backoff,
  300-second maximum backoff, 900-second queued-job reconciliation threshold,
  900-second reconciliation cooldown, hourly provider-file reconciliation,
  daily AI retention, daily outbox cleanup and 30-day published-event
  retention. All values are typed settings with safe bounds and are documented
  in `.env.example`.
- The scheduler writes one outbox row per UTC schedule bucket using a stable
  unique deduplication key. Concurrent coordinators therefore converge on one
  scheduled event; advisory locks in the maintenance actors add a second
  execution-side exclusion boundary.
- Unknown event type/version, invalid payload shape, missing aggregate or
  registry mismatch is permanent and marks the outbox row `dead` with a safe
  bounded error. Redis/database unavailability and other infrastructure errors
  are transient and retry indefinitely with capped backoff.
- The allow-listed registry covers exactly the existing durable job types
  `file.processing`, `notification.email` and `ai.execute`, plus the existing
  `ai.retention` and provider-file reconciliation maintenance actors. Tests
  fail when a durable producer or registered actor is missing from the map.
- No endpoint, request schema, response schema, frontend query or generated API
  type changes. `make generate-client` must remain diff-free.
- No new third-party runtime dependency is required. SQLAlchemy, Dramatiq,
  PostgreSQL advisory locks and existing standard-library facilities are
  sufficient.
- The migration is additive and non-destructive. Rolling deployment order is:
  migrate, deploy backward-compatible workers, start the coordinator, then
  deploy API producers that write outbox events. Rollback pauses the
  coordinator before reverting application containers; queued job/outbox rows
  remain durable and the recovery command republishes them after roll-forward.

## Commands that must work

Existing commands remain green:

```bash
make migrate
make lint
make typecheck
make test
make e2e
make generate-client
make validate-execution-contracts
make check
```

P3 adds and documents the native coordinator command:

```bash
make coordinator
```

P5 adds guarded operational reconciliation commands. Inspection is the default;
publication intent is created only with the explicit confirmation variable:

```bash
make jobs-reconcile
CONFIRM_RECONCILE=1 make jobs-reconcile-apply
```

The real-broker failure tests run against local PostgreSQL and Redis and remain
part of the backend integration contract:

```bash
cd backend && uv run pytest tests/test_outbox_db.py tests/test_job_coordinator.py tests/test_jobs_broker.py tests/test_notifications_db.py
```

## Acceptance criteria

1. Creating any existing durable job writes the job and one pending
   `job.dispatch_requested` outbox event in the same PostgreSQL transaction. A
   rollback leaves neither row; unavailable Redis does not prevent the API from
   committing and returning the durable queued job.
2. No API/service producer calls a durable job actor's `send()` method. The
   coordinator is the only production component that turns durable outbox
   events into Dramatiq messages, and its allow-listed registry rejects unknown
   job/event types without importing names from database content.
3. One or more coordinators claim due events without blocking each other,
   publish only `job_id` for durable jobs, mark successful events published and
   retry temporary broker failures indefinitely with bounded backoff. A
   malformed/permanently unsupported event becomes `dead` with a safe error.
4. A coordinator crash after Redis accepts a message but before PostgreSQL
   records publication may cause a duplicate message, but two copies cannot run
   the business task concurrently. The losing copy waits/retries and becomes a
   no-op after terminal completion.
5. A genuine retry of the owning dispatch can run after transient failure; an
   expired attempt can be taken over after worker death; and a superseded
   attempt cannot update progress, succeed, fail or trigger an exhausted-retry
   failure over a newer owner.
6. `file.processing`, `notification.email` and `ai.execute` retain their current
   successful, permanent-failure, audit, tenant and idempotency behaviours under
   the ownership contract. The public job lifecycle and polling schemas remain
   `queued → running → succeeded|failed` with progress 0–100.
7. A job still queued 900 seconds after its last published dispatch is given a
   new outbox dispatch no more than once per 900-second cooldown. Terminal jobs,
   actively leased jobs and jobs with pending/publishing events are never
   reconciled.
8. SMTP/network/timeout/disconnect and SMTP 4xx failures leave the notification
   delivery retryable and use bounded Dramatiq retries. Authentication,
   recipient-only 5xx and other SMTP 5xx failures fail immediately. Retry
   exhaustion leaves the job and delivery failed exactly once with safe public
   errors and an audit record.
9. The coordinator durably schedules the AI retention task daily and the
   provider-file reconciliation task hourly. Duplicate schedule ticks create
   one outbox event per UTC bucket, and duplicate messages cannot execute the
   same maintenance sweep concurrently.
10. `/metrics` exposes low-cardinality, database-backed gauges for outbox rows
    by status/event type, oldest due-event age and stale queued-job count. Logs
    bind outbox/dispatch/job identifiers without payload content, and the
    operations guide defines warning/critical thresholds and response steps.
11. Operators can inspect reconciliation candidates without mutation and can
    explicitly create deduplicated recovery events through the guarded apply
    command. No public replay endpoint, new permission or cross-organisation
    data surface is introduced.
12. A real PostgreSQL/Redis failure suite proves broker-down enqueue, later
    publication, publisher crash duplication, worker claim contention, worker
    death/lease takeover, Redis-volume-loss queued reconciliation, email
    transient recovery and permanent failure without external providers.
13. Published outbox cleanup deletes only rows older than 30 days in bounded
    batches; pending, publishing and dead rows survive cleanup and database
    backup/restore. Backup and rollback instructions explain how job and outbox
    records recover execution after Redis loss.
14. The blueprint, ADRs, architecture, API conventions, security, operations,
    backup/recovery, README, environment examples, Makefile help and code
    docstrings consistently describe PostgreSQL as the scheduling source of
    truth, Redis as transient execution transport, at-least-once delivery and
    the limits of queued versus running recovery.
15. `make check`, `make e2e`, the focused real-infrastructure tests and the
    execution-contract validator pass without weakening lint, typing, security
    tests or generated-client drift checks.

### Capability traceability

| Observable requirement | Acceptance | Checkpoint | Consumer/operation | Required evidence |
| --- | --- | --- | --- | --- |
| Atomic durable scheduling | AC1–AC2 | P1, P3 | file completion, notification creation, async AI classification | PostgreSQL transaction/rollback tests and structural no-direct-send test |
| Safe duplicate and retry execution | AC4–AC6 | P2 | all durable Dramatiq actors | concurrent duplicate, transient retry, lease expiry and stale-owner tests |
| Reliable outbox publication | AC2–AC3 | P3 | `make coordinator`, Compose `coordinator` | multi-publisher, Redis outage/recovery and crash-window tests |
| Automatic queued-job recovery | AC7, AC11–AC13 | P4, P5 | coordinator reconciliation and guarded CLI | threshold/cooldown, dry-run/apply and Redis-loss tests |
| Correct email retry behaviour | AC8 | P4 | `notification.email` | SMTP taxonomy, eventual success, permanent failure and exhaustion tests |
| Reliable maintenance scheduling | AC9 | P4 | AI retention and provider-file reconciliation actors | UTC-bucket deduplication and advisory-lock tests |
| Operational visibility and recovery | AC10–AC13 | P5 | `/metrics`, logs, commands and runbooks | metric values, safe-log assertions, failure-injection and cleanup tests |
| Accurate architecture contract | AC14 | P1, P6 | contributors and operators | ADR/blueprint/doc consistency review and stale-claim searches |
| No public-contract regression | AC6, AC11, AC15 | P2–P6 | existing APIs/frontend | API/security suites, generated-client drift check, `make check`, `make e2e` |

## Implementation checkpoints

### P1 — Outbox Decision and Durable Data Contract

Dependencies: none

- [x] Add `docs/decisions/0019-harden-dramatiq-delivery-with-an-outbox.md`
  recording the retained Dramatiq/Redis decision, rejected alternatives,
  PostgreSQL/Redis responsibilities, at-least-once guarantee, coordinator,
  execution ownership, rollout/rollback and absence of new dependencies; amend
  ADR-0004 with a clear pointer to the new decision.
- [x] Add `app/modules/outbox/` with ORM status/model, strict internal event
  payload contracts, query helpers and service boundaries matching existing
  module patterns; keep all complex claim/reconciliation SQL in `queries.py`
  and prohibit arbitrary actor/function names in persisted payloads.
- [x] Extend the `Job` persistence model with internal dispatch identity and
  execution-lease fields, including database constraints/indexes that support
  ownership and queued reconciliation without altering API schemas.
- [x] Add one additive Alembic migration creating `outbox_events` and the job
  columns, with upgrade/downgrade coverage, UUID/check/status constraints,
  unique deduplication keys and indexes for due claims, aggregate history,
  stale claim recovery and published retention.
- [x] Add database tests proving job + outbox atomic commit/rollback, tenant
  association, maintenance-event null-organisation rules, deduplication,
  payload bounds, state constraints and migration upgrade/downgrade; confirm
  the generated OpenAPI client is unchanged.

Human review required before application: none; the migration is additive and non-destructive.

### P2 — Atomic Worker Ownership and Backward-Compatible Delivery

Dependencies: P1

- [x] Replace permissive `mark_running` behaviour with atomic claim, lease
  renewal, transient release and owner-checked progress/success/failure helpers.
  Existing messages still carry only `job_id`; a non-terminal legacy row with
  no dispatch id receives one atomically on first claim.
- [x] Add a shared durable-actor execution wrapper that captures the dispatch
  owner, defers a duplicate until its active lease expires, releases ownership
  before propagating a transient error, preserves permanent-failure semantics
  and prevents a stale attempt from settling a newer owner.
- [x] Apply the wrapper/owner token contract to file processing, notification
  email and AI execution while preserving each domain service, progress,
  audit, provider and terminal-idempotency boundary; update the retries-
  exhausted actor to settle only the currently owned dispatch.
- [x] Put the 600,000 ms task time limit into the shared retry policy, add the
  900-second execution-lease setting and startup validation, renew leases on
  progress, and add bounded safe structured logs for claimed, deferred,
  released, taken-over and stale-settlement outcomes.
- [x] Add unit and real-database tests for simultaneous duplicate claims,
  sequential retry, transient release, lease renewal/expiry takeover,
  terminal duplicates, stale-owner progress/success/failure, exhausted stale
  messages and old one-argument broker messages; keep existing file/email/AI
  lifecycle tests green.

Human review required before application: none.

### P3 — Transactional Scheduling and Coordinator Process

Dependencies: P1, P2

- [x] Add a typed, allow-listed dispatch registry for the three durable job
  types and two maintenance events. Validate completeness at startup and in
  tests; registry handlers build only the existing actor message shapes and
  never resolve code from persisted strings.
- [x] Replace `create_and_enqueue` with a transaction-owned scheduling service
  that writes the job and `job.dispatch_requested` event together, sets the
  event id as the job dispatch id and commits once. Migrate every file,
  notification and async-AI producer and remove durable `Actor.send()` calls
  from API/service paths.
- [x] Implement `app.job_coordinator`: bounded due-row claims using
  `FOR UPDATE SKIP LOCKED`, claim-token guarded settlement, expired-claim
  recovery, capped exponential retry with jitter, permanent dead-event
  handling, graceful shutdown and structured logging. Publishing happens
  outside the row-lock transaction; crash-window duplicates are expected and
  handled by P2 ownership.
- [x] Add all typed coordinator/reconciliation/schedule/retention settings and
  validators to `app.core.config.Settings` and `.env.example` using the settled
  defaults in this plan; tests cover bounds and the task-time/lease
  relationship.
- [x] Add `make coordinator`, run it alongside API/worker/frontend in
  `scripts/dev.sh`, and add the same-backend-image `coordinator` service with
  liveness check, resource/log limits, dependency ordering and graceful stop to
  both Compose profiles and deployment validation.
- [x] Add structural, unit, PostgreSQL and real-Redis tests proving no producer
  publishes directly, registry coverage, two-coordinator claim safety,
  broker-down retry, recovery publication, invalid-event death, crash-after-
  send duplication and graceful restart without lost pending intent.

Human review required before application: infrastructure changes (new always-on coordinator process and deployment wiring).

Human infrastructure approval recorded: Daniel approved the coordinator
command, resource/log limits, liveness probe, dependency ordering, graceful
stop and rollout order on 2026-08-19.

### P4 — Reconciliation, Maintenance Scheduling and Email Retries

Dependencies: P3

- [ ] Implement bounded queued-job reconciliation queries and service logic:
  select only jobs beyond the 900-second threshold, exclude terminal/running
  jobs and those with pending/publishing dispatches, create a new deduplicated
  dispatch event and job dispatch id atomically, and enforce the 900-second
  cooldown under concurrent coordinators.
- [ ] Add coordinator UTC-bucket scheduling for daily AI retention and hourly
  provider-file reconciliation through outbox events; use unique schedule keys
  plus handler-side PostgreSQL advisory locks, and prove duplicate ticks or
  messages cannot run the same maintenance sweep concurrently.
- [ ] Replace the single email exception with provider-neutral transient and
  permanent subclasses while preserving `EmailSendError` as their common API;
  classify SMTP network/timeouts/disconnects and 4xx responses as transient,
  and authentication, recipient-only 5xx and other 5xx responses as permanent.
- [ ] Update the notification actor/delivery service so transient failures
  return the delivery to `queued` before Dramatiq retry, permanent failures
  settle immediately, and retry exhaustion marks both job and delivery failed
  exactly once through an allow-listed job-type exhaustion hook.
- [ ] Add deterministic fake/SMTP exception tests, delivery/audit database
  tests, coordinator scheduling/reconciliation concurrency tests and real-
  broker eventual-success/exhaustion journeys. Assert safe error strings do not
  expose SMTP responses, credentials, recipients or provider internals.

Human review required before application: none.

### P5 — Observability, Operator Recovery and Failure Injection

Dependencies: P3, P4

- [ ] Extend the API metrics refresh path with database-backed, low-cardinality
  gauges for outbox rows by status/event type, oldest due-event age and stale
  queued-job count. Add rate-limited outage/recovery logging and tests proving
  organisation/job ids, payloads and errors never become metric labels.
- [ ] Add `backend/scripts/reconcile_jobs.py`, `make jobs-reconcile` (read-only)
  and a `CONFIRM_RECONCILE=1` guarded apply target. Both paths use the same
  bounded reconciliation service as the coordinator, print counts/opaque ids
  rather than content, and make repeated application idempotent.
- [ ] Implement daily bounded cleanup for published outbox events older than 30
  days; prove pending, publishing, dead and newer published events cannot be
  selected, and log only aggregate cleanup counts.
- [ ] Add alerts and operator checks for oldest pending age, dead events, stale
  queued jobs, coordinator liveness, reconciliation growth and publication
  recovery. Define warning/critical thresholds and exact inspect/reconcile/
  restart/escalate steps.
- [ ] Add a real-infrastructure failure suite that stops/unavailable-stubs
  Redis at controlled boundaries and proves: API commit while broker is down,
  later publication, publisher crash duplication without concurrent work,
  worker lease takeover, simulated empty broker queued recovery, email retry
  recovery/exhaustion and persistence of actionable rows across restart.
- [ ] Verify existing protected job/file/notification/AI routes and frontend
  polling remain unchanged, `PROTECTED_ROUTES` stays complete, no new API
  security cases are required, and generated client output is diff-free.

Human review required before application: backup and recovery changes, plus operational application of the guarded recovery command in any production environment.

### P6 — Architecture and Documentation Closure

Dependencies: P1, P2, P3, P4, P5

- [ ] Update `Internal_Custom_Application_Starter_Architecture_v2.md` §§18–19,
  §28 and §§35–36 with the job → outbox → coordinator → Redis → worker flow,
  schemas, ownership/lease rules, maintenance scheduling, new process and
  explicit at-least-once/queued-recovery limits; remove the inaccurate direct
  record-then-enqueue guarantee.
- [ ] Update `ARCHITECTURE.md`, `API_CONVENTIONS.md`, `SECURITY.md`, `README.md`,
  `.env.example`, Makefile help, module docstrings and relevant ADR-0007/0008
  deployment/local-development descriptions so contributor and application
  guidance consistently matches the implemented coordinator/outbox system.
- [ ] Update `docs/operations.md` with process topology, scaling, settings,
  health, metrics, alert thresholds, scheduled maintenance, dead-event triage,
  reconciliation, published cleanup and deployment/rollback runbooks.
- [ ] Update `docs/backup-and-recovery.md` with PostgreSQL outbox backup/restore,
  Redis-loss automatic queued recovery, running-job limitations, coordinator
  restart order, rollback procedure and guarded operator recovery; remove the
  statement that continuity requires unspecified manual re-enqueueing.
- [ ] Add a checked task-authoring section documenting registry registration,
  reference-only payloads, atomic scheduling, execution ownership, transient
  release, permanent failure, exhaustion hooks, idempotency and tests required
  for every future durable actor.
- [ ] Search for and correct stale claims that the API publishes jobs directly,
  Redis is durable application state, retries provide exactly-once behaviour,
  or all provider/email failures are permanent. Run every required command and
  record infrastructure and backup/recovery human-review approval before the
  final checkpoint is applied.

Human review required before application: infrastructure changes and backup and recovery changes must have recorded human approval before documentation is treated as the deployed contract.

## Reference map

| Checkpoint | Governing sources | What to extract |
| --- | --- | --- |
| P1 | `Internal_Custom_Application_Starter_Architecture_v2.md:1083-1245`; `backend/app/modules/jobs/models.py:1-135`; `backend/app/modules/jobs/service.py:188-253`; existing `backend/alembic/versions/` | Background-job/outbox rules, durable job shape, current dual-write/claim behaviour, model and migration conventions |
| P2 | `Internal_Custom_Application_Starter_Architecture_v2.md:1140-1192`; `backend/app/modules/jobs/service.py:51-96,232-353`; `backend/app/modules/jobs/tasks.py:41-92`; `backend/app/modules/files/tasks.py`; `backend/app/modules/notifications/tasks.py`; `backend/app/ai/execution.py` | Retry policy, terminal/idempotency rules, worker helper boundaries, actor-specific progress/failure/audit behaviour |
| P3 | `Internal_Custom_Application_Starter_Architecture_v2.md:1224-1245,2122-2277`; `backend/app/broker.py:22-50`; `backend/app/workers.py:38-54`; `Makefile:31-83`; `scripts/dev.sh`; `deploy/compose/compose.local.yml:153-193`; `deploy/compose/compose.hybrid-vps.yml:129-178` | PostgreSQL durability boundary, broker factory, task registration, one-image/multiple-command convention, native and container process wiring |
| P4 | `Internal_Custom_Application_Starter_Architecture_v2.md:1140-1158,1249-1285`; `backend/app/email/base.py:19-51`; `backend/app/email/smtp.py:56-85`; `backend/app/modules/notifications/tasks.py:65-180`; `backend/app/ai/persistence/tasks.py` | Transient/permanent retries, email-only-in-workers rule, current collapsed SMTP error handling, maintenance actor contracts |
| P5 | `Internal_Custom_Application_Starter_Architecture_v2.md:1674-1695,1884-1953`; `backend/app/observability/metrics.py:45-164,307-330`; `docs/operations.md:90-176,220-289`; `docs/backup-and-recovery.md:371-391`; `backend/tests/test_jobs_broker.py` | Metrics/logging constraints, integration-test priority, existing queue alerts and Redis-loss semantics, real-broker test pattern |
| P6 | `Internal_Custom_Application_Starter_Architecture_v2.md:1083-1245,1674-1695,2122-2277`; `ARCHITECTURE.md:290-322`; `API_CONVENTIONS.md:176-217`; `SECURITY.md:135-169`; `README.md:115-162`; `docs/decisions/0004-use-dramatiq.md`; `docs/decisions/0007-two-deployment-profiles.md`; `docs/decisions/0008-local-development-model.md`; `docs/operations.md`; `docs/backup-and-recovery.md`; `.env.example:157-186` | Canonical architecture, public API non-change, security/privacy limits, developer commands, deployment/operations/recovery documentation that must be made consistent |

## API, data and security impact

- **API/frontend:** no new or changed endpoint, permission, request body,
  response schema or frontend consumer. `POST /api/v1/files/{file_id}/complete`,
  `POST /api/v1/notifications/test`, async `POST /api/v1/ai/classify`,
  `GET /api/v1/jobs` and `GET /api/v1/jobs/{job_id}` retain their contracts.
  The generated client must remain unchanged.
- **Database:** one additive Alembic migration creates `outbox_events` and adds
  internal dispatch/lease columns to `jobs`. Outbox claims, reconciliation and
  cleanup are service/query concerns. No destructive migration or business-row
  deletion is introduced; only published transient outbox records cross the
  documented 30-day cleanup boundary.
- **Tenant isolation:** every durable job event copies the validated job
  organisation id and the registry verifies event/job consistency before
  publication. Global maintenance events require null organisation and have no
  tenant payload. Outbox tables are internal and receive no API route.
- **Message/data minimisation:** Redis messages and job-dispatch outbox payloads
  contain only job ids. They contain no file bytes, document text, prompts,
  signed URLs, object keys, recipients, credentials or provider responses.
- **Execution safety:** at-least-once delivery is made safe through database
  ownership, leases, owner-checked settlement and existing domain idempotency.
  Lease expiry permits takeover, so external side effects still require their
  existing provider/delivery deduplication rules; the plan does not claim
  globally exactly-once effects.
- **Secrets/logging:** no new secret is introduced. Errors stored on outbox rows
  are bounded and sanitised; logs and metrics use low-cardinality event/job
  types and opaque ids, never payload bodies or sensitive references.
- **Human review:** P3 changes infrastructure by adding an always-on process.
  P5/P6 change backup/recovery and operational recovery. Prompt 03 must stop at
  those checkpoints until the named human review is recorded.

## Validation plan

- **Pure/unit tests:** event/payload validation, registry completeness, retry
  classification, backoff/jitter bounds, schedule bucket keys, lease timing,
  configuration validation and safe error/log formatting.
- **PostgreSQL integration tests:** migration upgrade/downgrade, atomic
  job+outbox rollback, `SKIP LOCKED` claims, claim expiry, event deduplication,
  execution ownership, stale-owner rejection, queued reconciliation,
  maintenance locks and published cleanup.
- **Redis/worker integration tests:** broker unavailable at scheduling time,
  later coordinator publication, crash-window duplicates, real worker
  contention, retry/exhaustion, lease takeover and empty-broker recovery.
- **Domain regression tests:** file ready/failed and notifications, email
  delivery/audit, AI execution/replay/budget persistence and terminal job
  polling remain green with the new scheduling boundary.
- **Security/contract tests:** structural no-direct-send/no-arbitrary-import
  checks, payload and log never-content assertions, mandatory protected-route
  suite, no new public route and diff-free generated OpenAPI client.
- **Operational tests:** native coordinator start/stop, fullstack Compose
  liveness, multi-coordinator safety, dry-run/apply recovery commands, metrics
  during healthy/backlogged/dead-event states and documented rollback order.
- **Final gates:** run the focused command from `Commands that must work`, then
  `make lint`, `make typecheck`, `make test`, `make e2e`,
  `make generate-client`, `make validate-execution-contracts` and `make check`.
  Do not weaken or exclude tests to obtain a pass.

## Review and delivery

- Execute P1 through P6 in order. Each checkpoint is one independent
  implement → review → apply-and-commit cycle on its own `feature/<checkpoint>`
  branch, following `CONTRIBUTING.md`.
- Do not check boxes or commit until a reviewer has inspected the checkpoint
  diff and its validation evidence. Keep unrelated existing worktree changes
  out of every checkpoint.
- P3 stops before application until infrastructure human review approves the
  coordinator service, commands, resource limits and deployment order.
- P5 stops before applying production recovery/cleanup behaviour until backup
  and recovery human review approves Redis-loss, restore, retention and guarded
  reconciliation procedures. P6 records those approvals in the final docs.
- The rollout is migration first, backward-compatible workers second,
  coordinator third and outbox-producing API last. Observe pending age, dead
  events, queue depth and stale jobs before and after cutover. Rollback pauses
  the coordinator before reverting application containers and preserves all
  PostgreSQL rows for roll-forward recovery.
- No new dependency, public API, auth, permission or tenant-isolation change is
  authorised by this plan. Discovery of such a requirement returns the plan to
  `Status: Draft` for an explicit decision and the corresponding human-review
  gate.
- Completion requires all acceptance evidence, green commands, reviewed and
  applied checkpoints, checked boxes, consistent documentation and the exact
  status transition from `Status: Active` to `Status: Complete`.
