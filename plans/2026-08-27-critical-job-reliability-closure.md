# Critical Reliability, Security and Audit Closure Plan

Status: Active

## Goal

This existing job-reliability plan now also tracks the critical findings from
the September 2026 starter review. P1-P5 retain their original job scope;
P6-P10 are separate, review-gated work units. Updating this draft authorises
planning, not application of authentication, permission, tenant, API,
infrastructure or destructive data changes.

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
- Close the document-source, mutable-upload, file-completion and scratch-object
  gaps before treating uploaded documents as trusted AI or download inputs.
- Prevent invitation/revocation races and last-platform-admin lockout, and give
  auditable business records a real conflict/revision contract.
- Make production browser upload and client-IP controls work in the deployed
  topology, and make the cloneable starter's release and capability claims
  truthful.

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
- `backend/app/ai/storage_resolver.py` and `backend/app/ai/streamed_source.py`
  accept organisation-prefixed document storage references by object metadata
  and MIME/size, without requiring a live `files` row in an allowed lifecycle
  state. AI can read a pending, failed, quarantined or unknown-key document;
  a fake-storage regression reproduced the missing-row case. Organisation
  prefix checks correctly reject cross-tenant keys but do not prove document
  readiness or caller authority.
- `backend/app/storage/s3.py` signs PUT on the final document key. The URL is
  reusable until expiry; `backend/app/modules/files/service.py` verifies the
  object at completion but does not pin an immutable version/digest through
  processing, AI reads and downloads. A same-key overwrite after completion
  can change what a previously approved File row refers to.
- `backend/app/modules/files/service.py` commits the `UPLOADED` transition and
  audit before calling `jobs_service.schedule_job()` in a second transaction.
  A failed schedule leaves an uploaded file with no processing job, while
  retrying completion conflicts with the changed status.
- `backend/app/modules/invitations/service.py` accepts a previously selected
  invitation without serialising `SENT -> ACCEPTED` against revocation or
  duplicate login. A stale acceptance may grant membership after a concurrent
  revoke; provider and database outcomes also lack an explicit reconciliation
  rule.
- `backend/app/modules/platform_admin/service.py` counts remaining admin
  memberships without excluding disabled users or serialising concurrent
  revocations/deactivations. Two removals can each observe another admin, and
  an inactive remaining admin is not a recovery principal.
- `backend/app/modules/records/service.py` updates records without a version
  check and hard-deletes them; its action-only audit events cannot reconstruct
  prior values. `backend/app/modules/audit/models.py` uses `ON DELETE SET NULL`
  on actor/organisation links and lacks database-level append-only protection.
  This is weaker than blueprint §10's collaborative-edit concurrency and the
  starter's auditable-business-app goal.
- `backend/app/modules/ai_demo/router.py` permits scratch upload without an
  AI-enabled check. Scratch objects have no durable upload lifecycle; retention
  selection in `backend/app/ai/persistence/queries.py` excludes organisations
  with null retention, leaving objects without a global maximum lifetime.
- `backend/app/modules/files/service.py` presently allows downloads at
  `UPLOADED` and `PROCESSING`, before the worker establishes `READY`; this must
  be reconciled with any scan/quarantine trust gate rather than preserved by
  accident. `backend/app/modules/users/service.py` and invitation acceptance
  also perform repeated WorkOS profile reads on the login path; P7 should
  measure and remove redundant calls without changing identity semantics.
- Production CSP in `deploy/caddy/Caddyfile` and `frontend/nginx.conf` allows
  same-origin and WorkOS connections but not the external S3 upload origin in
  `.env.production.example`. Browser direct PUT therefore fails in the example
  deployment. `backend/app/main.py` rate-limits by `request.client.host`; the
  hybrid Compose Uvicorn/Caddy topology does not explicitly trust only the
  Caddy proxy for forwarded client IP, so distinct clients may share one quota.
- `backend/app/modules/users/service.py` unions role codes across all user
  memberships for `/me`; `frontend/src/lib/permissions.ts` uses those global
  roles for selected-organisation UI capabilities. A multi-org owner/viewer
  can see write affordances in the viewer org. Backend authorisation remains
  the enforced boundary; the frontend contract is misleading.
