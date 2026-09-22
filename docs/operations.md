# Operations Runbook — Hybrid VPS Profile

This runbook covers day-to-day operations of the generic Linux VPS /
container-host production profile (Scope §6.6, blueprint §35.1, ADR-0007).
It assumes a host running `deploy/compose/compose.hybrid-vps.yml` with an
environment file at `$DEPLOY_ROOT/.env.production` (default
`/opt/app-template`) and the release layout produced by
`.github/workflows/deploy-vps.yml`:

```text
/opt/app-template/
├── compose.hybrid-vps.yml   # copied by the deploy workflow
├── Caddyfile                # copied by the deploy workflow
├── .env.production          # secrets; chmod 600; off-site backup
├── .deploy.lock             # deployment lock (flock)
└── releases/
    ├── <git-sha>/           # immutable release: frontend/ + artifact
    └── current -> <git-sha> # atomic symlink flipped on each release
```

Every `docker compose` command below is run from the release directory:

```bash
cd /opt/app-template
export COMPOSE="docker compose -f compose.hybrid-vps.yml --env-file .env.production"
```

## Service model

| Service       | Image / command                                                                      | Scaling unit                                                            | Notes                                                                                                                                |
| ------------- | ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `caddy`       | custom image from `deploy/caddy/Dockerfile` (Caddy v2.11.4 + caddy-ratelimit v0.1.0) | 1 instance                                                              | Edge TLS, static frontend, `/api` proxy, security headers, edge rate limits                                                          |
| `api`         | backend image, `uvicorn app.main:app`                                                | replicas via `--scale api=N`                                            | Health-checked on `/ready`; Caddy load-balances across replicas                                                                      |
| `worker`      | backend image, `dramatiq app.workers --processes 1 --threads N`                      | `WORKER_CONCURRENCY` per process, `--scale worker=N` for more processes | Durable job pipeline (ADR-0004)                                                                                                      |
| `coordinator` | backend image, `python -m app.job_coordinator`                                       | normally 1; safe to replicate                                           | Claims PostgreSQL outbox rows, publishes reference-only broker messages, reconciles queued jobs and schedules maintenance (ADR-0019) |
| `redis-broker` | `redis:7-alpine`, password + AOF + `noeviction`                                      | 1 instance                                                              | Dedicated Dramatiq transport; never published                                                                                        |
| `redis-rate-limit` | `redis:7-alpine`, password + `allkeys-lru`                                        | 1 instance                                                              | Disposable distributed API counters; never published                                                                                 |

Initial defaults: 1 API replica, 1 worker process at `WORKER_CONCURRENCY=8`,
1 coordinator, 1 Caddy, and 2 isolated Redis services. Compose limits each service (CPU/memory) and rotates JSON
logs (`json-file` driver, `max-size`/`max-file` per service).

Human review record: Daniel approved the coordinator process, liveness probe,
resource/log limits, dependency order, graceful stop and rollout order on
2026-08-19. The project owner approved the local-only backup/recovery,
retention and guarded-reconciliation procedures on 2026-08-27.

The `caddy` service runs the pinned custom edge image
(`deploy/caddy/Dockerfile`: Caddy v2.11.4 + caddy-ratelimit v0.1.0). The
deploy workflow builds and publishes it beside the backend image as
`<registry>/<org>/<app>-caddy:<git-sha>` and passes it to the host as
`CADDY_IMAGE` at deploy time, so the compose `pull` always gets the real
image. When running `docker compose up` manually instead of through the
workflow, set `CADDY_IMAGE` in `.env.production` to the CI-published ref or
to an image built locally from the release checkout
(`docker build deploy/caddy`); the compose-file placeholder is not a real
image and will not pull.

## Database roles and row-level security (ADR-0022)

The production database uses separate credentials. `DATABASE_URL` is the
schema-owner/migration credential (`app_owner`, Alembic/DDL only) and
`DATABASE_RUNTIME_URL` is the restricted runtime credential (`app_runtime`) the
API and workers use. A production process refuses to start when the runtime
credential is unset (`app/db/session.py::resolve_database_url`), so a correctly
configured environment can keep the owner credential off the ordinary
application path. That is a configuration capability, not proof of separation:
the resolver does not inspect the credential, and an environment can still
point both URLs at the same role.

| Role | Credential | Used by | Notes |
| --- | --- | --- | --- |
| `app_owner` | `DATABASE_URL` | Alembic DDL/seed | Schema owner; never the runtime path |
| `app_runtime` | `DATABASE_RUNTIME_URL` | API and Dramatiq workers | Non-owner, non-superuser, no `BYPASSRLS`; subject to enabled policies |
| `app_metrics` | none (NOLOGIN) | `SECURITY DEFINER` aggregate metrics function | Non-owner, non-superuser, no `BYPASSRLS`; narrow policy scoped to attention-required delivery rows |
| `app_coordinator` | `DATABASE_COORDINATOR_URL` | Outbox coordinator, reliability-metrics refresh, `reconcile_jobs` CLI | Second non-bypass role, scoped to dispatch state |
| `app_operator` | `DATABASE_OPERATOR_URL` | Backup/restore and support CLI | Isolated, audited operational credential; owns no table, member of no application role; the one `BYPASSRLS` application role; loaded only by CLI/ops tooling |

Before enabling a table group, confirm in each environment that the two
credentials authenticate as distinct roles and that the runtime role cannot
bypass RLS. Connect separately with each credential and run:

```sql
SELECT current_user;   -- via DATABASE_URL -> app_owner
                       -- via DATABASE_RUNTIME_URL -> app_runtime (distinct)

SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
FROM pg_roles
WHERE rolname IN ('app_owner', 'app_runtime');
-- app_runtime must be rolsuper=false, rolbypassrls=false,
-- rolcreatedb=false, rolcreaterole=false.

SELECT count(*) FROM pg_tables
WHERE schemaname = 'public' AND tableowner = 'app_runtime';
-- must be 0: the runtime role owns no protected table.

SELECT granted.rolname
FROM pg_auth_members m
JOIN pg_roles granted ON granted.oid = m.roleid
JOIN pg_roles member  ON member.oid  = m.member
WHERE member.rolname = 'app_runtime';
-- must contain no superuser, BYPASSRLS or protected-table-owner role.
```

