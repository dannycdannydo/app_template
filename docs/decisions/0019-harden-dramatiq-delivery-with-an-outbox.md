# ADR 0019: Harden Dramatiq Delivery with a PostgreSQL Transactional Outbox

Status: Accepted

## Context

The template runs background work on the ADR-0004 stack (Dramatiq + Redis +
PostgreSQL). The durable `jobs` table (v0.5) records what work was accepted,
but the only durable guarantee ends at the row: the current
`create_and_enqueue` flow flushes the `queued` job, calls `Actor.send()` and
then commits PostgreSQL. Redis publication and the database transaction cannot
commit atomically, so a broker outage at the wrong moment loses the accepted
job, and the worker may observe a message before its job row is visible.

Operational documentation (docs/operations.md, docs/backup-and-recovery.md)
states that wiping Redis loses queued messages while job rows survive and must
be re-enqueued for continuity, but no automated mechanism re-enqueues them.

Separately, the durable worker has no execution ownership: `mark_running` lets
an already-running job enter another attempt, and a message delivered twice
(running again after a broker crash) can execute the same business work
concurrently. Domain-specific idempotency exists in individual actors but is
not a delivery guarantee.

## Options considered

1. **Redis as the source of truth** (rejected). Making Redis durable (AOF) or
   relying on broker persistence does not remove the dual-write window between
   PostgreSQL and Redis, and it cannot make tenant-scoped job history
   queryable or auditable.
2. **Replace the broker/worker stack** (rejected). Supabase Queues, dedicated
   job orchestrators and exactly-once brokers were considered and excluded:
   they add a new infrastructure dependency, contradict the accepted ADR-0004
   stack, and cannot make external side effects (email, providers) exactly-once
   anyway.
3. **Transactional outbox in front of the existing broker (chosen)**.
   PostgreSQL already holds the durable job row; the business change and the
   intent to publish are written together in one transaction. A coordinator
   process publishes outbox rows to Redis and settles their state. This is the
   blueprint's own §19 rule ("use the transactional outbox where missed
   delivery would matter") applied to the existing stack.

## Decision

Keep Dramatiq, Redis and PostgreSQL. Add a generic `outbox_events` table
(blueprint §19) and a standalone `coordinator` process that turns outbox rows
into Dramatiq messages. PostgreSQL is the source of truth for both the job and
the intent to publish it; Redis remains the transient execution broker.

Responsibilities:

- **PostgreSQL**: durable job rows, durable outbox rows, atomic
  job + event scheduling, execution ownership (dispatch identity and lease),
  reconciliation and retention.
- **Redis**: transient message transport only. Broker messages remain
  reference-only (durable jobs carry `job_id` only). Production gives the
  broker its own AOF-backed, `noeviction` Redis process and volume, separate
  from disposable rate-limit counters. Broker pressure therefore fails a
  publish visibly and leaves the outbox intent available for retry.
- **Coordinator**: claims due outbox rows in bounded batches, publishes them,
  settles publication state, retries temporary failures with capped backoff,
  and reconciles stranded `queued` jobs.
- **Worker**: executes business work under a database-enforced ownership
  contract; a duplicate message waits or becomes a no-op rather than running
  concurrently.

The resulting guarantee is at-least-once delivery with idempotent execution,
not exactly-once. A crash between Redis accepting a message and PostgreSQL
recording publication may duplicate a message, but ownership prevents two
copies from running the business task concurrently, and terminal settlement
makes the loser a no-op.

## 2026-09 amendment: PostgreSQL-owned attempts and retry decisions

The outbox alone made initial dispatch durable, but a transient failure still
returned a claimed job to `queued` and relied on Dramatiq retry delivery and
its `on_retry_exhausted` callback for the final outcome. The callback is a
separate Redis message and cannot be the durable finalization boundary.

Every successful claim now creates one internal `job_attempts` row in the
same transaction as the running job transition. The PostgreSQL attempt count
is the global limit. Progress renews both leases; success and permanent
failure close the current attempt with the job. A transient failure closes
the attempt and, in one transaction, either writes a delayed reference-only
outbox dispatch or fails the job and runs its allow-listed domain failure
hook. Retry scheduling and exhaustion therefore survive lost Redis messages.

The old zero-retry exhausted actor remains registered so messages from a
rolling deployment can be acknowledged or owner-checked and settled. New
durable actors no longer rely on it for correctness. The guarantee remains
at-least-once, and a superseded worker must still be fenced at each domain
mutation boundary (closure plan P2).

Rollout order: migration first, backward-compatible workers second, the
coordinator third, outbox-producing API last. Rollback pauses the coordinator
before reverting application containers; job and outbox rows remain durable
and the guarded recovery command republishes them after roll-forward.

## Consequences

- Every durable job creation writes the job and a `job.dispatch_requested`
  outbox event in one transaction; API paths no longer publish durable jobs
  directly to Redis.
- The allow-listed dispatch registry maps durable `job_type` values to actor
  messages; no actor/function name is ever resolved from persisted strings.
- Worker transitions verify the captured dispatch identity, so a stale or
  expired attempt cannot overwrite a newer owner.
- A new always-on `coordinator` process is added to both deployment profiles.
- At-least-once delivery means external side effects still rely on the
  existing provider/delivery idempotency rules; the plan does not claim
  globally exactly-once effects.
- No new third-party runtime dependency is required.
- Queue state is observed through payload-blind Redis cardinalities validated
  against the deliberately constrained Dramatiq 2.2.x line. Ready, delayed,
  in-flight and dead-letter signals never inspect message content.

This decision amends ADR-0004 (retain the Dramatiq stack) rather than
replacing it.

---
