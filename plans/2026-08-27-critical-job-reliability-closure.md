# Critical Job Reliability Closure Plan

Status: Draft

## Goal

Close the remaining failure modes in the PostgreSQL + Redis + Dramatiq job
system that can leave accepted work permanently stranded, lose the durable
record of retry exhaustion, allow a superseded worker to mutate business state,
repeat an externally visible email after an ambiguous send, or report healthy
queue delivery while the production metric is non-functional.

Retain the existing transactional outbox, coordinator, reference-only broker
messages, durable job API, domain services and at-least-once execution model.
PostgreSQL remains the scheduling and audit source of truth. Redis remains
transient transport and must be configured to reject writes visibly rather
than silently evict broker state.

The resulting contract is not exactly-once execution. It is:

- every accepted durable job has a PostgreSQL-owned path to either eventual
  execution or a durable terminal/attention-required outcome;
- retry exhaustion and maintenance-run outcomes do not depend on a final
  best-effort Redis message;
- an expired or superseded attempt is fenced out of job and domain mutations;
- ambiguous external acceptance is recorded honestly instead of being treated
  as definitely unsent;
- broker pressure and queue backlog are observable using the locked production
  Dramatiq/Redis implementation.

## Agreed scope

- Replace the durable actors' broker-only retry-exhaustion finalizer with a
  PostgreSQL-owned attempt/finalization contract. A transient domain failure
  either writes a durable next-dispatch intent or settles the job terminally in
  the same transaction that closes the attempt. Dramatiq middleware retries may
  remain as transport assistance for failures before a durable claim, but they
  are never the only path to a terminal job record.
- Add a durable `job_attempts` ledger sufficient to audit claims,
  takeovers, retryable outcomes, success, permanent failure, exhaustion and
  abandonment. Store only opaque identifiers, timestamps, stable outcome/error
  codes and bounded operational metadata; never payloads, recipients, content,
  provider responses, URLs or credentials.
- Extend coordinator reconciliation to recover execution-lease-expired
  `running` jobs as well as stranded `queued` jobs. Recovery is bounded,
  cooldown-limited and owner-fenced; it creates a new durable dispatch intent
  in PostgreSQL rather than publishing directly to Redis.
- Fence every durable actor's business-state transitions against its captured
  job owner. File, notification-delivery and AI execution paths must revalidate
  ownership in the same transaction as consequential domain mutations. A stale
  worker may finish local computation, but it cannot commit business state over
  a newer attempt.
- Make email delivery ambiguity explicit. Persist one stable delivery identity
  before the first provider call; distinguish definitely-not-accepted transient
  failures from permanent rejection and acceptance-unknown failures. An
  acceptance-unknown SMTP outcome is durably recorded for operator attention
  and is not automatically resent.
- Add a durable global maintenance-run ledger for `ai.retention` and
  `ai.transfer_reconcile`. Scheduling, claim/lease, completion, failure,
  retry/exhaustion and duplicate suppression become PostgreSQL-visible.
  Maintenance broker messages remain reference-only and carry only the durable
  maintenance-run id.
- When the current initial job dispatch becomes permanently `dead`, settle the
  associated job to a safe internal delivery failure/attention-required outcome
  in the same owner-checked transaction. No accepted job may remain indefinitely
  `queued` behind an unrecoverable dead initial event.
- Separate broker Redis from rate-limit Redis in the production deployment
  contract. The broker instance uses `noeviction` and dedicated persistence;
  rate-limit storage may retain an eviction policy appropriate to counters.
  Production startup rejects an accidental shared endpoint.
- Replace the incompatible queue-depth implementation with a version-locked,
  real-Redis-tested observability adapter. Expose ready, delayed/in-flight where
  reliably available, and dead-letter/failed-message signals without inspecting
  message payloads.
- Update architecture, ADRs, operations and backup/recovery documentation so
  guarantees, ambiguous outcomes, Redis topology and operator actions match the
  implementation exactly.

## Findings and evidence

- `backend/app/modules/outbox/queries.py:158-204` selects only `queued` jobs for
  reconciliation. An execution-lease-expired `running` job can be taken over
  only if another broker message happens to arrive; Redis loss plus worker death
  can otherwise strand it indefinitely.