The check above is automated (RLS plan P4): `app.db.role_checks` connects with
each configured credential and proves, from the server catalogue, that the
authenticated role owns no table in `public`, carries none of
`SUPERUSER`/`BYPASSRLS`/`CREATEDB`/`CREATEROLE` and inherits no privileged role.
It runs automatically at production startup in **every normal runtime process**
and aborts it before any work is served when a credential fails: the API
(`create_app`'s lifespan), the Dramatiq worker (`app.workers.configure_worker`)
and the outbox coordinator (its `_async_main`). It is also available as a
pre-deployment command:

```bash
make verify-db-roles
# or: cd backend && uv run python -m scripts.verify_db_roles
```

The command exits non-zero on any violation and prints only role names and the
violated predicates, never credential material. The check deliberately connects
only with the runtime/coordinator credentials: a runtime URL that was
misconfigured to the schema owner is exactly what its ownership and `BYPASSRLS`
predicates reject.

The approved rollout order, per-group requirements and rollback procedure are in
`docs/rls-rollout.md`; the design is `docs/decisions/0022-postgresql-row-level-security.md`.

### Operational database access (`app_operator`)

Backup/restore, support and emergency access use the separate `app_operator`
credential (`DATABASE_OPERATOR_URL`), never the runtime or owner credential.
It is the only application role that may carry `BYPASSRLS`, because its reviewed
operations are exactly the cross-tenant reads a row policy cannot express; it
owns no table and has **no membership in either direction** (it inherits no
application role and no role is a member of it), and the runtime role
cannot `SET ROLE` it (`SET ROLE app_operator` as `app_runtime` must fail with
`permission denied to set role`).

- The credential is resolved only by `app.db.session.resolve_operator_database_url`;
  an unset value raises rather than falling back to the runtime or owner role,
  and no HTTP process or worker loads it.
- A logical backup (`pg_dump`) or a cross-tenant support read connects with this
  credential. It is granted `SELECT` on the application tables and sequences
  (`pg_dump` reads each sequence's `last_value`, so the sequence grant is what
  makes the backup executable); destructive restore remains an explicit,
  separately reviewed operation and follows `docs/backup-and-recovery.md`.
  `DATABASE_OPERATOR_URL` is a SQLAlchemy async URL, so libpq tools must use the
  plain `postgresql://` form (strip the `+asyncpg` suffix) as Procedure 1 shows.
- Every use is a privileged operational path: record who ran it, what operation
  was performed and against which environment. Do **not** place row contents,
  secrets, tokens or provider responses in the record or in an audit event
  (BP §28 never-log list).
- Verify the separation the same way as the runtime check above: connect with
  `DATABASE_OPERATOR_URL`, confirm `current_user = app_operator`, that
  `rolbypassrls = true` and `rolsuper = false`, that it owns no table, and that
  it inherits no application role (`docs/rls-rollout.md` §5). The automated
  startup/deployment check also proves the operator invariants from the runtime
  connection: `app_operator` is the only application role carrying `BYPASSRLS`,
  it owns no table and has no membership in either direction, and the runtime
  role cannot `SET ROLE` it.

## Scaling

### Scale API replicas

```bash
$COMPOSE up -d --scale api=3
```

Caddy's `reverse_proxy` load-balances `/api/*` across every `api` container
automatically. The API is stateless across containers (PostgreSQL is the
source of truth, Redis is shared), so replicas are safe. Health checks run per
container; an unhealthy replica is recreated by Compose.

To scale back down:

```bash
$COMPOSE up -d --scale api=1
```

### Scale the worker

Two dimensions — concurrency per process and number of processes:

```bash
# More threads per worker process (in .env.production):
WORKER_CONCURRENCY=16
# More worker processes:
$COMPOSE up -d --scale worker=3
```

All workers share the Redis broker and the same durable job table, so any
mix of processes is safe. Watch the job queue depth and delivery
`attempt_count` before scaling; if jobs back up, scale the worker, not the
API.

## Health and readiness

- `GET /health` — process liveness (always 200 when the API is up).
- `GET /ready` — full readiness (database reachable, service initialised);
  the deploy workflow and external uptime checks use this.
- `GET /metrics` — Prometheus text format (request counters/histograms, job
  counters); public like `/health`/`/ready` but edge-rate-limited.

Check state manually:

```bash
$COMPOSE ps
$COMPOSE logs --tail=100 api
$COMPOSE logs --tail=100 worker
$COMPOSE logs --tail=100 coordinator
```

## Monitoring

### External uptime checks

Point an external uptime monitor (UptimeRobot, Better Stack, Pingdom, or any
provider) at:

- `https://<DOMAIN>/ready` — alert on any non-200 within 2 consecutive checks.
- `https://<DOMAIN>/health` — same, as a secondary signal.

Alerting on the external service still counts as the readiness/API failure
alert; the VPS host itself is what these checks cover.

### Metrics scraping

Scrape `https://<DOMAIN>/metrics` with any Prometheus-compatible scraper.
Rate-limit the scraper account/IP if the provider supports it; the edge zone
allows 600 events/min. Metric families (blueprint §28,
`app/observability/metrics.py`): `http_requests_total` (labels `method`,
`path` normalised to `{id}`, `status_code`), `http_request_duration_seconds`
(`method`, `path`) and the job counters `jobs_enqueued_total`,
`jobs_succeeded_total`, `jobs_failed_total` (label `job_type`) and
`jobs_stale_messages_total` (messages discarded after bounded retries because
their durable PostgreSQL row is absent). Recommended alerts:

- `http_requests_total` error rate (`status_code` 5xx) above 1% over 10 min.
- `http_requests_total` per `path` traffic collapse (silent API).
- `jobs_failed_total` growth; alert on failed growth.
- Any `jobs_stale_messages_total` growth; investigate broker/database state
  drift, retention or an unsupported partial reset.
- `http_request_duration_seconds` `p95` above the SLO threshold (default 1 s).

The AI layer adds its own families (`ai_requests_total`,
`ai_request_duration_seconds`, `ai_tokens_total`, `ai_cost_total`,
`ai_validation_failures_total`, `ai_retries_total`, `ai_fallbacks_total`,
`ai_budget_denials_total`) and alerting/runbook guidance — see
"AI observability and runbooks (v0.7)" below.

### Alerts to configure

| Alert                      | Signal                                                                            | Severity               |
| -------------------------- | --------------------------------------------------------------------------------- | ---------------------- |
| Readiness / API failure    | `/ready` non-200 from the uptime monitor or scraper                               | critical               |
| Worker / job failures      | `jobs_failed_total` rising; delivery rows `failed`                                | critical               |
| Stale worker messages      | `jobs_stale_messages_total` rising                                                | warning                |
| Disk pressure              | host disk or Caddy/Redis log volumes ≥ 80%                                        | warning (90% critical) |
| Certificate expiry         | Let's Encrypt renewal failures in Caddy logs; cert expiry within 14 days          | critical               |
| Backup failure             | failed backup job / missing backup marker (docs/backup-and-recovery.md)           | critical               |
| Redis unavailable          | API `rate_limiter_unavailable` errors; `redis-cli ping` failure                   | critical               |
| Broker memory / rejection  | broker memory > 80% for 10 min; OOM error replies increase                        | warning / critical     |
| Queue metric refresh       | refresh success 0 or last success older than 90 s                                 | critical               |
| Expired running attempts   | `stale_running_jobs > 0` for 5 min                                                | warning                |
| Retry / exhaustion event   | `job_attempts{status=~"retry_scheduled|exhausted"}` increases                    | warning                |
| Ambiguous email            | `attention_required_email_deliveries > 0`                                         | critical               |
| Failed / stale maintenance | failed run count increases or stale running count is non-zero                      | warning                |
| Dead current dispatch      | `dead_current_job_dispatches > 0`                                                 | critical               |
| Outbox publication backlog | `outbox_oldest_due_age_seconds` > 300 s (warning), > 900 s (critical)             | warning / critical     |
| Dead outbox events         | `sum(outbox_events{status="dead"}) > 0`                                           | critical               |
| Queued-job recovery        | `stale_queued_jobs > 0` for 15 min                                                | warning                |
| Coordinator unavailable    | coordinator healthcheck failing or no `coordinator.cycle_completed` log for 2 min | critical               |

## Durable job delivery runbook

PostgreSQL is the scheduling source of truth; Redis is transient execution
transport. Scrape these database-backed gauges from `/metrics`: `outbox_events`
(`status`, `event_type` only), `outbox_oldest_due_age_seconds`, and
`stale_queued_jobs`, `stale_running_jobs`, `job_attempts`,
`attention_required_email_deliveries`, `maintenance_runs`, and
`dead_current_job_dispatches`. They deliberately never label an organisation, job id,
payload, error or provider reference. Under group-2 RLS the ambiguous-email
count is read through the non-bypass `app_attention_required_delivery_count()`
aggregate, so the gauge reports the true cross-tenant count rather than zero.

When a delivery alert fires:

1. Inspect `docker compose logs --tail=200 coordinator worker` and the gauge
   values. Do not copy outbox payloads or stored errors into tickets.
2. If the coordinator is unhealthy, restore PostgreSQL/Redis connectivity and
   restart only the coordinator; pending rows remain durable and will publish.
3. For `dead` events, investigate the allow-listed event/job contract before
   any repair. They are intentionally never auto-replayed.
4. For stale queued jobs, run `make jobs-reconcile` first. It is read-only and
   prints only opaque job ids. After confirming the candidates and cause, use
   `CONFIRM_RECONCILE=1 make jobs-reconcile-apply`; repeated application is
   idempotent because the same bounded reconciliation service creates
   deduplicated dispatch intents.
5. Escalate if the oldest due age remains critical after broker recovery, dead
   events grow, or reconciliation candidates recur after the cooldown. Preserve
   PostgreSQL/outbox rows for diagnosis; do not clear Redis to "fix" a backlog.

Privacy-safe investigation queries (return only opaque ids, timestamps and
closed operational states):

```sql
SELECT id, job_id, attempt_number, status, lease_expires_at, completed_at, error_code
FROM job_attempts
WHERE (status = 'running' AND lease_expires_at < now())
   OR status = 'exhausted'
ORDER BY started_at;

SELECT id, task_type, status, scheduled_for, lease_expires_at, error_code
FROM maintenance_runs
WHERE status = 'failed'
   OR (status = 'running' AND lease_expires_at < now())
ORDER BY scheduled_for;

SELECT j.id AS job_id, o.id AS dispatch_id, o.processed_at, o.error_code
FROM jobs j JOIN outbox_events o ON o.id = j.dispatch_id
WHERE j.status = 'queued' AND o.status = 'dead';
```

Restore database/broker connectivity and let the coordinator perform its
owner-fenced recovery. A recurring expired lease requires worker diagnosis;
an exhausted attempt is terminal and must not be replayed by hand. A dead
current dispatch should normally already have settled its job; preserve both
rows and escalate if the last query returns anything. The checked-in baseline
rules are `deploy/monitoring/prometheus-alerts.yml`; load them into the chosen
Prometheus-compatible monitoring service and keep environment thresholds in
the deployment runbook.

The coordinator deletes only one bounded batch of `published` outbox rows
older than 30 days each UTC cleanup interval (settings:
`OUTBOX_RETENTION_DAYS`, `OUTBOX_CLEANUP_BATCH_SIZE`,
`OUTBOX_CLEANUP_INTERVAL_HOURS`). A durable cleanup-bucket marker makes this
cadence safe across coordinator replicas and restarts. Pending, publishing and
dead rows are retained.

### Acceptance-unknown email

An email delivery with `status = 'attention_required'` crossed the durable
submission boundary, but the worker could not prove whether the SMTP relay
accepted it. The worker terminally fails the associated job with
`email_delivery_acceptance_unknown`; it never resends the delivery
automatically. The delivery's stable `delivery_identity` maps to the SMTP
`Message-ID` as `<delivery_identity@configured-smtp-host>`.

When this alert occurs:

1. Find affected rows with a privacy-safe query (run through your normal
   read-only database access):

   ```sql
   SELECT id, delivery_identity
   FROM notification_deliveries
   WHERE status = 'attention_required'
   ORDER BY id;
   ```

   Record only the opaque delivery id and `delivery_identity`; do not copy the
   recipient, subject, body, provider response or credentials into tickets.
2. Search the relay's delivery/activity log for that exact Message-ID. Treat a
   matching accepted event as sent; absence is not proof of non-acceptance
   until the provider's documented log-retention and ingestion delay have been
   checked.
3. Do not update the row or enqueue the job directly. This release deliberately
   has no generic replay/resolution API. Resolution or resend requires a
   separately reviewed, authenticated operator workflow that records who
   verified the provider evidence and preserves the original delivery row.
4. If provider evidence is unavailable, leave the row attention-required and
   communicate the uncertainty through the application-specific support path.

## Redis

The profile has two private, password-protected Redis processes. `redis-broker`
has its own 200 MB volume, AOF persistence and `noeviction`; its credentials
are `BROKER_REDIS_PASSWORD` / `BROKER_REDIS_URL`. `redis-rate-limit` has a
separate 64 MB volume and `allkeys-lru`; its credentials are
`RATE_LIMIT_REDIS_PASSWORD` / `RATE_LIMIT_REDIS_URL`. Production startup
rejects the same normalised host/port for both, even if DB numbers differ.

### One-time two-Redis cutover

Before deploying the release that replaces the former single `redis` service,
add `BROKER_REDIS_PASSWORD`, `RATE_LIMIT_REDIS_PASSWORD`,
`BROKER_REDIS_URL` and `RATE_LIMIT_REDIS_URL` to `.env.production`; validate
the Compose configuration before running `up --remove-orphans`. The deployment
starts a fresh `redis_broker_data` volume and a separate disposable counter
volume. It leaves the former `redis_data` volume intact but unreferenced.

Drain workers when practical, then deploy the API, worker and coordinator
together. Messages that were only in the old broker are not copied. This does
not lose the work request: PostgreSQL retains the durable job and outbox intent,
and the coordinator replaces stranded queued or lease-expired running
dispatches after the configured reconciliation threshold and cooldown.

To roll back, stop the coordinator, restore the previous release and its
Compose file, restore the old `REDIS_PASSWORD` / `REDIS_URL` settings, then
recreate the stack. The previous `redis` service reattaches its unchanged
`redis_data` volume. Keep that volume until the new release has passed its
normal rollback-retention window and all stranded durable jobs have been
reconciled; only then may an operator remove it deliberately.

### Graceful shutdown

`docker compose stop redis-broker` runs SIGTERM and flushes its AOF before
exit; `docker compose restart redis-broker` is safe. Restarting the rate-limit
service resets or evicts only counters.
The API fails closed when Redis is unavailable (`rate_limiter_unavailable`, 503) rather than silently dropping the abuse control — a deliberate choice.

### Consequences of Redis loss

- **Broker**: queued Dramatiq messages are lost; jobs already delivered to a
  worker continue. PostgreSQL retains each job and its outbox publication
  intent. After Redis returns, the coordinator automatically creates a
  cooldown-limited replacement dispatch for eligible stranded `queued` jobs
  and lease-expired `running` jobs under a rotated owner fence.
- **Rate limiting**: API traffic fails closed with 503 until Redis returns
  (the rate limiter is the only Redis consumer at the edge; `RATE_LIMIT_REDIS_URL`
  connectivity is the dependency).
- **Persistence and pressure**: broker AOF survives restarts, but its volume
  is not a source of truth. At 200 MB, `noeviction` rejects new messages;
  publication fails visibly and the pending PostgreSQL outbox row retries.
  Rate-limit counter loss does not affect broker state.

## Records, revisions and audit integrity (plan P8)

- **A `409 record_version_conflict` is expected behaviour**, not an incident. It
  means two users edited the same record; the UI reloads and the user retries.
  No operator action is needed. A spike is useful product signal, not a fault.
- **`audit_events` and `record_revisions` are append-only.** Row-level
  `BEFORE UPDATE OR DELETE` triggers plus statement-level `BEFORE TRUNCATE`
  triggers reject any mutation with an
  `... is append-only and cannot be modified` database error (ADR-0020). Never
  try to correct history in place, and do not `TRUNCATE` these tables; the
  ledger is the provenance.
- **Record deletion is permanent and has no restore operation.** The immutable
  revisions are reconstruction evidence, not a programmatic restore: a deleted
  record stays 404 and `service.restore_record` rejects restoration with
  `record_restore_unsupported`. Re-creating content produces a new record id at
  version 1; never re-insert a deleted record id by hand.
- **Actor/organisation ids on these rows are opaque UUIDs** and may reference a
  user or organisation that was hard-deleted. That is intended: the identity
  survives the referent. A missing join row is not corruption.
- **Tenant or actor erasure is a reviewed purge**, not a cascade. Deleting an
  organisation does not touch these tables; erasing business history requires
  deliberately disabling the trigger under the retention decision in ADR-0020
  and `docs/backup-and-recovery.md`.
- Useful read-only checks:
  `SELECT count(*) FROM record_revisions;`
  `SELECT max(created_at) FROM audit_events;`
  `SELECT action, count(*) FROM audit_events GROUP BY action ORDER BY 2 DESC;`

## Trusted proxy and client-IP handling

Caddy is the single TLS-terminating edge. In `compose.hybrid-vps.yml` Caddy and
the API share the private `edge` network (`EDGE_SUBNET`, default
`172.30.0.0/24`), while the worker, coordinator and both Redis instances sit on
the separate private `backend` network. The API starts with
`--proxy-headers --forwarded-allow-ips=<edge subnet>`, so:

- **Only Caddy is trusted for forwarded headers.** uvicorn rewrites
  `request.client` from `X-Forwarded-For` only when the TCP peer is inside the
  edge subnet. A browser cannot connect to the API directly (the API port is
  never published), and if one could, its `X-Forwarded-For` would be ignored.
- **A spoofed chain cannot move the client IP.** Caddy appends the real client
  address, so the resolved value is the right-most untrusted entry; a
  browser-supplied `X-Forwarded-For: 10.0.0.1` is discarded. Never set
  `--forwarded-allow-ips=*`: uvicorn would then trust the left-most
  (browser-supplied) value and per-client limits would be forgeable. The
  contract is asserted by `backend/tests/test_proxy_trust.py` and
  `scripts/assert_deployment_boundaries.py`.
- **Both limiters are per-client-IP.** The edge limit in the Caddyfile keys on
  `{remote_host}` and the application limiter (`app/core/rate_limit.py`) keys
  on `request.client.host`, which is now the real client IP. Distinct clients
  therefore keep distinct quotas.
- **Override `EDGE_SUBNET` in one place.** If `172.30.0.0/24` collides with a
  host network, set `EDGE_SUBNET` in `.env.production`; the API command and the
  `edge` network both read it. CI validates that the two stay identical.

## Browser storage access (CSP and CORS)

Browser uploads and downloads go **directly** to the private object store via
short-lived signed URLs, so the browser must be allowed to reach that origin:

- **CSP `connect-src`.** `deploy/caddy/Caddyfile` injects
  `{$STORAGE_PUBLIC_ORIGIN}` into the `connect-src` directive. Set
  `STORAGE_PUBLIC_ORIGIN` in `.env.production` to the bare
  `scheme://host[:port]` origin of `STORAGE_PUBLIC_ENDPOINT_URL` — no path,
  query string, fragment, credentials, wildcard or trailing slash. Both values
  are required by the hybrid compose edge (fail-fast), CI asserts
  `STORAGE_PUBLIC_ORIGIN` is exactly the origin of `STORAGE_PUBLIC_ENDPOINT_URL`
  (`scripts/assert_deployment_boundaries.py`), and the local `make dev-docker`
  nginx template uses the same value via `NGINX_ENVSUBST_FILTER`.
- **Object-store CORS.** Configure this at the provider (S3/R2/B2/Spaces): allow
  methods `PUT`, `GET`, `HEAD`; allow the exact frontend origins from
  `CORS_ALLOWED_ORIGINS`; allow request header `Content-Type` (plus any
  provider signing header such as `x-amz-*`). Never use `*`. Expose `ETag` only
  if the client verifies checksums. A wrong CORS policy is visible in the
  browser console as a preflight failure before the API receives the request.
  MinIO configures CORS server-wide rather than per bucket, so the local
  `compose.local.yml` passes `STORAGE_CORS_ALLOWED_ORIGIN` as
  `MINIO_API_CORS_ALLOW_ORIGIN` (default `http://localhost:5173`); CI starts
  MinIO with the same setting. The MinIO-backed `storage_integration` suite
  proves from the browser's perspective that the preflight and the direct
  signed `PUT` from the authorised origin succeed while a forbidden origin is
  refused (`backend/tests/test_storage_integration.py`), and the Playwright
  `ai-ask` journey proves the browser itself enforces the storage origin's CORS
  (positive and negative) against a real external storage server
  (`frontend/e2e/ai-ask.spec.ts`, `frontend/e2e/storage-server.mjs`).
- **WorkOS stays allowed.** The CSP keeps `https://api.workos.com` and `'self'`
  unchanged; adding the storage origin must not widen or wildcard either.

## Edge rate limiting

Implemented in `deploy/caddy/Dockerfile` (pinned Caddy v2.11.4 +
`mholt/caddy-ratelimit` v0.1.0) and configured in `deploy/caddy/Caddyfile`:

- `/api/*`, `/health`, `/metrics`: 600 events/min per client IP (looser than
  the application's 300/min on `/api/v1`).
- Static assets: 2400 events/min per client IP.
- `/ready`: unlimited, so deployment health checks are never throttled.

Tune the numbers in the Caddyfile and re-run CI (`caddy validate` job) before
deploying. An external WAF (e.g. Cloudflare) can sit in front instead; keep
TLS termination and the security headers at Caddy and document the WAF rules
in this runbook.

## AI observability and runbooks (v0.7, v0.8 large-file transfer modes)

The AI layer (v0.7 Scope §6.7, ADR-0017) emits its own metric families on
`GET /metrics`, binds `ai_request_id` to every AI log line, and keeps Sentry
free of prompts, provider responses and document content (the shared
`before_send` redaction applies; AI errors are handled, so they never reach
Sentry as unhandled exceptions). v0.8 adds the large-file transfer metrics
(mode selection, lifecycle outcomes, reconciliation sweep and cleanup
backlog) with the same redaction invariants. The durable `ai_requests` /
`ai_outputs` rows and the v0.8 `ai_attachment_references` rows are the
per-request source of truth; the counters below are the aggregate signal,
labelled only with low-cardinality registry ids (task/provider/model/mode) —
organisation ids, request ids, object keys, URLs and content never become
labels.

### AI metrics families

| Metric                         | Type      | Labels                                                       | Meaning                                                                                                                                                                                      |
| ------------------------------ | --------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ai_requests_total`            | counter   | `task`, `provider`, `model`, `status` (`succeeded`/`failed`) | provider executions by terminal outcome, one sample per settled attempt                                                                                                                      |
| `ai_request_duration_seconds`  | histogram | `task`, `provider`, `model`                                  | provider execution latency                                                                                                                                                                   |
| `ai_tokens_total`              | counter   | `task`, `provider`, `model`, `direction` (`input`/`output`)  | tokens consumed                                                                                                                                                                              |
| `ai_cost_total`                | counter   | `task`, `provider`, `model`                                  | spend in USD, priced with the registry's reviewed rates (the registry's single pricing currency)                                                                                             |
| `ai_validation_failures_total` | counter   | `task`, `provider`, `model`                                  | structured-output validation failures (each followed by one bounded repair or a task retry)                                                                                                  |
| `ai_retries_total`             | counter   | `task`, `provider`, `model`                                  | bounded retry dispatches after the first (including repair dispatches)                                                                                                                       |
| `ai_fallbacks_total`           | counter   | `task`, `provider`, `model`                                  | reviewed provider/model fallbacks under the task's fallback policy                                                                                                                           |
| `ai_budget_denials_total`      | counter   | `task`                                                       | monthly organisation budget denials before dispatch                                                                                                                                          |
| `dramatiq_queue_depth`         | gauge     | `queue`                                                      | compatibility alias for the ready count |
| `dramatiq_queue_messages`      | gauge     | `queue`, `state` (`ready`/`delayed`/`in_flight`/`dead`)      | payload-blind broker cardinalities from the locked Dramatiq Redis layout |
| `dramatiq_queue_metrics_refresh_success` | gauge | none                                                | 1 for the last successful refresh, otherwise 0 |

#### v0.8 large-file transfer metrics

| Metric                             | Type    | Labels                       | Meaning                                                                                                              |
| ---------------------------------- | ------- | ---------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `ai_transfer_selections_total`     | counter | `mode`, `provider`           | selected transfer mode by mode and provider (`inline`, `provider_upload`, `managed_signed_url`, `storage_reference`) |
| `ai_transfer_outcomes_total`       | counter | `mode`, `provider`, `result` | transfer lifecycle outcomes (upload/stage success, reuse, expiry, terminal deletion, deletion failure)               |
| `ai_transfer_reconciliation_total` | counter | `provider`, `result`         | provider-file reconciliation sweep outcomes (`deleted`/`failed` per claimed reference)                               |
| `ai_transfer_cleanup_backlog`      | gauge   | `mode`                       | provider-file references currently waiting on the reconciliation sweep (see the cleanup-backlog runbook below)       |

The durable `ai_attachment_references` rows are the per-request source of truth
for transfer state; these counters are the aggregate signal. Labels are
low-cardinality mode/provider ids and safe outcome names only — never object
keys, external file ids, `gs://` URIs, managed signed URLs, request ids or
content (BP §28, Scope §2.3).

### AI dashboard contract

The template defines its dashboard here rather than shipping a Grafana JSON
file so any Prometheus-compatible frontend (Grafana, managed dashboards) can
implement it. One panel per row; every query is a PromQL expression over the
families above, with a corresponding alert rule (aggregate table below).

| Panel                 | PromQL query                                                                                                                | Type             | Alert rule                                                             |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------- | ---------------- | ---------------------------------------------------------------------- |
| Provider success rate | `1 - sum(rate(ai_requests_total{status="failed"}[10m])) / sum(rate(ai_requests_total[10m]))`                                | gauge (0-1)      | `< 0.95` (provider outage, critical)                                   |
| Provider latency p95  | `histogram_quantile(0.95, sum(rate(ai_request_duration_seconds_bucket[10m])) by (le, task))`                                | gauge (s)        | `> 30` (warning; per-task override)                                    |
| Token throughput      | `sum(rate(ai_tokens_total[10m])) by (direction)`                                                                            | gauge (tokens/s) | trend only                                                             |
| Spend rate            | `sum(rate(ai_cost_total[10m]))`                                                                                             | gauge (USD/s)    | daily-normalised `ai_cost_total` rate above budget threshold (warning) |
| Validation failures   | `sum(rate(ai_validation_failures_total[10m]))`                                                                              | gauge (events/s) | rising, or retry/repair ratio `> 0.2` of requests (warning)            |
| Retry/fallback ratio  | `sum(rate(ai_retries_total[10m])) / clamp_min(sum(rate(ai_requests_total[10m])), 1e-9)`                                     | gauge (ratio)    | `> 0.2` (warning)                                                      |
| Budget denials        | `sum(rate(ai_budget_denials_total[10m]))`                                                                                   | gauge (events/s) | `> 0` (warning; info if deliberate)                                    |
| Transfer failure rate | `sum(rate(ai_transfer_outcomes_total{result="failed"}[10m])) / clamp_min(sum(rate(ai_transfer_outcomes_total[10m])), 1e-9)` | gauge (ratio)    | `> 0.05` over 10 min (warning; upload/staging/deletion health)         |
| Cleanup backlog       | `ai_transfer_cleanup_backlog`                                                                                               | gauge            | `> 10` for `> 15 min` (warning; provider files persist until deleted)  |
| AI queue backlog      | `dramatiq_queue_messages{queue="ai",state="ready"}`                                                                       | gauge (messages) | `> 10` for `> 5 min` (warning)                                         |

Queue-state source: the API uses the checked-in adapter for the locked
Dramatiq 2.2.x Redis layout. It issues only `LLEN`, `SCARD`, `ZCARD`, `SCAN`
for acknowledgement-set names, and Redis `INFO`; it never reads message ids
or bodies. A mismatch or outage sets the refresh-success gauge to zero and
leaves previous state samples intact.

### AI alerts to configure

| Alert                  | Signal                                                                                                                                                        | Severity                                                                        |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| Provider outage        | `ai_requests_total{status="failed"}` error rate above threshold (e.g. 5% over 10 min, or a step change in `provider_unavailable`/`provider_timeout` failures) | critical                                                                        |
| Provider latency       | `ai_request_duration_seconds` `p95` above the SLO threshold (default 30 s; raise per task/provider)                                                           | warning                                                                         |
| Validation degradation | `ai_validation_failures_total` rising or repair/retry ratio above threshold (e.g. > 20% of requests)                                                          | warning                                                                         |
| Spend spike            | `ai_cost_total` rate above the daily budget-normalised threshold                                                                                              | warning                                                                         |
| Budget denials         | `ai_budget_denials_total` growth (users hitting the monthly cap)                                                                                              | warning (info if deliberate)                                                    |
| Transfer failures      | `ai_transfer_outcomes_total` deletion-failure / upload-failure rate above threshold (e.g. > 5% over 10 min)                                                   | warning                                                                         |
| Cleanup backlog        | `ai_transfer_cleanup_backlog` above threshold (e.g. > 10) for > 15 min, or `ai_transfer_reconciliation_total` deletion failures rising                        | warning (provider files persist until deleted; see the cleanup-backlog runbook) |
| Queue backlog          | `dramatiq_queue_messages{queue="ai",state="ready"}` above threshold (e.g. 10) for > 5 min (AI jobs are durable; see "Consequences of Redis loss")       | warning                                                                         |

### AI runbooks

Runbook sections assume the release directory and `$COMPOSE` alias from the
top of this document.

#### Provider outage

1. **Confirm**: `ai_requests_total{status="failed"}` error rate rises with
   `provider_unavailable`/`provider_timeout`/`provider_rate_limited` in
   `worker` logs; check the provider status page before acting.
2. **Contain**: if the task's reviewed `fallback_policy` allows it, routing
   already falls back to an eligible model **within the same region** (never
   implicitly across regions). If fallback is disabled or exhausted, requests
   fail fast with a safe error — no retry storm (bounded by the task's
   `retry_policy`, not the broker).
3. **Respond**: for a short outage, wait it out (bounded retries absorb
   transient blips). For a longer one, flip the reviewed model registry
   configuration to an eligible alternative and redeploy (see Model rollback
   below); the change is configuration-only, no feature-code change.
4. **Resolve**: restore the primary model once the provider recovers and
   re-run the full `make check` gate before deploying.

#### Budget response

1. **Confirm**: `ai_budget_denials_total` rises; the organisation's
   `organisation_ai_settings` row has `monthly_budget` set and the denial is
   audited (`ai.budget_denied` audit events identify actor and task; the
   `ai_requests` spend sum for the current UTC month is the source of truth).
2. **Assess**: is the spend legitimate (a feature scaling up) or unexpected
   (a cost spike, a misconfigured model/prompt)? Compare `ai_cost_total` per
   task/provider/model against the budget.
3. **Act**: raise the monthly budget through the platform AI-settings API
   (recorded, audited `ai.settings_updated`), or leave the cap in place if
   the denial is correct behaviour. Budgets are reserved before dispatch
   under the settings-row lock, so the cap cannot be overrun by concurrent
   requests; changing the cap takes effect on the next reservation.

#### Prompt rollback

Prompts are append-only and versioned: correcting a prompt creates
`*_vN.yaml` (a new immutable version), never an edit to a released version.
A task pins `prompt_name` + `prompt_version`; the version is part of the
routing metadata, the durable `ai_requests` rows and the audit events.

1. **Confirm**: `ai_validation_failures_total` or poor-quality output
   correlated with a prompt version deployed in the last release.
2. **Roll back**: point the task at the previous released prompt version in
   the checked-in task configuration (`app/ai/tasks/`), run
   `make validate-ai-registries`, and deploy through the normal reviewed
   workflow. The registry validates at startup/CI, so an invalid pin cannot
   ship.
3. **Resolve**: investigate and release the corrected prompt as a new
   version; never rewrite the released one.

#### Model rollback

The model registry (`app/ai/models/`) is checked-in configuration: the router
selects the task's eligible model ordered by the configured tier/policy. A
task change can move between eligible OpenAI, Anthropic, Azure OpenAI or
Vertex Gemini models through reviewed configuration without feature-code
changes; document input can never route to a model lacking the `documents`
capability, and fallback never changes a provider's configured region.

1. **Confirm**: `ai_requests_total{status="failed"}` or latency/cost anomaly
   correlated with a model introduced in the last release.
2. **Roll back**: point the task's default/fallback ordering at the previous
   eligible model (or change the organisation's `model_override` through the
   platform AI-settings API), run `make validate-ai-registries`, and deploy.
3. **Resolve**: investigate and re-enable the model in a later reviewed
   release; keep the pricing metadata's effective dates accurate.

#### Retention deletion

The retention job (`ai.retention`, `ai` queue) enforces two independent
controls:

- **Global scratch expiry (plan P6)**: every durable scratch intent past its
  bounded `expires_at` is marked `expired` and its object deleted best-effort.
  This runs for every organisation, independent of any per-organisation
  `retention_policy_days`, so a scratch object can never outlive
  `AI_SCRATCH_MAX_LIFETIME_SECONDS` (tightened by any shorter per-org policy).
  Once the intent is terminal the bytes are already unauthorised by the source
  authority.
- **Per-organisation output retention**: for organisations with
  `retention_policy_days` configured, expired `ai_outputs` rows (and the
  organisation-scoped AI scratch objects they reference) are deleted, and the
  scratch namespace is swept page by page for objects older than the policy.

The job also reconciles stale `running` requests to `failed` (keeping their
reserved cost) and writes one `ai.retention_deleted` audit event per purge.
Keep-flow objects under `organisations/{org}/documents/…` are never touched.

1. **Confirm**: `ai.retention.*` worker logs report the sweep summary
   (`scratch_intents_expired` counts the global expiry sweep);
   `ai.retention_deleted` audit events record the purge.
2. **Act**: output retention is a privacy control changed through the platform
   AI-settings API (audited). The global scratch ceiling
   `AI_SCRATCH_MAX_LIFETIME_SECONDS` is a deployment setting, not
   per-organisation; changing it requires a reviewed redeploy. The sweep pages
   the scratch namespace (`start_after` paging), so any namespace size is fully
   swept.
3. **Verify**: spot-check that expired rows and scratch objects are gone and
   keep-flow objects remain; the audit event per purge and the
   `scratch_intents_expired` summary are the evidence trail.

#### Scratch and orphaned object cleanup (plan P6, AC19)

The application's intent sweep is the primary cleanup and runs globally; the
object-store lifecycle rules documented in `.env.production.example` are the
asynchronous backstop. S3-compatible lifecycle prefixes are literal, so a rule
is configured **per organisation** with the exact
`organisations/<organisation-id>/ai/scratch/` prefix and an expiration at
least `AI_SCRATCH_MAX_LIFETIME_SECONDS` — never a wildcard and never shorter
than the configured maximum.

1. **Confirm**: an expired intent is `status = 'expired'` in
   `ai_scratch_uploads`. If objects remain under a scratch prefix after their
   intent is terminal, the best-effort provider delete failed or no lifecycle
   rule is configured for that organisation.
2. **Repair**: configure/verify the per-organisation lifecycle rule (see
   `.env.production.example`) with an expiration >= the configured maximum; the
   next sweep retries deletion. Abandoned document staging objects under
   `organisations/<org>/documents/<file-id>/staging/` are removed best-effort
   at completion and may additionally be aged out by a short (for example
   1-day) staging rule — they are never served. Never age out the final
   `documents/<file-id>/original` prefix.
3. **Inventory promoted orphans**: list the bucket's `organisations/` prefix
   and compare it with the live rows
   (`SELECT id, object_key FROM files WHERE deleted_at IS NULL;`). A final
   object with no matching live row is an orphan. Remove it only after
   confirming the file is soft-deleted and is not required for a restore:
   object deletion is destructive and recoverable only through object-store
   versioning/backup, if enabled.

**Retention/restore boundary.** Scratch and staging objects are transient:
they are not backed up and are not restorable, and deletion of them is the
reviewed cleanup boundary. Final document objects are removed only by the API
delete path or a reviewed operator action against a confirmed orphan; a
restore procedure must not resurrect scratch/staging objects or an orphan that
was deliberately removed.

#### Cleanup backlog (provider-file reconciliation, v0.8)

Transient OpenAI/Anthropic uploads are deleted best-effort at the terminal
outcome. When a deletion fails (provider outage, transient API error, worker
crash between terminal outcome and delete), the provider-hosted file persists
and the reference waits on the reconciliation sweep
(`reconcile_provider_file_references`, `ai` queue, `AI_RECONCILE_BATCH_SIZE`
per run, `AI_RECONCILE_RETRY_AFTER_SECONDS` minimum backoff). The sweep
deletes only provider-hosted files whose owning request is terminal; it never
processes managed signed URLs (no provider copy), Vertex GCS staging objects
(deployer-owned lifecycle backstop) or feature-owned sources.

The coordinator creates durable `maintenance_runs` and reference-only outbox
events for `ai.retention` and provider-file reconciliation on their typed UTC
schedule. Do not call actor `.send()` from cron or scripts: bypassing the run
ledger defeats completion, retry and exhaustion visibility. Tune the typed
hourly/daily intervals and let the coordinator schedule them.

1. **Confirm**: `ai_transfer_cleanup_backlog` is above the threshold for more
   than a few sweep cycles, `ai_transfer_reconciliation_total{result="failed"}`
   is rising, or `ai.transfer_reconciled`/`ai.transfer_failed` audit events
   accumulate (deletion failure is the `ai.transfer_failed` event with
   `error_code = "provider_reference_deletion_failed"`). The durable
   `ai_attachment_references` rows are the source of truth: a failed deletion
   is a row still in `live` state with `deletion_attempted_at` set and that
   `error_code`; rows reach `deleted` only after a successful deletion
   (reference states are `live`/`expired`/`deleted` — there is no separate
   `deletion_failed` state).
2. **Assess**: is the provider API reachable? A provider outage naturally
   stalls deletion; the sweep's bounded backoff retries automatically. A
   sustained backlog with the provider healthy suggests a systematic cause
   (revoked credential, changed file id, provider-side retention).
3. **Act**: for a provider outage, wait for recovery — the sweep resumes on
   schedule with bounded backoff. For a revoked/broken credential, restore the
   provider secret and redeploy. For provider-side retention changes, follow
   the provider's manual deletion procedure for the affected file ids (from
   the durable rows) once, then verify the sweep returns the backlog to zero.
4. **Resolve**: `ai_transfer_cleanup_backlog` returns to 0 and the reference
   rows reach `deleted`; the audit trail records each deletion attempt.

#### Disabling a compromised transfer mode

1. **Contain**: remove the mode from `AI_ENABLED_TRANSFER_MODES` (deployment
   level) and from the organisation's `allowed_transfer_modes` (platform
   AI-settings API, audited). The mode is then ineligible before any external
   transfer: dispatch fails closed with a safe configuration/policy error and
   no provider call or upload occurs. Production fails fast on restart if an
   enabled mode loses its supporting configuration.
