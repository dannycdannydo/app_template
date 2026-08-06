# ADR 0004: Use Dramatiq for Background Jobs

Status: Accepted

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
- Redis is a required local and production service.

---