- `backend/app/modules/jobs/execution.py:181-217` releases a transiently failing
  attempt back to `queued` before propagating the exception, while
  `backend/app/modules/jobs/tasks.py:144-149` declares the only exhausted-retry
  finalizer with `max_retries=0`. Loss or failure of that Redis message leaves
  no durable terminal settlement and permits later queued reconciliation to
  start another nominal retry budget.
- `backend/app/modules/jobs/service.py:383-443` fences the `jobs` row with an
  owner token, but `backend/app/modules/files/service.py:422-531` and
  `backend/app/modules/notifications/service.py:411-510` commit domain state
  without that token. A superseded attempt can therefore reach business-state
  mutations before its later job settlement detects staleness.
- `backend/app/modules/notifications/tasks.py:142-185` calls the external email
  provider before recording success. A provider can accept the message and the
  worker can die or lose the connection before the database commit.
  `backend/app/email/smtp.py:69-108` generates a new Message-ID per attempt and
  currently treats disconnect/network cases as definitely retryable, so an
  ambiguous accepted send can be repeated.
- `backend/app/job_coordinator/reconciliation.py:125-152` durably schedules
  maintenance publication, but `backend/app/ai/persistence/tasks.py:111-121`
  runs maintenance as ordinary actors with broker-only retries and no durable
  per-run success/failure record. A published outbox event proves enqueue, not
  completion.
- `backend/app/job_coordinator/loop.py:236-253` makes invalid current dispatches
  dead, while queued reconciliation requires a previously published event. An
  initial dead dispatch can therefore leave its accepted job misleadingly
  queued indefinitely.
- `backend/app/observability/metrics.py:364-389` calls
  `RedisBroker.get_queue_message_counts`. The locked dependency is Dramatiq
  2.2.0 (`backend/uv.lock`), whose Redis broker does not expose that method.
  `backend/tests/test_observability_metrics.py:201-247` substitutes a fake
  broker that does, so the production incompatibility is not covered.
- `deploy/compose/compose.hybrid-vps.yml:225-265` uses one 200 MB Redis instance
  for Dramatiq and rate limiting with `allkeys-lru`. Memory pressure may evict
  broker state silently; PostgreSQL recovery does not currently cover every
  running or maintenance execution state.

## Out of scope

| Capability | Boundary |
| --- | --- |
| Exactly-once execution or external side effects | impossible across PostgreSQL, Redis and external providers; ambiguous outcomes remain explicit |
| General workflow orchestration, DAGs, priorities or cancellation | no change to the one-job execution model |
| Public replay, cancel or job-administration APIs | operator/database-backed recovery remains internal |
| A general event-sourcing rewrite | `job_attempts` and maintenance runs are narrow operational ledgers, not domain event sourcing |
| A worker dashboard or new monitoring product | repair existing metrics and document alert queries only |
| Separate worker pools per queue | queue isolation remains a later capacity decision |
| New email provider SDK | retain the provider-neutral adapter; stable identity and ambiguity semantics apply to SMTP now and future adapters |
| Automatic resend of acceptance-unknown email | requires explicit operator/provider evidence; safety takes precedence over duplicate delivery |
| Reworking successful AI request idempotency and provider accounting | retain the existing deterministic request/replay contract; add ownership fencing only where needed |
| Arbitrary replay of dead outbox events | current-dispatch death settles visibly; repair remains guarded and contract-specific |
| Broad dependency upgrades | pin/validate the existing Dramatiq line needed by this work; no unrelated upgrades |

## Decisions and assumptions

- PostgreSQL, not Dramatiq's `on_retry_exhausted` message, owns durable retry
  state and terminal settlement. Durable actors record an attempt outcome before
  returning or propagating. A retryable outcome atomically queues the next
  dispatch with a durable `available_at`; an exhausted outcome atomically fails
  the job and runs its allow-listed domain finalization hook.
- Broker middleware retry remains bounded and covers transport/infrastructure
  failures that occur before an owned attempt can durably record its outcome.
  Reconciliation remains the PostgreSQL backstop if those broker retries vanish.
- `job_attempts` has one row per successful claim, keyed by an opaque attempt id
  and linked to job, dispatch id and owner token. Its closed statuses are
  `running`, `retry_scheduled`, `succeeded`, `failed`, `exhausted` and
  `abandoned`. It records attempt number, lease, start/end times, takeover flag
  and safe error code. Rows are not exposed through the public job schemas.