- `/ai/ask` in `backend/app/modules/ai_demo/router.py` performs managed AI work
  synchronously even for large document attachments. The starter rule places
  long-running work behind Dramatiq. File processing also does not provide a
  malware-scanning/quarantine gate before readiness; `SECURITY.md` explicitly
  defers scanning.
- The repository is tagged `v0.8.0` while backend/frontend package versions
  remain `0.7.0`, and `TEMPLATE_V0_8_SCOPE.md` still says planned despite its
  completed checkboxes. Blueprint §41's upgrade-guide expectation is not met;
  clone consumers cannot reliably infer the template's implemented release.

## Out of scope

| Capability | Boundary |
| --- | --- |
| Exactly-once execution or external side effects | impossible across PostgreSQL, Redis and external providers; ambiguous outcomes remain explicit |
| General workflow orchestration, DAGs, priorities or cancellation | no change to the one-job execution model |
| Public replay, cancel or job-administration APIs | operator/database-backed recovery remains internal |
| A general event-sourcing rewrite | `job_attempts` and maintenance runs are narrow operational ledgers; P8 adds bounded record revisions, not whole-system event sourcing |
| A worker dashboard or new monitoring product | repair existing metrics and document alert queries only |
| Separate worker pools per queue | queue isolation remains a later capacity decision |
| New email provider SDK | retain the provider-neutral adapter; stable identity and ambiguity semantics apply to SMTP now and future adapters |
| Automatic resend of acceptance-unknown email | requires explicit operator/provider evidence; safety takes precedence over duplicate delivery |
| Reworking successful AI request idempotency and provider accounting | retain the existing deterministic request/replay contract; add ownership fencing only where needed |
| Arbitrary replay of dead outbox events | current-dispatch death settles visibly; repair remains guarded and contract-specific |
| Broad dependency upgrades | pin/validate the existing Dramatiq line needed by this work; no unrelated upgrades |
| Exactly-once object writes or provider calls | staged promotion/version pinning and honest ambiguity are the achievable boundaries |
| A full commercial-property domain model | prove the starter contract with one representative editable record and document journey; app-specific schemas remain clone work |
| An automatic malware verdict without a selected provider | P6 must define a deny-by-default scan/quarantine boundary and a documented adapter; provider choice requires separate review |

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
- P6-P10 may require authentication, permission, tenant-isolation, additive
  public API, infrastructure or secret/configuration changes. Those choices
  remain proposals until the owner selects the contract and human review is
  completed; no public API break or destructive migration is implied by this
  draft. Infrastructure, backup/recovery and terminal semantics retain their
  existing review gates.
- A signed storage URL is a time-limited capability, not a proof that object
  bytes are immutable. Document AI/download access must derive File identity,
  tenant, status and pinned content identity from validated database context;
  AI scratch keys must use a separate, bounded lifecycle contract.
- Critical transaction races must be tested with two real PostgreSQL sessions,
  not only single-session mocks. Recovery and audit guarantees must hold through
  rollback, concurrent requests, worker restart and user deletion/deactivation.

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

P6-P10 also require targeted PostgreSQL, MinIO, browser, WorkOS-webhook and
concurrency regression runs created with those checkpoints. Do not count an
environment-skipped database test as closure evidence. Each new protected
route must enter `backend/tests/test_security_suite.py` before final gates.

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
12. P1-P5 introduce no endpoint, response schema, permission, tenant-isolation
    behavior or generated frontend API type changes. Existing file,
    notification and AI successful journeys remain green; any P6-P10 public
    contract addition is explicit, reviewed and regenerated.
13. AI document reads require a live tenant-matched File record in an allowed
    completed/ready state and the exact pinned object identity. Pending,
    failed, quarantined, deleted, unknown and cross-org document keys fail
    closed at request and worker execution; scratch keys use their own guard.
14. A completed document cannot be silently changed by replaying a signed PUT.
    Staging/promotion or version/digest pinning is verified through processing,
    AI and download, including same-size overwrites and interrupted promotion.
15. File completion, audit, job and outbox are one recoverable/idempotent
    transaction. Scheduling failure and two concurrent completions leave no
    `UPLOADED` file without one processable job and no duplicate jobs.
