# ADR 0004: Use Dramatiq for Background Jobs

Status: Accepted (amended 2026-09-17: durable delivery uses the PostgreSQL
outbox and a dedicated noeviction Redis broker; see ADR-0019)

## Context

The template needs durable, Redis-backed background job processing (email, imports/exports, notifications, file processing) without introducing a heavyweight distributed-systems dependency.

## Options considered

- **Dramatiq**: lightweight, Redis-backed task queue with a simple API, retries, and mid-level visibility; integrates with FastAPI processes and containers easily.
- **Celery**: the most established option, but heavier (separate broker semantics, larger dependency surface, more moving parts) for the needs of this template.
- **ARQ**: modern asyncio-native queue, but smaller ecosystem and fewer battle-tested integrations.
- **Built-in asyncio tasks**: no durability, no retries, no visibility; unsuitable for production work.

## Decision

Use **Dramatiq** with Redis as the broker for background jobs. Long-running work must be expressed as Dramatiq tasks, never as in-process asyncio tasks. Job records that the application needs to reason about are persisted durably (v0.5).

## Consequences

- The API and the worker run the same backend image with different commands (blueprint §35.1).
- Teams must follow the task-writing conventions (idempotency where possible, bounded retries, structured logging) defined in the blueprint §18.
- A dedicated Redis broker is required locally and in production. It is
  separate from rate-limit Redis; production uses AOF and `noeviction` so
  capacity failures reject publication instead of silently deleting work.
- Dramatiq is constrained to the reviewed 2.2.x line because payload-blind
  queue metrics rely on that version's Redis list/ack/dead-letter layout. A
  real-Redis contract test is required before widening the supported range.
- Durable delivery is hardened by a PostgreSQL transactional outbox
  (`outbox_events`) published by a dedicated `coordinator` process: PostgreSQL
  is the scheduling source of truth, Redis is transient execution transport,
  and delivery is at-least-once with database-enforced execution ownership.
  See [ADR-0019](0019-harden-dramatiq-delivery-with-an-outbox.md).

---