- Global maximum attempts are enforced from PostgreSQL claim/attempt history,
  not from a message-local Dramatiq retry counter. Reconciliation never grants a
  fresh attempt beyond that maximum; it terminally settles an exhausted job.
- Expired-running recovery locks the job, verifies the lease remains expired,
  closes the old attempt as `abandoned`, rotates the dispatch/owner boundary,
  returns the job to `queued` and creates one cooldown-deduplicated dispatch in
  one transaction. Non-expired, terminal and already-recovering jobs are excluded.
- Domain fencing uses an internal service contract, not router logic. Every
  consequential worker mutation either locks/verifies the owning job in the
  same database transaction or executes through a domain helper that performs
  that verification. Existing public schemas and tenant gates do not change.
- External calls cannot be rolled back. Actors verify ownership immediately
  before a provider call and again before committing its outcome. Stable
  provider idempotency is used when supported; otherwise an uncertain result is
  persisted rather than automatically repeated.
- Notification delivery gains a closed `unknown`/`attention_required` terminal
  outcome (exact naming settled in P3) plus a stable pre-send delivery identity.
  Public notification APIs do not expose provider internals or sensitive error
  text. SMTP Message-ID is correlation evidence, not an exactly-once guarantee.
- `maintenance_runs` is global internal infrastructure with a closed task type,
  scheduled bucket, status, attempt count, lease/owner fields, safe error code
  and timestamps. The coordinator atomically creates the run and its
  reference-only outbox event. Advisory locks remain defence in depth.
- Broker and rate-limit Redis endpoints are distinct typed settings. Production
  validation rejects equality after URL normalisation. Local development may
  run two Compose services from the existing Redis image; no new runtime
  dependency is introduced.
- Broker Redis uses AOF plus `noeviction`. When full, publication fails visibly,
  the coordinator retains the pending outbox intent and alerts fire. Rate-limit
  Redis retains its existing fail-closed API behavior.
- Queue metrics use only supported behavior verified against the locked
  Dramatiq version and real Redis. Tests must instantiate the production broker;
  a fake-only method is insufficient evidence.
- No authentication, permission, tenant-isolation or public API change is
  authorised. Infrastructure, secret/configuration, backup/recovery and any
  migration affecting terminal semantics require the human-review gates below.

## Commands that must work

Focused commands added or strengthened by this plan:

```bash
cd backend && uv run pytest \
  tests/test_jobs_db.py \
  tests/test_jobs_broker.py \
  tests/test_job_coordinator.py \
  tests/test_notifications_db.py \
  tests/test_observability_metrics.py
```

```bash
cd backend && uv run pytest \
  tests/test_outbox_db.py \
  tests/test_ai_reconcile_db.py \
  tests/test_ai_persistence_db.py
```

Existing final gates remain green:

```bash
make lint
make typecheck
make test
make e2e
make generate-client
make validate-execution-contracts
make check
```

Deployment validation must also prove the two-Redis topology:

```bash
docker compose -f deploy/compose/compose.hybrid-vps.yml config
docker compose -f deploy/compose/compose.local.yml --profile fullstack config
```

## Acceptance criteria

1. Every successful durable-job claim creates one PostgreSQL attempt row that
   is never deleted automatically. Success, permanent failure, retry scheduling, exhaustion,
   takeover and abandonment have durable timestamps and safe outcome codes;
   logs are supplementary rather than the only attempt evidence.
2. A transient domain failure atomically records its attempt outcome and either
   creates the next delayed dispatch or terminally exhausts the job. Deleting
   the Dramatiq exhausted-handler message, restarting Redis, or making the old
   finalizer unavailable cannot leave the job without a PostgreSQL-owned next
   action or terminal outcome.
3. The configured maximum is a global durable attempt limit. Redis loss,
   coordinator restart, duplicate publication and queued/running reconciliation
   cannot reset it or execute business work beyond it.
4. A worker that dies after claiming is recovered after its execution lease
   expires even when Redis contains no copy of the original message. Recovery
   is bounded, cooldown-deduplicated and safe under multiple coordinators.