16. Revocation and invitation acceptance are serialised around the legal state
    transition and membership grant. A committed revoke cannot later become
    accepted or grant access; duplicate login and provider/webhook races are
    reconciled without reopening access.
17. Last-admin checks count only active recovery principals and are safe under
    concurrent revocation and user deactivation. An audited break-glass path is
    specified for pre-existing zero-active-admin states; ordinary actions
    cannot silently create one.
18. A representative business record has optimistic concurrency, history and
    durable provenance. Stale updates/deletes return conflict, revisions can
    reconstruct approved changes without sensitive audit leakage, and actor/
    organisation identity survives deletion according to a reviewed retention
    policy. Database-level append-only enforcement is validated.
19. Scratch upload respects the organisation AI policy and every scratch object
    has a bounded global maximum lifetime, including null per-org retention,
    aborted uploads, worker crashes and storage cleanup failure. Untrusted
    document bytes cannot become ready/AI-readable before the reviewed
    quarantine/scanning decision.
20. Example production browser PUT works with a deliberately scoped storage
    CSP origin and CORS policy. Per-client limits use a verified Caddy-to-
    Uvicorn forwarded-IP trust boundary; forged headers cannot bypass limits.
21. `/me` and generated frontend types provide selected-organisation roles or
    capabilities; multi-org UI write affordances match the backend 403. Large
    AI asks either use a durable job/result path or reject above a documented
    synchronous bound; no 50 MB provider work runs inside an API request.
22. Tag, package metadata, scope status and upgrade guidance consistently
    describe the released starter. A fresh clone passes environment, migration,
    generated-client and deployment smoke checks with no undocumented steps.

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
| Document source and immutable file bytes | AC13-AC15 | P6 | real-DB lifecycle/atomicity races and MinIO PUT-replay/version tests |
| Identity and recovery-admin safety | AC16-AC17 | P7 | two-session invitation/revoke/admin/deactivation races and WorkOS webhook tests |
| Business audit and conflicts | AC18 | P8 | revision/409/delete/provenance/DB append-only tests |
| Scratch, scanning and async AI | AC19, AC21 | P6, P9 | AI-disabled/null-retention/orphan tests, scan boundary and large-ask job tests |
| Deployed browser and proxy security | AC20 | P9 | external-origin browser E2E, Compose/CSP/CORS and forged-XFF tests |
| Selected-org UI and release truth | AC21-AC22 | P10 | multi-org frontend/API contract and fresh-clone/release smoke tests |

## Implementation checkpoints

### P1 — Durable Attempt Ledger and Retry/Finalization Contract

Dependencies: none

- [x] Add an ADR amendment recording PostgreSQL-owned attempt history, global
      retry limits and terminal settlement; explicitly retire the claim that a
      Dramatiq `on_retry_exhausted` message is the durable finalization boundary.
- [x] Add `job_attempts` model/query/service boundaries following existing
      module patterns, with closed statuses, safe error codes, ownership fields,
      constraints and indexes for current attempt, lease expiry and job history.
- [x] Add an additive Alembic migration and migration tests. Keep attempt rows
      internal; do not alter public job request/response schemas.
- [x] Change claim, progress/lease renewal and terminal helpers so attempt and
      job state remain transactionally consistent. A claimed owner is represented
      by exactly one running attempt row.
- [x] Add a PostgreSQL-owned retry decision service: retryable outcome closes the
      current attempt and creates a delayed next dispatch atomically; exhaustion
      closes the attempt and fails the job plus its allow-listed domain hook
      atomically. Remove durable correctness dependence on the zero-retry
      exhausted-handler actor while preserving rolling-deployment compatibility.
- [x] When a permanently invalid event still owns the job's current initial
      dispatch, settle the job and attempt to a safe delivery-contract failure
      in the same claim-token/dispatch-checked transaction. A dead stale event is
      a no-op against a newer dispatch.
- [x] Add database and real-broker tests for commit/rollback, duplicate callback,
      Redis loss before/after retry scheduling, global attempt ceiling, failed
      finalizer compatibility and dead current/stale dispatch settlement.