2. **Clean up**: provider-hosted files from the disabled mode are drained by
   the reconciliation sweep (see above); managed URLs expire through their
   short TTL and never own/delete the retained feature source; Vertex staging
   objects are cleaned by the deployer-configured `age = 1` lifecycle rule.
3. **Verify**: `ai_transfer_selections_total{mode="<disabled mode>"}` stops
   incrementing, the backlog drains, and the security/contract suites pass.

### AI configuration notes

- **Provider regions / inference geography**: OpenAI `AI_OPENAI_REGION`
  (`us`/`eu`, approved-account data-residency opt-ins, deriving the regional
  endpoint), Anthropic `AI_ANTHROPIC_INFERENCE_GEOGRAPHY` (`us` = US-only
  inference, Claude 4.6+ only), Azure region inherent in
  `AI_AZURE_OPENAI_ENDPOINT`, Vertex pinned by `AI_VERTEX_LOCATION`, DeepSeek
  documents no template-controlled pinning, local/fake providers inherit
  their operator-controlled location. These settings distinguish the
  **configured endpoint location** from any contractual data-residency
  guarantee — treat them as routing configuration, not residency proof.
  Fallback never changes a provider's region implicitly.
- **Vertex identity (ADR-0018)**: Gemini goes through the Vertex AI API only.
  `AI_VERTEX_PROJECT` + `AI_VERTEX_LOCATION` are required when the adapter is
  enabled; credentials come from Application Default Credentials (workload
  identity on Google Cloud) or a service-account key mounted through the
  deployment secret mechanism (`AI_VERTEX_CREDENTIALS_PATH`). There is no
  Gemini Developer API key setting anywhere in the template.