5. A superseded worker cannot commit file, notification-delivery, AI or job
   state after takeover. Tests pause an old worker across lease expiry, let a
   new owner take over, and prove every old-owner mutation is rejected or an
   idempotent no-op.
6. Email delivery persists one stable identity before send. Definitely-unsent
   transient failures retry; permanent rejection fails; acceptance-unknown
   failures become a durable attention-required outcome and are not
   automatically resent. A crash after provider acceptance is test-covered.
7. Every scheduled maintenance bucket creates one durable maintenance run and
   reference-only dispatch. Completion, retry, exhaustion, lease-expired
   takeover and duplicate delivery are PostgreSQL-visible; an outbox
   `published` state alone is never treated as proof the sweep completed.
8. A permanently dead event that is still the current initial dispatch cannot
   leave an accepted job indefinitely queued. The job receives a safe durable
   failure/attention outcome and audit record without allowing a stale event to
   fail a newer dispatch.
9. Production uses distinct broker and rate-limit Redis endpoints. Broker Redis
   is private, authenticated, persistent and `noeviction`; a full broker causes
   visible publication failure and durable outbox retry rather than key eviction.
10. Queue metrics work against the locked production Dramatiq/Redis stack and
    are verified with real Redis. A compatibility regression fails tests rather
    than being swallowed as a permanent stale gauge.
11. Operator documentation provides exact queries and actions for expired
    running jobs, exhausted attempts, dead current dispatches, ambiguous email,
    failed maintenance runs, broker capacity and metric-refresh failure.
12. No endpoint, response schema, permission, tenant-isolation behavior or
    generated frontend API type changes. Existing file, notification and AI
    successful journeys remain green.

### Capability traceability

| Observable requirement | Acceptance | Checkpoint | Required evidence |
| --- | --- | --- | --- |
| Durable attempts and exhaustion | AC1-AC3 | P1-P2 | PostgreSQL transaction/crash tests and real-broker exhaustion without finalizer dependency |
| Running-job recovery | AC3-AC5 | P2 | empty-Redis + dead-worker lease-expiry journey with concurrent coordinators |
| Domain and external-effect fencing | AC5-AC6 | P2-P3 | paused stale-worker tests at each consequential mutation/provider boundary |
| Honest email delivery | AC6 | P3 | stable identity, accepted-then-disconnected, definitely-unsent and permanent-rejection tests |
| Durable maintenance execution | AC7 | P4 | schedule/claim/success/failure/exhaustion/takeover database and broker tests |
| Dead-dispatch terminal visibility | AC8 | P1-P2 | owner-checked dead-current-dispatch settlement tests |
| Broker integrity and truthful metrics | AC9-AC10 | P5 | real Redis pressure/compatibility tests and Compose validation |
| Operational and public-contract closure | AC11-AC12 | P5 | runbook review, security suite and diff-free generated client |

## Implementation checkpoints

### P1 — Durable Attempt Ledger and Retry/Finalization Contract

Dependencies: none

- [ ] Add an ADR amendment recording PostgreSQL-owned attempt history, global
      retry limits and terminal settlement; explicitly retire the claim that a
      Dramatiq `on_retry_exhausted` message is the durable finalization boundary.
- [ ] Add `job_attempts` model/query/service boundaries following existing
      module patterns, with closed statuses, safe error codes, ownership fields,
      constraints and indexes for current attempt, lease expiry and job history.
- [ ] Add an additive Alembic migration and migration tests. Keep attempt rows
      internal; do not alter public job request/response schemas.
- [ ] Change claim, progress/lease renewal and terminal helpers so attempt and
      job state remain transactionally consistent. A claimed owner is represented
      by exactly one running attempt row.
- [ ] Add a PostgreSQL-owned retry decision service: retryable outcome closes the
      current attempt and creates a delayed next dispatch atomically; exhaustion
      closes the attempt and fails the job plus its allow-listed domain hook
      atomically. Remove durable correctness dependence on the zero-retry
      exhausted-handler actor while preserving rolling-deployment compatibility.
- [ ] When a permanently invalid event still owns the job's current initial
      dispatch, settle the job and attempt to a safe delivery-contract failure
      in the same claim-token/dispatch-checked transaction. A dead stale event is
      a no-op against a newer dispatch.