Human review required before application: database migration and job terminal-
semantics changes must be reviewed. The migration is additive and non-destructive.

### P2 — End-to-End Fencing and Expired-Running Recovery

Dependencies: P1

- [x] Add a reusable internal ownership guard that locks/verifies the job and
      running attempt in the same transaction as a consequential domain
      mutation. Do not pass ORM job objects as authority across commits.
- [x] Apply the guard to file processing transitions/notification creation,
      notification delivery transitions, AI execution persistence and all
      domain exhaustion hooks. Preserve existing service boundaries and audit
      semantics.
- [x] Revalidate ownership immediately before each external provider call and
      before committing its outcome. Treat stale ownership as a no-op/abandoned
      attempt, not as a retryable domain failure.
- [x] Add bounded coordinator queries/services for execution-lease-expired
      `running` jobs: lock with `FOR UPDATE SKIP LOCKED`, close the old attempt
      abandoned, rotate the dispatch boundary, queue the job and create one
      cooldown-keyed outbox event atomically.
- [x] Enforce the global attempt ceiling during queued and running
      reconciliation. Exhausted work settles terminally instead of receiving a
      fresh dispatch.
- [x] Add database and real-worker failure injection covering worker SIGKILL,
      empty Redis, lease expiry, two coordinators, stale worker resumption and
      stale mutations at every domain boundary.

Human review required before application: recovery semantics and tenant-linked
domain fencing require human review for tenant isolation and backup/recovery
impact, even though no public permission changes are intended.

### P3 — Honest and Idempotency-Aware Email Delivery

Dependencies: P1, P2

- [x] Extend the provider-neutral email contract with a stable caller-supplied
      delivery identity and explicit definitely-unsent, permanently-rejected and
      acceptance-unknown failure categories. Keep provider SDKs/adapters behind
      `app/email/`.
- [x] Persist the stable delivery identity before the first send and reuse it on
      every safe retry. SMTP uses it as the stable Message-ID; future providers
      may map it to a native idempotency key.
- [x] Refine the SMTP adapter so connection/setup failures before message
      submission are retryable, explicit SMTP rejection is permanent according
      to response class, and disconnect/timeout after submission begins is
      acceptance-unknown rather than definitely retryable.
- [x] Add an internal terminal attention-required delivery outcome with bounded
      safe error code and audit event. Do not automatically resend it; document
      provider-side verification and guarded operator resolution.
- [x] Ensure job, job-attempt and notification-delivery outcomes settle
      consistently under the captured owner for success, rejection, safe retry,
      ambiguity and exhaustion.
- [x] Add deterministic adapter, database and real-SMTP/broker tests including
      provider-accepted-then-worker-crashed, disconnect during submission,
      stable Message-ID reuse and stale-owner resumption.

Human review required before application: external-delivery semantics and the
new attention-required state require review. No public API break is authorised.

### P4 — Durable Maintenance Runs

Dependencies: P1, P2

- [x] Add an internal `maintenance_runs` model with closed task types/statuses,
      UTC bucket identity, attempt/owner/lease fields, safe errors and timestamps;
      add its additive Alembic migration, constraints and indexes.
- [x] Make schedule creation write the maintenance run and reference-only outbox
      event atomically. Update the registry/actors so broker messages carry only
      `maintenance_run_id`.
- [x] Add maintenance claim, success, retry, exhaustion and expired-lease
      takeover services. Retain PostgreSQL advisory locks as defence in depth,
      not as the durable execution record.
- [x] Apply the contract to AI retention and provider-file reconciliation while
      preserving their bounded work, provider adapters, per-item audit records
      and privacy constraints.
- [x] Add database and real-broker tests for duplicate schedule ticks,
      publish-without-run, worker crash, Redis loss, retry exhaustion, advisory-
      lock contention and eventual successful rerun.

Human review required before application: additive database and scheduled
privacy/cleanup recovery semantics require backup-and-recovery review.

### P5 — Broker Isolation, Truthful Observability and Operational Closure

Dependencies: P2, P3, P4

- [x] Introduce typed `BROKER_REDIS_URL` and `RATE_LIMIT_REDIS_URL` settings,
      production validation requiring distinct normalised endpoints, and
      backward-compatible non-production defaults only where safe.