- **Attachment limits and lifecycle (v0.8)**: the v0.7 inline path keeps one
  conservative template limit — 5 MB per attachment, 10 MB combined, validated
  before dispatch — and models lacking the `documents` capability (e.g.
  DeepSeek) reject attachments before any provider call. v0.8 adds the
  policy-driven transfer modes for large files: inline is eligible only at or
  below 5,000,000 aggregate raw bytes; above that threshold exactly one PDF up
  to 50,000,000 bytes (or the provider/model ceiling, whichever is lower) uses
  `provider_upload` (OpenAI/Anthropic transient sources), `managed_signed_url`
  (retained private S3 sources; just-in-time signed URL, 900 s default / 1,800 s
  max TTL, never returned/persisted/audited/logged) or `storage_reference`
  (Vertex private GCS staging with the deployer-configured `age = 1` lifecycle
  backstop). Bytes exist only in bounded worker memory/staging for one provider
  call, are never persisted (records store references + digests), and are never
  placed on the job broker. Azure OpenAI, DeepSeek and local providers declare
  no non-inline mode and reject large files before any transfer. See
  `backend/app/ai/README.md` and README → Large AI attachments for the full
  contract.
- **Synchronous ask bound (plan P9)**: `/api/v1/ai/ask` is synchronous only, so
  it is bounded by `AI_ASK_MAX_SYNCHRONOUS_BYTES` (default 5,000,000 — the
  inline threshold). The bound can be lowered but never raised above
  `AI_INLINE_AGGREGATE_THRESHOLD_BYTES`, so a large-file transfer can never run
  inside an HTTP request. The bound is enforced in the common `AIService.execute`
  boundary *after* the organisation AI-enabled policy and the P6 durable source
  authority and *before* any attachment bytes are read, so a disabled
  organisation or an unauthorised/quarantined/expired/foreign source keeps its
  own error and never triggers a pre-authorisation object read. A source above
  the bound is rejected with `ai_ask_attachment_too_large`. The v0.8 non-inline
  transfer modes remain implemented and tested at the `AIService` layer, but are
  not reachable through this endpoint; this release exposes **no durable
  asynchronous ask operation**, so the supported remedy is a smaller document.
  Durable `document.classify` (`sync=false`) remains the supported long-running
  AI route.