- [ ] Add database and real-broker tests for commit/rollback, duplicate callback,
      Redis loss before/after retry scheduling, global attempt ceiling, failed
      finalizer compatibility and dead current/stale dispatch settlement.

Human review required before application: database migration and job terminal-
semantics changes must be reviewed. The migration is additive and non-destructive.

### P2 — End-to-End Fencing and Expired-Running Recovery

Dependencies: P1

- [ ] Add a reusable internal ownership guard that locks/verifies the job and
      running attempt in the same transaction as a consequential domain
      mutation. Do not pass ORM job objects as authority across commits.
- [ ] Apply the guard to file processing transitions/notification creation,
      notification delivery transitions, AI execution persistence and all
      domain exhaustion hooks. Preserve existing service boundaries and audit
      semantics.
- [ ] Revalidate ownership immediately before each external provider call and
      before committing its outcome. Treat stale ownership as a no-op/abandoned
      attempt, not as a retryable domain failure.
- [ ] Add bounded coordinator queries/services for execution-lease-expired
      `running` jobs: lock with `FOR UPDATE SKIP LOCKED`, close the old attempt
      abandoned, rotate the dispatch boundary, queue the job and create one
      cooldown-keyed outbox event atomically.
- [ ] Enforce the global attempt ceiling during queued and running
      reconciliation. Exhausted work settles terminally instead of receiving a
      fresh dispatch.
- [ ] Add database and real-worker failure injection covering worker SIGKILL,
      empty Redis, lease expiry, two coordinators, stale worker resumption and
      stale mutations at every domain boundary.

Human review required before application: recovery semantics and tenant-linked
domain fencing require human review for tenant isolation and backup/recovery
impact, even though no public permission changes are intended.

### P3 — Honest and Idempotency-Aware Email Delivery

Dependencies: P1, P2

- [ ] Extend the provider-neutral email contract with a stable caller-supplied
      delivery identity and explicit definitely-unsent, permanently-rejected and
      acceptance-unknown failure categories. Keep provider SDKs/adapters behind
      `app/email/`.
- [ ] Persist the stable delivery identity before the first send and reuse it on
      every safe retry. SMTP uses it as the stable Message-ID; future providers
      may map it to a native idempotency key.
- [ ] Refine the SMTP adapter so connection/setup failures before message
      submission are retryable, explicit SMTP rejection is permanent according
      to response class, and disconnect/timeout after submission begins is
      acceptance-unknown rather than definitely retryable.
- [ ] Add an internal terminal attention-required delivery outcome with bounded
      safe error code and audit event. Do not automatically resend it; document
      provider-side verification and guarded operator resolution.
- [ ] Ensure job, job-attempt and notification-delivery outcomes settle
      consistently under the captured owner for success, rejection, safe retry,
      ambiguity and exhaustion.
- [ ] Add deterministic adapter, database and real-SMTP/broker tests including
      provider-accepted-then-worker-crashed, disconnect during submission,
      stable Message-ID reuse and stale-owner resumption.

Human review required before application: external-delivery semantics and the
new attention-required state require review. No public API break is authorised.

### P4 — Durable Maintenance Runs

Dependencies: P1, P2

- [ ] Add an internal `maintenance_runs` model with closed task types/statuses,
      UTC bucket identity, attempt/owner/lease fields, safe errors and timestamps;
      add its additive Alembic migration, constraints and indexes.
- [ ] Make schedule creation write the maintenance run and reference-only outbox
      event atomically. Update the registry/actors so broker messages carry only
      `maintenance_run_id`.
- [ ] Add maintenance claim, success, retry, exhaustion and expired-lease
      takeover services. Retain PostgreSQL advisory locks as defence in depth,
      not as the durable execution record.
- [ ] Apply the contract to AI retention and provider-file reconciliation while
      preserving their bounded work, provider adapters, per-item audit records
      and privacy constraints.
- [ ] Add database and real-broker tests for duplicate schedule ticks,
      publish-without-run, worker crash, Redis loss, retry exhaustion, advisory-
      lock contention and eventual successful rerun.

Human review required before application: additive database and scheduled
privacy/cleanup recovery semantics require backup-and-recovery review.

### P5 — Broker Isolation, Truthful Observability and Operational Closure

Dependencies: P2, P3, P4