- [x] Split Redis into broker and rate-limit services/volumes in both Compose
      profiles. Broker Redis is authenticated, AOF-backed and `noeviction`;
      rate-limit Redis retains bounded counter-oriented memory behavior.
- [x] Replace `get_queue_message_counts` with an observability adapter proven
      against the locked Dramatiq version and real Redis. Cover ready queues and
      every delayed/in-flight/dead signal that can be derived reliably without
      payload access; label only closed queue/state values.
- [x] Add startup compatibility validation or a failing integration test so a
      future Dramatiq change cannot silently disable queue metrics. Pin the
      supported Dramatiq version range deliberately and record why.
- [x] Add alerts for expired running attempts, retry/exhaustion backlog,
      attention-required email, failed/stale maintenance runs, broker memory/
      rejected writes, dead current dispatches and metric refresh failure.
- [x] Update the blueprint, ADR-0004/0015/0019, `ARCHITECTURE.md`, operations,
      backup/recovery, environment examples, deployment docs and task-authoring
      guidance. Remove stale instructions that operators should schedule
      maintenance by calling actor `.send()` directly.
- [x] Run focused validation, human-review gates and final repository commands;
      prove generated API types remain diff-free and the mandatory security
      suite remains green.

Human review required before application: infrastructure changes, Redis
credentials/configuration, backup/recovery changes and deployment/rollback order.

### P6 — Document Authority, Immutable Uploads and Atomic File Completion

Dependencies: P1-P2 for worker fencing; security design may be reviewed earlier

- [ ] Define one source-authorisation service for document keys used by inline
      AI, streamed/provider AI, job retries and download. Resolve the File from
      validated organisation context; require an allowed lifecycle status and
      pinned content identity. Do not treat a prefix, object HEAD, MIME or size
      as authorisation. Scratch keys use a distinct durable intent/expiry guard.
- [ ] Choose and review a provider-neutral immutable upload strategy: unique
      staging key plus verified promotion, or object-version/digest pinning with
      a proven S3-compatible adapter. Bound signed PUT lifetime, prevent old
      capabilities from mutating approved bytes, and reconcile DB/object-store
      partial failures without exposing unverified content.
- [ ] Move File completion transition, audit, durable processing job and
      dispatch outbox into one transaction. Lock the File, make completion
      replay idempotent, and preserve one processable job under parallel calls.
- [ ] Define untrusted-file quarantine/scanning policy and adapter boundary.
      `READY`, AI and download must be gated until the approved verdict; a
      scanner outage must not turn unscanned content into trusted content.
- [ ] Require AI-enabled policy before scratch upload capability issuance;
      persist scratch expiry/usage state, apply a global maximum independent of
      optional per-org retention, and configure storage lifecycle as a backstop.
      Reconcile aborted uploads, failed cleanup and in-use provider transfers.
- [ ] Add real-DB, concurrent-session, fake-storage and MinIO tests: unknown/
      pending/failed/quarantined/deleted/cross-org keys; AI inline and streamed
      reads; PUT reuse and same-size overwrite; completion rollback/parallel
      replay; scanner unavailable; disabled AI and null-retention expiry.

Human review required before application: tenant isolation, signed capability
and secret handling, File lifecycle/public API compatibility, storage
infrastructure, malware policy and backup/recovery. Destructive object cleanup
needs a reviewed retention/restore boundary.

### P7 — Invitation Revocation and Platform-Admin Recovery Safety

Dependencies: none; coordinate authentication rollouts with existing v0.4 flow

- [ ] Specify legal invitation transitions and provider/database conflict
      precedence. Lock or conditionally update `SENT -> ACCEPTED` in the same
      transaction as membership grant; revoke, webhook and duplicate acceptance
      must use the same serialisation boundary. Reconcile provider failures
      without accepting a committed revoked invitation.
- [ ] Make the last-platform-admin invariant count enabled users with active
      admin membership. Serialise grant/revoke and user disable/delete/webhook
      pathways that can remove the last active principal. Define an audited,
      tightly scoped operator recovery path for an already locked-out plane.