- **Vertex large-file staging (v0.8)**: `AI_VERTEX_TEMP_GCS_BUCKET` must be a
  user-provisioned private, single-region bucket in the configured
  `AI_VERTEX_LOCATION`, owned by `AI_VERTEX_PROJECT`. The workload
  identity/service account needs more than an object role — the adapter
  verifies project ownership, bucket metadata and the bucket IAM policy before
  any upload — so grant `roles/storage.objectAdmin` (or `objectUser`) plus
  `roles/viewer`, or a custom role with `resourcemanager.projects.get`,
  `storage.buckets.get`, `storage.buckets.getIamPolicy`,
  `storage.objects.create`, `storage.objects.get`, `storage.objects.delete`.
  The application never creates, configures or manages the bucket and runs no
  scheduled GCS cleanup or reconciliation; the only object it deletes is the
  exact AI-owned staging object it uploaded, best-effort at the terminal
  request outcome and during the provider-file sweep. The deployer configures
  a console Object Lifecycle rule (`age = 1` day → Delete) as the cleanup
  backstop. Lifecycle execution is asynchronous — it is a backstop, not an
  exact 24-hour deletion guarantee — and soft-delete/versioning/conflicting
  retention holds on the bucket extend object retention/recoverability rather
  than guaranteeing that a live object persists. Deployers seeking
  Files-API-like ephemeral storage must disable soft delete, versioning and
  conflicting holds, or explicitly accept their longer retention semantics.