- [ ] Introduce typed `BROKER_REDIS_URL` and `RATE_LIMIT_REDIS_URL` settings,
      production validation requiring distinct normalised endpoints, and
      backward-compatible non-production defaults only where safe.
- [ ] Split Redis into broker and rate-limit services/volumes in both Compose
      profiles. Broker Redis is authenticated, AOF-backed and `noeviction`;
      rate-limit Redis retains bounded counter-oriented memory behavior.
- [ ] Replace `get_queue_message_counts` with an observability adapter proven
      against the locked Dramatiq version and real Redis. Cover ready queues and
      every delayed/in-flight/dead signal that can be derived reliably without
      payload access; label only closed queue/state values.
- [ ] Add startup compatibility validation or a failing integration test so a
      future Dramatiq change cannot silently disable queue metrics. Pin the
      supported Dramatiq version range deliberately and record why.
- [ ] Add alerts for expired running attempts, retry/exhaustion backlog,
      attention-required email, failed/stale maintenance runs, broker memory/
      rejected writes, dead current dispatches and metric refresh failure.
- [ ] Update the blueprint, ADR-0004/0019, `ARCHITECTURE.md`, operations,
      backup/recovery, environment examples, deployment docs and task-authoring
      guidance. Remove stale instructions that operators should schedule
      maintenance by calling actor `.send()` directly.
- [ ] Run focused validation, human-review gates and final repository commands;
      prove generated API types remain diff-free and the mandatory security
      suite remains green.

Human review required before application: infrastructure changes, Redis
credentials/configuration, backup/recovery changes and deployment/rollback order.

## Reference map

| Checkpoint | Governing sources | What to extract |
| --- | --- | --- |
| P1 | `Internal_Custom_Application_Starter_Architecture_v2.md` BP §18-§19; `docs/decisions/0019-harden-dramatiq-delivery-with-an-outbox.md`; `backend/app/modules/jobs/service.py`; `backend/app/modules/jobs/execution.py`; `backend/app/modules/jobs/tasks.py`; `backend/app/job_coordinator/loop.py` | Current ownership, retry, outbox settlement, audit and migration patterns; remove broker-only terminal dependence without weakening at-least-once delivery |
| P2 | BP §18-§19 and BP §31; `backend/app/modules/files/tasks.py`; `backend/app/modules/files/service.py`; `backend/app/modules/notifications/tasks.py`; `backend/app/modules/notifications/service.py`; `backend/app/ai/execution.py`; `backend/app/job_coordinator/reconciliation.py` | Consequential domain mutations, tenant boundaries, worker claim/lease behavior and current queued-only recovery |
| P3 | BP §18 and BP §20; `backend/app/email/base.py`; `backend/app/email/smtp.py`; `backend/app/modules/notifications/models.py`; `backend/app/modules/notifications/tasks.py`; `backend/app/modules/notifications/service.py` | Provider-neutral adapter rules, delivery states, audit fields, SMTP ambiguity and current retry classification |
| P4 | BP §18-§19 and BP §28; `backend/app/ai/persistence/tasks.py`; `backend/app/ai/persistence/service.py`; `backend/app/ai/persistence/reconciliation.py`; `backend/app/job_coordinator/registry.py`; `backend/app/job_coordinator/reconciliation.py` | Maintenance privacy, bounded sweeps, advisory locks, scheduling deduplication and missing durable run outcome |
| P5 | BP §28 and BP §35-§36; `backend/app/broker.py`; `backend/app/core/config.py`; `backend/app/observability/metrics.py`; `deploy/compose/compose.local.yml`; `deploy/compose/compose.hybrid-vps.yml`; `docs/operations.md`; `docs/backup-and-recovery.md`; locked Dramatiq source/API | Production Redis topology, fail-visible capacity behavior, supported queue observability, liveness, rollback and recovery documentation |

## API, data and security impact

- **API/frontend:** no endpoint, permission, request or response schema change is
  planned. Attempt, maintenance-run and internal delivery-attention state remain
  backend operational details. `make generate-client` must be diff-free.
- **Database:** additive `job_attempts` and `maintenance_runs` migrations plus
  the minimum notification-delivery fields/status change needed for stable
  identity and ambiguity. No destructive migration or automatic deletion of
  business/audit rows is authorised.
