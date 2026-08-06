# ADR-0012: Use Redis for distributed API rate limiting

## Decision

The API uses Redis and the official `redis` Python client for a coarse,
distributed `/api/v1` request limit. Production requires a TLS `rediss://`
connection and fails closed if Redis is unavailable.

## Rationale

In-process counters reset on deploy and do not protect a horizontally scaled
deployment. Redis gives every API instance the same atomic counter without
placing an authentication or abuse-control responsibility in the frontend.

## Consequences

Redis is now a required production dependency. The test profile deliberately
uses a no-op adapter so deterministic endpoint tests do not require a network
service; the real local stack includes Redis through Compose.