- [ ] Add two-session PostgreSQL race tests for accept-vs-revoke, duplicate
      login, two concurrent admin removals, admin disable/delete and WorkOS
      revoke/deactivation webhooks. Preserve cross-plane and non-admin 403
      security-suite cases.
- [ ] Measure and eliminate duplicate WorkOS profile retrieval during a
      successful login when the same validated identity can be reused; retain
      fail-closed profile and membership checks.

Human review required before application: authentication, permission model,
tenant isolation, provider reconciliation and break-glass secret handling.

### P8 — Auditable Business Records and Conflict-Safe Edits

Dependencies: none; share audit retention decisions with P6-P7

- [ ] Add a version/conditional-update contract to the representative records
      module. Stale update/delete returns 409; retries do not lose a later
      writer's work. Keep ORM objects out of request schemas and regenerate
      frontend API types for reviewed additive response fields.
- [ ] Design a bounded immutable revision record or redacted field-diff that
      can reconstruct business changes without copying secrets or sensitive
      document contents into generic audit metadata. Review actor/org stable
      identity on user/organisation deletion and retention/erasure tradeoffs.
- [ ] Enforce audit append-only at the database privilege/trigger boundary, or
      document and test an equivalent separately held immutable export. Make
      hard delete/restore semantics explicit and protect revision history from
      normal API callers.
- [ ] Add migrations and two-session tests for stale writes/deletes, revision
      reconstruction, actor deletion, tenant scope, append-only denial and
      restoration. Add security-suite route coverage for any new protected API.

Human review required before application: public API, permissions, tenant
isolation, destructive migration/retention, backup/recovery and audit privacy.

### P9 — Production Browser/Proxy Boundaries and Bounded AI Work

Dependencies: P6 for document trust; P5 for production topology verification

- [ ] Set the production CSP `connect-src` to exact configured public storage
      origins without signed query strings or broad wildcard origins. Configure
      storage CORS for the actual browser PUT method/headers and authorised
      frontend origin. Keep WorkOS and other existing restrictions intact.
- [ ] Establish the precise Caddy-to-Uvicorn trusted-proxy boundary for client
      IP extraction and rate limiting. Reject client-supplied forwarded headers
      from untrusted peers; verify separate real users have separate quotas.
- [ ] Move large `/ai/ask` work behind the durable job path with an explicit
      accepted/result contract, or cap synchronous attachments below a tested
      latency/size limit and reject larger requests. Keep provider calls behind
      AI adapters and avoid public provider-accounting leakage.
- [ ] Add external-S3-origin browser E2E, CSP/CORS negative cases, Compose
      forwarded-IP/spoofing tests, rate-limit separation and large-ask timeout/
      duplicate/job recovery tests. Update production examples and runbooks.

Human review required before application: infrastructure, proxy/auth security,
secret handling, tenant isolation and any additive public AI API contract.

### P10 — Organisation-Scoped UI and Clone/Release Truth

Dependencies: P7 for final permission semantics; P6-P9 for clone smoke closure

- [ ] Make `/me` expose per-organisation roles/capabilities (or an explicit
      selected-org capability endpoint); never interpret a union of roles as
      selected-org authority. Generate frontend API types and derive selected-
      org UI affordances from that context. Backend permission checks remain
      decisive and the viewer-write security suite remains green.
- [ ] Reconcile the historical `v0.8.0` tag, package versions and
      `TEMPLATE_V0_8_SCOPE.md` state without rewriting the immutable tag.
      Document the version convention and a v0.7-to-v0.8 upgrade guide with
      migration, config, worker and frontend-client implications.
- [ ] Run a fresh-clone smoke scenario: example environment validation,
      migrations, generated client, protected-route security suite, external
      upload and deployment Compose configuration. Record unsupported/deferred
      features (including chosen malware provider) honestly in starter docs.
- [ ] Add multi-org owner/viewer frontend and API tests plus version/scope
      consistency checks; keep all final lint, typing, test and E2E gates green.

Human review required before application: permission model, additive public
API, generated frontend types, release contract and deployment documentation.

## Reference map

