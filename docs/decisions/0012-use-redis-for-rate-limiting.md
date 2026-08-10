# ADR-0012: Use Redis for distributed API rate limiting

## Decision

The API uses Redis and the official `redis` Python client for a coarse,
distributed `/api/v1` request limit. Production Redis must be protected in
transit and fails closed if Redis is unavailable. The hybrid VPS profile
(Scope §6.6) runs a private, password-protected Redis on the non-published
compose network, where plain `redis://` over the private network is
acceptable; any externally reachable Redis requires TLS (`rediss://`).
`backend/app/core/config.py` enforces this (`_redis_url_is_production_safe`),
a change recorded in the v0.6 release-gate follow-up (backup-and-recovery.md
run B, defect D1).

## Rationale

In-process counters reset on deploy and do not protect a horizontally scaled
deployment. Redis gives every API instance the same atomic counter without
placing an authentication or abuse-control responsibility in the frontend.
TLS for externally reachable Redis keeps credentials and traffic off
observable networks; a private compose-network Redis is already unreachable
from outside the host, so TLS there adds cost without reducing exposure.

## Consequences

Redis is now a required production dependency. The test profile deliberately
uses a no-op adapter so deterministic endpoint tests do not require a network
service; the real local stack includes Redis through Compose.
