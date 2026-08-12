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
| `worker` | backend image, `dramatiq app.workers --processes 1 --threads N` | `WORKER_CONCURRENCY` per process, `--scale worker=N` for more processes | Durable job pipeline (ADR-0004) |
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

The AI layer adds its own families (`ai_requests_total`,
`ai_request_duration_seconds`, `ai_tokens_total`, `ai_cost_total`,
`ai_validation_failures_total`, `ai_retries_total`, `ai_fallbacks_total`,
`ai_budget_denials_total`) and alerting/runbook guidance — see
"AI observability and runbooks (v0.7)" below.

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

## AI observability and runbooks (v0.7)

The AI layer (v0.7 Scope §6.7, ADR-0017) emits its own metric families on
`GET /metrics`, binds `ai_request_id` to every AI log line, and keeps Sentry
free of prompts, provider responses and document content (the shared
`before_send` redaction applies; AI errors are handled, so they never reach
Sentry as unhandled exceptions). The durable `ai_requests` / `ai_outputs`
rows are the per-request source of truth; the counters below are the
aggregate signal, labelled only with low-cardinality registry ids
(task/provider/model) — organisation ids, request ids and content never
become labels.

### AI metrics families

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `ai_requests_total` | counter | `task`, `provider`, `model`, `status` (`succeeded`/`failed`) | provider executions by terminal outcome, one sample per settled attempt |
| `ai_request_duration_seconds` | histogram | `task`, `provider`, `model` | provider execution latency |
| `ai_tokens_total` | counter | `task`, `provider`, `model`, `direction` (`input`/`output`) | tokens consumed |
| `ai_cost_total` | counter | `task`, `provider`, `model` | spend in USD, priced with the registry's reviewed rates (the registry's single pricing currency) |
| `ai_validation_failures_total` | counter | `task`, `provider`, `model` | structured-output validation failures (each followed by one bounded repair or a task retry) |
| `ai_retries_total` | counter | `task`, `provider`, `model` | bounded retry dispatches after the first (including repair dispatches) |
| `ai_fallbacks_total` | counter | `task`, `provider`, `model` | reviewed provider/model fallbacks under the task's fallback policy |
| `ai_budget_denials_total` | counter | `task` | monthly organisation budget denials before dispatch |
| `dramatiq_queue_depth` | gauge | `queue` | undelivered messages waiting in a Dramatiq queue (`LLEN dramatiq:<queue>` on Redis); refreshed by the API process every 30 s, so the promised backlog alert is queryable from `GET /metrics` |

### AI dashboard contract

The template defines its dashboard here rather than shipping a Grafana JSON
file so any Prometheus-compatible frontend (Grafana, managed dashboards) can
implement it. One panel per row; every query is a PromQL expression over the
families above, with a corresponding alert rule (aggregate table below).

| Panel | PromQL query | Type | Alert rule |
| --- | --- | --- | --- |
| Provider success rate | `1 - sum(rate(ai_requests_total{status="failed"}[10m])) / sum(rate(ai_requests_total[10m]))` | gauge (0-1) | `< 0.95` (provider outage, critical) |
| Provider latency p95 | `histogram_quantile(0.95, sum(rate(ai_request_duration_seconds_bucket[10m])) by (le, task))` | gauge (s) | `> 30` (warning; per-task override) |
| Token throughput | `sum(rate(ai_tokens_total[10m])) by (direction)` | gauge (tokens/s) | trend only |
| Spend rate | `sum(rate(ai_cost_total[10m]))` | gauge (USD/s) | daily-normalised `ai_cost_total` rate above budget threshold (warning) |
| Validation failures | `sum(rate(ai_validation_failures_total[10m]))` | gauge (events/s) | rising, or retry/repair ratio `> 0.2` of requests (warning) |
| Retry/fallback ratio | `sum(rate(ai_retries_total[10m])) / clamp_min(sum(rate(ai_requests_total[10m])), 1e-9)` | gauge (ratio) | `> 0.2` (warning) |
| Budget denials | `sum(rate(ai_budget_denials_total[10m]))` | gauge (events/s) | `> 0` (warning; info if deliberate) |
| AI queue backlog | `dramatiq_queue_depth{queue="ai"}` | gauge (messages) | `> 10` for `> 5 min` (warning) |

Queue-depth source: the API process reads the broker's queue lengths through
`RedisBroker.get_queue_message_counts` (Dramatiq stores each queue as a Redis
list `dramatiq:<queue>`; the `ai` queue carries the `ai.execute` and
`ai.retention` jobs). The refresh loop runs inside the API process every 30 s,
so no separate exporter is required — the same `/metrics` endpoint serves the
gauge. A Redis outage leaves the gauge stale (logged once) rather than failing
the scrape.

### AI alerts to configure

| Alert | Signal | Severity |
| --- | --- | --- |
| Provider outage | `ai_requests_total{status="failed"}` error rate above threshold (e.g. 5% over 10 min, or a step change in `provider_unavailable`/`provider_timeout` failures) | critical |
| Provider latency | `ai_request_duration_seconds` `p95` above the SLO threshold (default 30 s; raise per task/provider) | warning |
| Validation degradation | `ai_validation_failures_total` rising or repair/retry ratio above threshold (e.g. > 20% of requests) | warning |
| Spend spike | `ai_cost_total` rate above the daily budget-normalised threshold | warning |
| Budget denials | `ai_budget_denials_total` growth (users hitting the monthly cap) | warning (info if deliberate) |
| Queue backlog | `dramatiq_queue_depth{queue="ai"}` above threshold (e.g. 10) for > 5 min (AI jobs are durable; see "Consequences of Redis loss") | warning |

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

The retention job (`ai.retention`, `ai` queue) enforces the organisation's
`retention_policy_days`: it deletes expired `ai_outputs` rows (and the
organisation-scoped AI scratch objects they reference), sweeps orphaned
scratch objects older than the policy, reconciles stale `running` requests to
`failed` (keeping their reserved cost), and writes one `ai.retention_deleted`
audit event per purge. Keep-flow objects under
`organisations/{org}/documents/…` are never touched.

1. **Confirm**: `ai.retention.*` worker logs report the sweep summary;
   `ai.retention_deleted` audit events record the purge.
2. **Act**: retention is a privacy control — only an organisation with a
   configured policy is swept. To change retention, update the platform
   AI-settings API (audited). The sweep pages the scratch namespace
   (`start_after` paging), so any namespace size is fully swept.
3. **Verify**: spot-check that expired rows and scratch objects are gone and
   keep-flow objects remain; the audit event per purge is the evidence trail.

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
- **Attachment limits and lifecycle**: one conservative template limit — 5 MB
  per attachment, 10 MB combined, validated before dispatch; models lacking
  the `documents` capability (e.g. DeepSeek) reject attachments before any
  provider call. Bytes exist only in worker memory for one provider call,
  are never persisted (records store references + digests), and are never
  placed on the job broker. Large-file/provider-reference support is
  explicitly deferred to v0.8 (`plans/AI_LARGE_ATTACHMENTS_V0_8_PLAN.md`).
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