| Checkpoint | Governing sources | What to extract |
| --- | --- | --- |
| P1 | `Internal_Custom_Application_Starter_Architecture_v2.md` BP §18-§19; `docs/decisions/0019-harden-dramatiq-delivery-with-an-outbox.md`; `backend/app/modules/jobs/service.py`; `backend/app/modules/jobs/execution.py`; `backend/app/modules/jobs/tasks.py`; `backend/app/job_coordinator/loop.py` | Current ownership, retry, outbox settlement, audit and migration patterns; remove broker-only terminal dependence without weakening at-least-once delivery |
| P2 | BP §18-§19 and BP §31; `backend/app/modules/files/tasks.py`; `backend/app/modules/files/service.py`; `backend/app/modules/notifications/tasks.py`; `backend/app/modules/notifications/service.py`; `backend/app/ai/execution.py`; `backend/app/job_coordinator/reconciliation.py` | Consequential domain mutations, tenant boundaries, worker claim/lease behavior and current queued-only recovery |
| P3 | BP §18 and BP §20; `backend/app/email/base.py`; `backend/app/email/smtp.py`; `backend/app/modules/notifications/models.py`; `backend/app/modules/notifications/tasks.py`; `backend/app/modules/notifications/service.py` | Provider-neutral adapter rules, delivery states, audit fields, SMTP ambiguity and current retry classification |
| P4 | BP §18-§19 and BP §28; `backend/app/ai/persistence/tasks.py`; `backend/app/ai/persistence/service.py`; `backend/app/ai/persistence/reconciliation.py`; `backend/app/job_coordinator/registry.py`; `backend/app/job_coordinator/reconciliation.py` | Maintenance privacy, bounded sweeps, advisory locks, scheduling deduplication and missing durable run outcome |
| P5 | BP §28 and BP §35-§36; `backend/app/broker.py`; `backend/app/core/config.py`; `backend/app/observability/metrics.py`; `deploy/compose/compose.local.yml`; `deploy/compose/compose.hybrid-vps.yml`; `docs/operations.md`; `docs/backup-and-recovery.md`; locked Dramatiq source/API | Production Redis topology, fail-visible capacity behavior, supported queue observability, liveness, rollback and recovery documentation |
| P6 | BP §17, §29 and §30; `ARCHITECTURE.md` storage/AI flow; `backend/app/modules/files/service.py`; `backend/app/storage/s3.py`; `backend/app/ai/storage_resolver.py`; `backend/app/ai/streamed_source.py`; `backend/app/ai/persistence/queries.py`; `SECURITY.md` | File/scratch lifecycle authority, final-key signing, non-atomic completion, retention and quarantine policy |
| P7 | BP §7-§9 and §31; `ARCHITECTURE.md` invitation/platform flows; `backend/app/modules/invitations/service.py`; `backend/app/modules/platform_admin/service.py`; identity webhook services | Invite transition races, provider reconciliation and active last-admin invariant |
| P8 | BP §10-§11 and §22; `backend/app/modules/records/service.py`; `backend/app/modules/audit/models.py`; `backend/app/modules/audit/service.py`; `docs/backup-and-recovery.md` | Representative record concurrency, revision/erasure design and durable audit provenance |
| P9 | BP §30, §35 and §36; `deploy/caddy/Caddyfile`; `frontend/nginx.conf`; `deploy/compose/compose.hybrid-vps.yml`; `backend/app/main.py`; `backend/app/modules/ai_demo/router.py`; production environment example | Browser storage origin/CORS, trusted client IP and long-running AI request boundary |
| P10 | BP §8, §14, §31 and §41; `backend/app/modules/users/service.py`; `frontend/src/lib/permissions.ts`; `TEMPLATE_V0_8_SCOPE.md`; package manifests; `CONTRIBUTING.md` | Selected-org UI authority, version/upgrade truth and fresh-clone release proof |

## API, data and security impact

- **API/frontend:** P1-P5 retain the diff-free public contract. P8-P10 may add
  reviewed record version, large-AI job and per-org capability schemas; every
  endpoint has an explicit response model, security-suite coverage and
  regenerated frontend types. No unreviewed public break is authorised.
