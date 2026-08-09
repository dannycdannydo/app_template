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

| Service | Image / command | Scaling unit | Notes |
| --- | --- | --- | --- |
| `caddy` | custom image from `deploy/caddy/Dockerfile` (Caddy v2.11.4 + caddy-ratelimit v0.1.0) | 1 instance | Edge TLS, static frontend, `/api` proxy, security headers, edge rate limits |
| `api` | backend image, `uvicorn app.main:app` | replicas via `--scale api=N` | Health-checked on `/ready`; Caddy load-balances across replicas |
| `worker` | backend image, `dramatiq app.workers --threads N` | `WORKER_CONCURRENCY` per process, `--scale worker=N` for more processes | Durable job pipeline (ADR-0004) |
| `redis` | `redis:7-alpine`, password + AOF persistence | 1 instance | Dramatiq broker + API rate-limit store; never published |

Initial defaults: 1 API replica, 1 worker process at `WORKER_CONCURRENCY=8`,
1 Caddy, 1 Redis. Compose limits each service (CPU/memory) and rotates JSON
logs (`json-file` driver, `max-size`/`max-file` per service).

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
`jobs_succeeded_total`, `jobs_failed_total` (label `job_type`). Recommended
alerts:

- `http_requests_total` error rate (`status_code` 5xx) above 1% over 10 min.
- `http_requests_total` per `path` traffic collapse (silent API).
- `jobs_failed_total` growth; alert on failed growth.
- `http_request_duration_seconds` `p95` above the SLO threshold (default 1 s).

### Alerts to configure

| Alert | Signal | Severity |
| --- | --- | --- |
| Readiness / API failure | `/ready` non-200 from the uptime monitor or scraper | critical |
| Worker / job failures | `jobs_failed_total` rising; delivery rows `failed` | critical |
| Disk pressure | host disk or Caddy/Redis log volumes ≥ 80% | warning (90% critical) |
| Certificate expiry | Let's Encrypt renewal failures in Caddy logs; cert expiry within 14 days | critical |
| Backup failure | failed backup job / missing backup marker (docs/backup-and-recovery.md) | critical |
| Redis unavailable | API `rate_limiter_unavailable` errors; `redis-cli ping` failure | critical |

## Redis

Redis in this profile is a private service: no published port, password
authentication, AOF persistence, a 200 MB memory cap and the `allkeys-lru`
eviction policy (all set in `compose.hybrid-vps.yml`; `REDIS_PASSWORD` comes
from `.env.production` and must match `REDIS_URL`).

### Graceful shutdown

`docker compose stop redis` runs SIGTERM and Redis flushes the AOF before
exit (default `stop_grace_period`); `docker compose restart redis` is safe.
The API fails closed when Redis is unavailable (`rate_limiter_unavailable`,
503) rather than silently dropping the abuse control — a deliberate choice.

### Consequences of Redis loss

- **Broker**: queued Dramatiq messages are lost; jobs already delivered to a
  worker continue. Durable job *records* (the `jobs` table) survive in
  PostgreSQL, so job state is recoverable, but messages in the queue are not.
- **Rate limiting**: API traffic fails closed with 503 until Redis returns
  (the rate limiter is the only Redis consumer at the edge; `REDIS_URL`
  connectivity is the dependency).
- **Persistence**: the AOF (`appendonly yes`, `--save 60 1000`) survives
  container restarts and host reboots; a wiped volume loses the queue and
  counters, not application data. Keep `redis_data` in the off-site backup
  picture only for continuity, not as a source of truth.

## Trusted proxy and client-IP handling

Caddy is the single TLS-terminating edge. Requests reach the API with the
real client IP in `X-Forwarded-For` and the Caddy container address as the
TCP peer:

- **Edge rate limits** (`deploy/caddy/Caddyfile`) key on `{remote_host}` —
  the real client IP — so the edge control is per-client-IP.
- **Application rate limit** (`app/core/rate_limit.py`) keys on
  `request.client.host`, which behind the edge is the Caddy container
  address, so it acts as a site-wide bucket in this topology. The edge
  control is therefore the effective per-IP limit; the application limiter is
  the authoritative control in direct-connect deployments. Applications that
  need true per-client-IP app-level limits behind the edge must read
  `X-Forwarded-For` while trusting only the edge (never a client-supplied
  header), and must not weaken `TRUSTED_HOSTS`.

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
(`BACKEND_IMAGE`), so to roll the API/worker back to `$PREV` re-run the deploy
workflow for that SHA, or pull and re-tag the previous image. Migrations are
forward-only by policy; see `docs/backup-and-recovery.md` for the database
restore path that a schema rollback would require.

## Tuning reference

| Parameter | Default | Where |
| --- | --- | --- |
| Worker threads per process | 8 | `WORKER_CONCURRENCY` in `.env.production` |
| API replicas | 1 | `docker compose up -d --scale api=N` |
| Worker processes | 1 | `docker compose up -d --scale worker=N` |
| API memory/CPU limits | 512 MB / 1 CPU | `compose.hybrid-vps.yml` `deploy.resources` |
| Worker memory/CPU limits | 1 GB / 2 CPU | `compose.hybrid-vps.yml` `deploy.resources` |
| Redis memory cap / eviction | 200 MB / allkeys-lru | `compose.hybrid-vps.yml` |
| Edge API rate limit | 600/min per IP | `deploy/caddy/Caddyfile` |
| App rate limit (/api/v1) | 300/min per key | `app/core/rate_limit.py` |
| API graceful shutdown | 30 s | `compose.hybrid-vps.yml` `stop_grace_period` |
| Worker graceful shutdown | 120 s | `compose.hybrid-vps.yml` `stop_grace_period` |