- **Provider retention and deletion (v0.8)**: OpenAI uploads use
  `purpose=user_data` with the shortest supported `expires_after` and
  best-effort terminal deletion; Anthropic uses the pinned beta Files API with
  delete-only retention, so provider-file reconciliation is mandatory (the
  scheduled sweep is the only job that touches provider-hosted files). Managed
  URLs expire through their TTL and never own/delete the retained feature
  source. AI cleanup never deletes feature-owned source objects.
- **Local-provider network controls**: the local OpenAI-compatible adapter
  targets loopback/private hosts only; production fails fast on publicly
  reachable endpoints, and the endpoint must never be exposed to browsers.
- **Non-production contract-test credentials**: `make test-ai-contracts`
  runs the opt-in `ai_contracts`-marked adapter tests; each skips cleanly
  when its dedicated non-production credentials are absent. Credentials use
  a dedicated `AI_CONTRACTS_*` namespace (never the operational `AI_*`
  settings): `AI_CONTRACTS_OPENAI_API_KEY`, `AI_CONTRACTS_ANTHROPIC_API_KEY`,
  `AI_CONTRACTS_DEEPSEEK_API_KEY`, `AI_CONTRACTS_AZURE_ENDPOINT` /
  `AI_CONTRACTS_AZURE_API_KEY`, `AI_CONTRACTS_VERTEX_PROJECT` /
  `AI_CONTRACTS_VERTEX_LOCATION` (+ optional
  `AI_CONTRACTS_VERTEX_CREDENTIALS_PATH`), and `AI_CONTRACTS_LOCAL_BASE_URL`.
  When configured, use only dedicated non-production accounts/projects
  (Vertex: a dedicated project/location and Vertex credentials, never a
  Gemini API key), and keep those credentials out of `.env.example`, logs
  and CI logs. A protected-CI job may run `make test-ai-contracts` only when
  those secrets are deliberately configured as CI secrets.