- **Database:** P1-P5 add `job_attempts`, `maintenance_runs` and minimal delivery
  state. P6-P8 may add scratch/upload intents, File identity/reconciliation,
  revision and identity safety constraints; every change gets an Alembic
  migration. No destructive migration or deletion of audit history is
  authorised without the explicit human retention/recovery decision.
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
- **Public compatibility:** no public cancellation/replay or generic admin
  bypass. New protected routes join `PROTECTED_ROUTES` in the mandatory suite;
  platform routes get non-admin and cross-plane denial cases and no `X-Org-Id`.

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
- **Document/storage tests:** real PostgreSQL File lifecycle gates for both AI
  modes; MinIO signed-PUT replay/version and same-size overwrite; completion
  rollback and concurrent replay; scratch null-retention/orphans and scanner
  unavailable. Cross-tenant and unknown-key cases fail closed.
- **Identity/audit tests:** two-session accept/revoke/admin/deactivation races,
  duplicate WorkOS webhook; stale record 409, revision reconstruction,
  actor/org deletion and DB append-only enforcement.
- **Browser/proxy/UI tests:** external-origin browser upload with scoped CSP/
  CORS, forged forwarded-IP denial, large AI ask bound/job path, and multi-org
  owner/viewer UI with matching backend 403.
- **Security/contract tests:** no tenant id from broker payload, no sensitive
  attempt/error content, all added routes in mandatory protected-route suite,
  per-org generated types reviewed, and P1-P5 public client diff-free.
- **Operational tests:** two Redis services, credentials, AOF/noeviction,
  backup/restore, rolling deployment, coordinator/worker restart and alerts for
  every new durable attention state.
- **Release/clone tests:** package/scope/tag consistency, v0.7-to-v0.8 upgrade
  guidance and fresh-clone migration/client/Compose/environment smoke proof.
- **Final gates:** after review findings are applied, run focused commands,
  then `make check` once plus every additional contract command. Do not weaken
  linting, typing, tests or security coverage.

## Review and delivery

- Execute P1-P5 in their dependency order. P6-P10 are separately reviewable
  work units with the dependencies above, not permission to combine security
  and job changes in one unreviewed commit. Each checkpoint follows
  implement → review → apply-and-commit under `CONTRIBUTING.md`.
- Keep this plan `Status: Draft` until the owner approves its decisions and
  human-review gates. Activation uses the exact transition to `Status: Active`;
  completion uses `Status: Complete` only after all evidence is reviewed.
- Never check boxes or commit before review. Preserve unrelated worktree changes
  and do not fold the current coordinator/logging edits into this plan unless
  their owner deliberately assigns them to a checkpoint.
- P1 stops before application for migration and terminal-semantics review. P2
  stops for tenant-isolation/recovery review. P3 stops for external-delivery
  review. P4 stops for cleanup/privacy recovery review. P5 stops for
  infrastructure, secrets and backup/recovery review. P6-P10 stop for their
  respective human-review gates, especially authentication, permission,
  tenant, public API, storage and audit-retention decisions.
- Recommended job rollout: migrations first; backward-compatible workers that
  can read old and new messages second; coordinator/attempt retry contract
  third; domain-fenced actors fourth; durable maintenance fifth; split Redis
  and metric cutover last. P6 document gating needs a non-destructive inventory
  and reconciliation of existing File/object states before enabling deny gates;
  P7 identity transitions need WorkOS/webhook rollback rehearsal; P8 revisions
  need a reviewed historical-data baseline; P9-P10 deploy only after the
  storage/proxy/API contracts are stable. Observe attention/backlog signals at
  each stage.
- Rollback must pause coordinator publication/reconciliation before reverting
  application containers. Preserve job, attempt, maintenance and outbox rows for
  roll-forward. Do not downgrade additive migrations in production merely to
  roll back application code.
- No major dependency, public API break, authentication, permission or tenant
  model change is automatically authorised by this draft. Discovery of such a
  requirement returns the affected checkpoint to explicit owner decision and
  human review. Preserve the existing historical `v0.8.0` tag.
- Completion requires AC1-AC22 evidence, real PostgreSQL/Redis/MinIO failure
  journeys, reviewed identity/storage/audit/infrastructure recovery procedures,
  green final gates, an independently cloneable release and honest statements
  of remaining exactly-once and deferred malware-provider limitations.