- **Tenant isolation:** job attempts inherit their job's validated organisation;
  all reconciliation and domain fencing derives organisation from the locked
  durable row. Maintenance runs are global and contain no tenant payload.
- **Messages:** job and maintenance messages remain reference-only (`job_id` or
  `maintenance_run_id`). No payload content, email address, file reference,
  prompt, provider id, credential or URL enters Redis.
- **External effects:** the implementation remains at-least-once. Stable
  idempotency is used where available; acceptance-unknown effects become
  durable attention-required outcomes rather than unsafe automatic retries.
- **Secrets/infrastructure:** splitting Redis adds a second authenticated
  endpoint/credential surface and changes backup/deployment topology. Human
  infrastructure, secret-handling and backup/recovery review is mandatory.
- **Public compatibility:** no public cancellation/replay behavior and no
  generic administrative bypass are introduced. Protected-route coverage is
  unchanged and must remain green.

## Validation plan

- **Pure/unit tests:** attempt state machine, global retry decision, safe error
  vocabulary, reconciliation eligibility/cooldown, email acceptance certainty,
  stable delivery identity, maintenance state machine, Redis URL separation and
  metric label bounds.
- **PostgreSQL integration tests:** atomic claim/attempt creation, retry dispatch
  scheduling, exhaustion/domain hook transaction, dead-current dispatch,
  expired-running recovery, stale-owner domain rejection, maintenance run
  lifecycle and concurrent `SKIP LOCKED` behavior.
- **Redis/worker integration tests:** lost exhausted callback, worker SIGKILL,
  Redis flush between claim and settlement, duplicate publish, delayed retry,
  broker full/noeviction, maintenance crash/takeover and real queue metrics on
  the locked Dramatiq implementation.
- **External-boundary tests:** SMTP accepted/disconnected ambiguity, definite
  pre-send failure, permanent rejection, stable identity reuse and no automatic
  resend of attention-required delivery. No real recipient is used.
- **Domain regression tests:** file ready/failed notifications, email success,
  AI request replay/budget/output persistence, retention and provider-file
  reconciliation remain correct under the owner fence.
- **Security/contract tests:** no tenant id from broker payload, no sensitive
  attempt/error content, no new public route, mandatory protected routes green
  and generated client unchanged.
- **Operational tests:** two Redis services, credentials, AOF/noeviction,
  backup/restore, rolling deployment, coordinator/worker restart and alerts for
  every new durable attention state.
- **Final gates:** after review findings are applied, run the focused commands,
  then `make check` once plus every additional contract command. Do not weaken
  linting, typing, tests or security coverage.

## Review and delivery

- Execute P1 through P5 in order. Each checkpoint is one independent
  implement → review → apply-and-commit cycle on its own feature branch under
  `CONTRIBUTING.md`.
- Keep this plan `Status: Draft` until the owner approves its decisions and
  human-review gates. Activation uses the exact transition to `Status: Active`;
  completion uses `Status: Complete` only after all evidence is reviewed.
- Never check boxes or commit before review. Preserve unrelated worktree changes
  and do not fold the current coordinator/logging edits into this plan unless
  their owner deliberately assigns them to a checkpoint.
- P1 stops before application for migration and terminal-semantics review. P2
  stops for tenant-isolation and recovery review. P3 stops for external-delivery
  semantics review. P4 stops for cleanup/privacy recovery review. P5 stops for
  infrastructure, secrets and backup/recovery review.
- Recommended rollout: migrations first; backward-compatible workers that can
  read old and new messages second; coordinator/attempt retry contract third;
  domain-fenced actors fourth; durable maintenance fifth; split Redis and metric
  cutover last. Observe old/new attempt, stale-running, maintenance and broker
  signals at every stage.
- Rollback must pause coordinator publication/reconciliation before reverting
  application containers. Preserve job, attempt, maintenance and outbox rows for
  roll-forward. Do not downgrade additive migrations in production merely to
  roll back application code.
- No new dependency, public API, authentication, permission or tenant model is
  authorised. Discovery of such a requirement returns the affected checkpoint
  to draft for an explicit decision and human review.
- Completion requires all acceptance evidence, real PostgreSQL/Redis failure
  journeys, reviewed infrastructure and recovery procedures, green final gates,
  consistent documentation and an honest statement of remaining exactly-once
  limitations.