## Log rotation and retention

Compose rotates each container's JSON logs (`json-file` driver; API/worker
20 MB × 5 files, Caddy/Redis 10 MB × 3 files). Host-level rotation for
`/var/lib/docker/containers` and the syslog/audit logs follows the distro
default (logrotate); set retention so the disk alert threshold is never
crossed by logs alone. Logs are streamed to stdout in JSON
(structlog/Caddy), so a log shipper (Vector, Fluent Bit, Loki) can tail the
containers without code changes.

## Rollback

Releases are immutable and the frontend is served from
`releases/current/frontend` (an atomic symlink), so rollback is:

```bash
cd /opt/app-template
RELEASE_DIR=/opt/app-template
# Second-newest by mtime; for a host with clock skew or re-staged
# same-SHA releases, pick the release directory name you actually want.
PREV=$(ls -1t "$RELEASE_DIR"/releases | sed -n 2p)
ln -sfn "$PREV" "$RELEASE_DIR/releases/current"
docker compose -f compose.hybrid-vps.yml --env-file .env.production up -d --remove-orphans
```

The backend image is pinned to the release SHA in the compose environment
(`BACKEND_IMAGE`), so to roll the API/worker/coordinator back to `$PREV`, first
stop the coordinator, then re-run the deploy workflow for that SHA or pull and
re-tag the previous image before recreating the services. Migrations are
forward-only by policy; see `docs/backup-and-recovery.md` for the database
restore path that a schema rollback would require.

## Tuning reference

| Parameter                   | Default              | Where                                        |
| --------------------------- | -------------------- | -------------------------------------------- |
| Worker threads per process  | 8                    | `WORKER_CONCURRENCY` in `.env.production`    |
| API replicas                | 1                    | `docker compose up -d --scale api=N`         |
| Worker processes            | 1                    | `docker compose up -d --scale worker=N`      |
| Coordinator replicas        | 1                    | `docker compose up -d --scale coordinator=N` |
| API memory/CPU limits       | 512 MB / 1 CPU       | `compose.hybrid-vps.yml` `deploy.resources`  |
| Worker memory/CPU limits    | 1 GB / 2 CPU         | `compose.hybrid-vps.yml` `deploy.resources`  |
| Broker Redis cap / eviction | 200 MB / noeviction  | `compose.hybrid-vps.yml`                     |
| Rate Redis cap / eviction   | 64 MB / allkeys-lru  | `compose.hybrid-vps.yml`                     |
| Edge API rate limit         | 600/min per IP       | `deploy/caddy/Caddyfile`                     |
| App rate limit (/api/v1)    | 300/min per key      | `app/core/rate_limit.py`                     |
| API graceful shutdown       | 30 s                 | `compose.hybrid-vps.yml` `stop_grace_period` |
| Worker graceful shutdown    | 120 s                | `compose.hybrid-vps.yml` `stop_grace_period` |
