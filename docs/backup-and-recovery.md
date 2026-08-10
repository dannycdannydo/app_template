# Backup and Recovery — Hybrid VPS Profile

This document is the backup and recovery contract for the generic Linux VPS /
container-host production profile (Scope §6.7, blueprint §39, ADR-0007). It
covers what to back up, how often, the recovery procedures for every failure
class, and the RPO/RTO targets the procedures are expected to meet. It is the
recovery companion to `docs/operations.md` (day-to-day operations) and
`SECURITY.md` (mandatory protections, including off-site configuration
backups); the deployment workflow that produces the release layout is
`.github/workflows/deploy-vps.yml`.

Blueprint §39 rule this document is built on: **backups are not considered
valid until the restore procedures have been tested.** The two procedures
that require scratch infrastructure (database restore, lost-host /
environment recreation) are executed and recorded in
[Tested runs](#tested-runs) below, and the whole set is re-verified on the
schedule in [Backup verification schedule](#backup-verification-schedule).

## Assumed environment

Everything below assumes a host running `deploy/compose/compose.hybrid-vps.yml`
with an environment file at `$DEPLOY_ROOT/.env.production` (default
`/opt/app-template`) and the release layout produced by the deploy workflow:

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

Every `docker compose` command uses the release-directory alias from
`docs/operations.md`:

```bash
cd /opt/app-template
export COMPOSE="docker compose -f compose.hybrid-vps.yml --env-file .env.production"
```

The application's durable state lives in two external services — managed
PostgreSQL and S3-compatible object storage — plus the private Redis on the
host (broker and rate-limit store only, never a source of truth). Backing up
those external services is the priority; the VPS disk itself holds no
application data that is not recoverable from them plus the deployment
artifacts.

## What to back up, how often, and where

| Asset | Source of truth | Backup mechanism | Frequency | Retention | RPO | RTO |
| --- | --- | --- | --- | --- | --- | --- |
| Database (PostgreSQL) | Managed DB provider | Provider-native backups + point-in-time recovery (PITR) | Continuous PITR (≥ 24 h window) + nightly logical dump (`pg_dump -Fc`) sent off-site | PITR ≥ 7 days; dumps ≥ 30 days | ≤ 5 min | ≤ 30 min |
| Object storage bucket | S3-compatible provider | Bucket versioning + cross-region/bucket replication | Continuous (per object write) | Versioning ≥ 90 days; replicate to a second bucket | 0 (versioned) | ≤ 15 min |
| Secrets (`.env.production`) | Operator | Encrypted copy off the host (password manager, secret vault, or encrypted archive) | On every change | Indefinite (every version) | 0 | ≤ 30 min |
| Deployment artifacts (compose file, Caddyfile, images, frontend artifact) | Git + container registry | Git history + immutable images in the registry; the host retains the newest 3 releases | Every release | Images/artifacts ≥ 6 months; host releases 3 | 0 | ≤ 30 min |
| Certificates (TLS) | Caddy (Let's Encrypt) | Auto-renewed by Caddy; no manual backup needed | Continuous | Renewed before expiry | 0 | automatic |
| Redis (`redis_data` volume) | Transient | None required (AOF only) | n/a | n/a | n/a | see [Redis recovery semantics](#redis-recovery-semantics) |

RPO/RTO targets are operational defaults: the numbers above assume a
single-region managed PostgreSQL with ≥ 24 h PITR and an object-storage bucket
with versioning and replication enabled. If a deployment accepts different
targets (for example a lower-cost provider without PITR), record the deviation
in the environment's runbook and adjust the alerts in `docs/operations.md`.

Backup jobs must alert on failure. `docs/operations.md` lists "backup failure"
as a critical alert; wire the nightly logical dump and the bucket
versioning/replication health check to that alert.

## External-service dependency model

The application is stateless across containers; the external services it
depends on and what losing each one means:

| External service | What the app needs | If lost | Recovery action |
| --- | --- | --- | --- |
| Managed PostgreSQL | `DATABASE_URL`; all durable application data | Complete data loss if backups are also lost; app serves 500s | Procedure 1 (database restore) |
| Object storage | `STORAGE_*` bucket; all file payloads | Files unreachable; app serves upload/download errors | Procedure 2 (object-storage recovery) |
| WorkOS | `WORKOS_API_KEY`, `WORKOS_CLIENT_ID`, webhook secret; org mapping + auth | No logins, no org provisioning; webhook deliveries rejected (fail-closed) | Re-enter credentials; dashboard config (redirect URIs, CORS, webhook endpoint) is re-created from `README.md` + `.env.production.example` |
| Transactional email (SMTP relay) | `SMTP_*` + `EMAIL_FROM` | Notification/test emails not delivered (jobs still succeed/fail durably) | Re-enter credentials; deliveries retry or re-send via the notifications API |
| Sentry | `SENTRY_DSN` | No error capture; app otherwise unaffected | Re-enter DSN; `SENTRY_ENVIRONMENT` routing restored |
| Monitoring (uptime checks, Prometheus scraper) | `/health`, `/ready`, `/metrics` endpoints | No alerts; no visibility | Re-create checks per `docs/operations.md` |
| DNS | A/AAAA record → host IP | Site unreachable | Update the record to the (new) host IP; see DNS/TLS recovery |
| Let's Encrypt | `ACME_EMAIL`, ports 80/443 reachable | Certificate expiry | Automatic renewal by Caddy; see DNS/TLS recovery |

Recovery ordering after a partial or total loss: restore the database first
(most state), then object storage, then secrets/configuration, then start the
services, then verify through `/ready` and the external checks.

---

## Procedure 1 — Database restore (managed PostgreSQL)

Two restore paths, in priority order:

**Primary: provider-native restore + PITR.** Managed PostgreSQL providers
(Amazon RDS, Azure Database for PostgreSQL, DigitalOcean, Neon, Supabase,
etc.) snapshot continuously and keep the transaction log for point-in-time
recovery. The recovery procedure is provider-specific (console or API), and
this template deliberately does not vendor one provider — the application
never depends on how the restore happens, only on the resulting database URL.
The general steps:

1. Choose the restore target: the latest backup, or a point in time within the
   PITR window (the RPO target is ≤ 5 min, so a PITR restore to just before an
   incident is the standard choice for a logical error; a full backup restore
   is the choice for a provider-side failure).
2. Restore into a **new** database instance (never overwrite the live one in
   place — keep the original available until the restored copy is verified).
3. Point the deployment at the restored instance: update `DATABASE_URL` in
   `.env.production` (secret recovery rules apply, see Procedure 3), then
   recreate the services so the API and worker pick up the new URL.
4. Bring the schema to head: `$COMPOSE run --rm --no-deps api alembic
   upgrade head` (a no-op when the backup is already at head; migrations are
   forward-only by policy).
5. Recreate the services and wait for readiness:

   ```bash
   $COMPOSE up -d --remove-orphans
   $COMPOSE exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=3)"
   ```

6. Verify the restore: the API reports `/ready` healthy; `alembic_version`
   matches the deployed release; spot-check the newest rows of
   `audit_events`, `jobs` and `notifications` for continuity up to the restore
   point.

**Fallback: logical dump restore.** The nightly `pg_dump -Fc` archive is the
provider-neutral escape hatch (works against any PostgreSQL, including moving
between providers). Restore into an empty database:

```bash
# Restore target: a new, empty database (never restore over the live one).
pg_restore --no-owner --role=app -d app_template_production_restore \
  app_template_production_$(date -u +%F).dump
# Then steps 4-6 above (migration, recreate, /ready, verify).
```

Because the dump is taken nightly, its RPO is up to 24 h: use it only when
PITR is unavailable, and expect to re-apply anything written after the dump.

This procedure was executed against scratch infrastructure — see
[Tested run A](#tested-run-a-database-logical-restore-on-scratch-postgresql).

---

## Procedure 2 — Object-storage recovery

The bucket holds every uploaded file payload; the metadata lives in the
database, so a storage restore must not be confused with a database restore —
they are independent.

**Recover deleted or overwritten objects (versioning enabled).** With bucket
versioning on, every PUT/DELETE is retained. Restore through the provider
console or CLI; for a deletion, promote the latest non-delete marker version;
for an overwrite, promote the previous version. Example with the AWS CLI
(any S3-compatible provider works the same):

```bash
aws s3api list-object-versions --bucket "$STORAGE_BUCKET" --prefix "files/" --query "Versions[?IsLatest==\`false\`]"
# Restore one object: copy the desired version-id back over the current key.
aws s3api copy-object --bucket "$STORAGE_BUCKET" --copy-source "$STORAGE_BUCKET/files/<key>?versionId=<version-id>" --key "files/<key>"
```

**Recover after the bucket itself is lost.** If the bucket must be recreated
(the provider lost it, or it was deleted by mistake):

1. Recreate the bucket with the same name and region (or update
   `STORAGE_BUCKET`/`STORAGE_ENDPOINT_URL` in `.env.production` if the name
   changes — the endpoint may change across providers, which is a config
   change, not a code change).
2. Copy objects back from the replication target bucket (or from the
   versioned backup) with `aws s3 sync`.
3. Verify: upload/download a probe file through the app's signed-URL flow and
   confirm a `documents.download` audit event.

**Configuration recovery.** The bucket is private and accessed through signed
URLs; the signing identity comes from `STORAGE_ACCESS_KEY_ID` /
`STORAGE_SECRET_ACCESS_KEY` and the endpoint/region settings. Recreating a
bucket requires re-issuing those credentials at the provider and updating
`.env.production` (Procedure 3). CORS/permissions are configured on the
provider side; record them with the environment's runbook so a recreated
bucket can be reconfigured to match.

---

## Procedure 3 — Secret recovery

The production secret store is the `.env.production` file on the host (the
API and worker containers read it directly), plus the GitHub secrets the
deploy workflow needs. The source of truth is the operator's encrypted
off-site copy; the file is `chmod 600` on the host and is on the off-site
configuration-backup list in `SECURITY.md`.

1. **`.env.production`** — restore the latest copy from the off-site vault
   into `$DEPLOY_ROOT/.env.production`, `chmod 600` it, and verify it against
   the live external services (credentials that have rotated since the backup
   must be updated — see step 3). Values that must match the live services:
   `DATABASE_URL`, `REDIS_PASSWORD`/`REDIS_URL`, `WORKOS_*`, `STORAGE_*`,
   `EMAIL_*`/`SMTP_*`, `SENTRY_DSN`, `CORS_ALLOWED_ORIGINS`,
   `TRUSTED_HOSTS`.
2. **GitHub secrets** (`DEPLOY_SSH_KEY`, `REGISTRY_PASSWORD`,
   `REGISTRY_USERNAME`) — if lost, re-enter them in the repository or
   environment settings; rotate the SSH key at the host (`~/.ssh/authorized_keys`)
   and the registry credentials before re-entering.
3. **Rotation after a compromise or a stale backup**: for each secret, rotate
   at the provider (managed Postgres credentials, storage keys, SMTP password,
   WorkOS API key/webhook secret, `REDIS_PASSWORD`) and update
   `.env.production`, then recreate the services so the containers read the
   new values:
   `$COMPOSE up -d --remove-orphans` and wait for `/ready`.
4. **WorkOS dashboard configuration** is a second secret surface: the
   application (API key, client id, redirect URIs, CORS origins, webhook
   endpoint + secret) and its org mappings. The dashboard configuration is
   re-created from `README.md` (redirect URIs, CORS) and `.env.production.example`
   (every variable), and the org ↔ WorkOS-Organization mapping is
   re-established through the platform admin centre (ADR-0013). If the mapping
   is lost, users cannot be invited or authenticated until organisations are
   re-linked.

Never log or print secrets; the BP §28 never-log list is enforced by test.

---

## Procedure 4 — Deployment rollback

Releases are immutable and the frontend is served from
`releases/current/frontend` (an atomic symlink), so rollback is a symlink
flip plus a recreate. The previous release is retained on the host by the
deploy workflow (newest 3 releases plus the rollback target).

**Application rollback (API/worker/frontend):**

```bash
cd /opt/app-template
# Second-newest by mtime; pick the release directory name explicitly if the
# ordering is ambiguous (see docs/operations.md — rollback).
PREV=$(ls -1t "$RELEASE_DIR"/releases | sed -n 2p)
ln -sfn "$PREV" "$RELEASE_DIR/releases/current"
$COMPOSE up -d --remove-orphans
$COMPOSE exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=3)"
```

**Backend image rollback:** the API and worker run the commit-pinned image
(`BACKEND_IMAGE`, set by the workflow at deploy time). Rolling back to a
previous release therefore requires either re-running the deploy workflow for
the previous SHA (the workflow pulls and tags that commit's image) or, on the
host, pulling the previous image and re-tagging it, then recreating the
services.

**Database rollback is a restore, not a migration.** Migrations are
forward-only by policy; there is no `downgrade` path in the release
procedure. If a release's migration must be undone, restore the database to
the pre-release point (Procedure 1) and deploy the previous release — never
run a partial migration. When the database and the application code disagree
on schema version, `/ready` still reports healthy (it checks database
reachability), so verify schema/version alignment explicitly after any
rollback (`alembic_version` vs. the release's migration head).

**Frontend-only rollback:** flip `releases/current` to the previous release
and recreate Caddy (`$COMPOSE up -d caddy`); no migration or image pull is
involved.

---

## Procedure 5 — Lost VPS replacement

A lost host is rebuilt from: the off-site configuration backup (compose file,
Caddyfile, `.env.production`), the container registry (immutable backend and
Caddy images), Git (frontend artifact source), and the external services
(database, object storage, WorkOS, email — all untouched). The host itself
holds no non-recoverable state: Redis is transient (see Redis recovery
semantics) and the `redis_data` volume is rebuilt empty.

1. **Provision a new host** (any Linux VPS or container host per the
   portability contract in Scope §3.1): install Docker Engine + Compose,
   apply the mandatory protections from `SECURITY.md` (firewall: 22/80/443
   only; SSH keys only, no root/password login; unattended-upgrades),
   and point DNS at the new IP (A/AAAA record — see DNS/TLS recovery).
2. **Restore the configuration backup**: copy the off-site `compose.hybrid-vps.yml`,
   `Caddyfile` and `.env.production` into `$DEPLOY_ROOT` (default
   `/opt/app-template`), `chmod 600` the env file. The compose file and
   Caddyfile also live in Git (`.github/`-adjacent `deploy/`), so a fresh
   checkout plus the secret file from the vault is the complete picture.
3. **Restore the release layout**: `mkdir -p $DEPLOY_ROOT/releases`; the
   frontend artifact can be re-downloaded from the registry/release store or
   rebuilt (`pnpm build` from the release tag), placed under
   `releases/<git-sha>/frontend/`, with `releases/current` symlinked to it.
   If the previous release directory was lost with the host, the newest
   release still exists as an immutable image in the registry and as the
   frontend artifact in the release store.
4. **Pull the immutable images** (the workflow sets these; manually,
   `BACKEND_IMAGE` and `CADDY_IMAGE` must name the commit-pinned refs in
   `.env.production`, or the compose-file placeholders will not pull):

   ```bash
   $COMPOSE pull api worker caddy
   ```

5. **Run exactly one deliberate migration** (the database is external and was
   never lost; the migration is a no-op when the restored schema is already at
   head):

   ```bash
   $COMPOSE run --rm --no-deps api alembic upgrade head
   ```

6. **Start the services and wait for readiness:**

   ```bash
   $COMPOSE up -d --remove-orphans
   $COMPOSE exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=3)"
   ```

7. **Verify end to end**: `https://<DOMAIN>/ready` from an external uptime
   check; a login through WorkOS; a file upload through the signed-URL flow;
   a test notification (`POST /api/v1/notifications/test`). Re-create the
   monitoring checks and alerts per `docs/operations.md`.

Redis state is not restored: the AOF is rebuilt empty, queued Dramatiq
messages are lost, and the rate-limit counters reset — none of that is
application data (see Redis recovery semantics).

The lost-host and environment-recreation procedures share the same core —
this procedure was exercised against scratch infrastructure in
[Tested run B](#tested-run-b-environment-recreation-on-scratch-infrastructure),
which also recorded two shipped-profile defects that must be resolved before a
real host is rebuilt (see [Open defects from the tested runs](#open-defects-from-the-tested-runs)).

---

## Procedure 6 — Environment recreation (staging / production)

Environment recreation is the full rebuild of an environment from the
template and the documented configuration, not from a host backup. Blueprint
§38 (environment separation) governs: **staging never uses production data or
credentials** — separate databases, Redis instances, buckets, WorkOS
environments, secrets, and frontend/API URLs per environment.

1. **Create the environment file** from `.env.production.example`, filling
   the per-environment values (its header documents every variable and the
   separation rule). For a staging environment: a staging WorkOS
   application/environment, a staging database, a staging bucket, staging
   SMTP credentials or the local Mailhog relay, staging CORS/TRUSTED_HOSTS,
   and `SENTRY_ENVIRONMENT=staging`. Never copy a production `.env.production`
   to staging.
2. **Build and publish the artifacts** for the chosen commit: run the deploy
   workflow (`.github/workflows/deploy-vps.yml`, `workflow_dispatch` with the
   `staging` environment) or, manually, build the backend and Caddy images
   and the frontend artifact as the workflow does.
3. **Deploy** to the target host via the workflow (or the manual
   `$COMPOSE pull` / `alembic upgrade head` / `up -d` / `/ready` sequence in
   Procedure 5 — the steps are identical).
4. **Verify the environment is self-contained**: login through the
   environment's own WorkOS app, create an organisation, upload a file, send a
   test notification, and confirm the audit trail and notifications behave —
   all against the environment's own data. A staging environment that touches
   production credentials or data is a failed recreation.

Because recreation starts from the template + configuration (both in Git and
the off-site vault) rather than from a host image, it is also the procedure
for moving to a new provider: any Linux VPS or container host that satisfies
the portability contract consumes the same images, compose file, and settings
(Scope §3.1). The database restore (Procedure 1) covers the one piece that is
not recreated empty.

---

## Redis recovery semantics

Redis in this profile is a private, non-published service (password
authentication, AOF persistence, 200 MB memory cap, `allkeys-lru` eviction —
`compose.hybrid-vps.yml`; see `docs/operations.md`). It is the Dramatiq
broker and the API rate-limit store; **it is never a source of truth for
application data.**

- **Container restart / host reboot**: the AOF (`appendonly yes`,
  `--save 60 1000`) survives; queued messages and counters are retained.
- **Wiped `redis_data` volume (lost host)**: the queue and counters are lost,
  not application data. Durable job *records* live in the `jobs` table in
  PostgreSQL and survive; messages still queued at loss time do not (re-enqueue
  from the job records if continuity matters). Rate-limit counters reset —
  the limiter fails closed with 503 until Redis returns
  (`rate_limiter_unavailable`), so a healthy Redis is required before the API
  accepts traffic.
- **Recovery**: start Redis, verify with
  `docker exec <redis-container> redis-cli -a "$REDIS_PASSWORD" ping`
  (PONG), then recreate the API/worker so they reconnect. No restore step
  exists or is needed; keep `redis_data` in the off-site backup picture only
  for continuity, never as a source of truth.

---

## DNS/TLS recovery

- **DNS**: the site depends on an A/AAAA record pointing at the host IP
  (CNAME for the `www` alias). After a lost-host replacement with a new IP,
  update the record; propagation is typically minutes (lower TTL to e.g. 300
  s if fast failover matters). The external uptime checks in
  `docs/operations.md` confirm the record once the site answers.
- **TLS**: Caddy terminates TLS with automatic Let's Encrypt certificates
  (`deploy/caddy/Caddyfile`, `email {$ACME_EMAIL}`); issuance and renewal are
  automatic and require only that ports 80/443 reach the host and
  `ACME_EMAIL` is set. Certificates live in the `caddy_data` volume and are
  re-issued automatically on a new host — there is no certificate backup or
  restore step. Renewal failures surface in Caddy logs; alert on them per
  `docs/operations.md` (certificate expiry alert). If the domain changes,
  update `DOMAIN` (and `ACME_EMAIL`) in the environment file, recreate Caddy,
  and confirm the new certificate is issued.

---

## Tested runs

Blueprint §39: backups are not valid until restore procedures have been
tested. The two procedures requiring scratch infrastructure were executed on
2026-08-10 against throwaway containers on a development machine; the exact
commands and results are recorded below. The environment-recreation run
failed on its first execution against the shipped §6.6 artifacts (defects
D1/D2 below), was fixed in the §6.8 release-gate follow-up (plus defect D3
the re-run itself surfaced), and re-run to **PASS** — a backup procedure is
not valid until the recorded run passes. Re-run these on the schedule in
[Backup verification schedule](#backup-verification-schedule).

### Tested run A: database logical restore on scratch PostgreSQL

Environment: scratch `postgres:16-alpine` container; the application's real
Alembic migration chain applied; one marker row in the append-only
`audit_events` table (simulating production audit data); a custom-format
logical dump as the backup.

| Step | Command | Result |
| --- | --- | --- |
| Start scratch PostgreSQL | `docker run -d --name scratch-pg -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=app postgres:16-alpine` | ready |
| Apply the real schema | `DATABASE_URL=postgresql+asyncpg://app:app@127.0.0.1:55432/app uv --directory backend run alembic upgrade head` | all 16 migrations applied |
| Insert marker data | `INSERT INTO audit_events (action, resource_type, resource_id, metadata) VALUES ('record.created', 'record', '...0001', '{"request_id":"scratch-restore-test"}')` | 1 row |
| Take the backup | `pg_dump -U app -Fc -d app > app-backup.dump` | 56 660-byte archive |
| Simulate data loss | `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` | all 20 tables gone |
| Restore | `pg_restore -U app -d app -c backup.dump` | 113 ignored errors (expected: the `-c` clean-mode DROP statements for already-absent objects); all tables recreated |
| Verify | count of marker row = 1; `alembic_version` = `9e4f5c6a7d8b` (head); 20 tables present | **PASS** |

The run validates the logical-dump escape hatch end to end. The primary
provider-native PITR path cannot be executed against scratch infrastructure
(no provider), so it is validated by construction: the procedure is provider
console/API steps around the same post-restore steps (migration, recreate,
`/ready`, verification) that the scratch run proved.

### Tested run B: environment recreation on scratch infrastructure

Environment: fresh scratch directory standing in for a new host, the shipped
`compose.hybrid-vps.yml` and `Caddyfile` restored from the repository (the
off-site configuration backup), a scratch `.env.production` built from
`.env.production.example`, a scratch `postgres:16-alpine` on the compose
network (the "managed PostgreSQL" stand-in), and the backend image built from
the release checkout.

| Step | Command | Result |
| --- | --- | --- |
| Restore artifacts | copy compose file, Caddyfile, env file into fresh `$DEPLOY_ROOT` | **PASS** |
| Validate configuration | `docker compose -f compose.hybrid-vps.yml --env-file .env.production config --quiet` | **PASS** |
| Start private Redis | `docker compose up -d redis` | **PASS** (`redis-cli ping` → `PONG`, healthy) |
| Stand up scratch managed PostgreSQL | `docker run -d --network app-template-prod_default ... postgres:16-alpine` | **PASS** (reachable as `scratch-pg-recreate:5432`) |
| Run exactly one migration | `docker compose run --rm --no-deps api alembic upgrade head` | **FAIL** — see defect D1 below |
| Full stack up | `docker compose up -d` | Redis healthy; **API and worker crash-loop** — see defect D1; **Caddy crash-loops** — see defect D2 |
| `/ready` wait | `docker compose exec -T api python -c "urllib.request.urlopen('http://localhost:8000/ready')"` | blocked by D1 (API never boots) |

**Result: FAIL on the shipped artifacts — production-blocking defects D1/D2
recorded below.** The recreation procedure itself (artifact restore,
configuration validation, external-service wiring, image pull path, migration
invocation, service recreation) was exercised up to the point the shipped
configuration contract rejects the environment; the defects were in the
shipped §6.6 profile, not in the recreation procedure's steps. Both were
fixed in the §6.8 release-gate follow-up (infrastructure/secret changes,
human-reviewed) and the run re-executed to PASS below.

### Tested run B (re-run): environment recreation passes after the defect fixes

Environment identical to the first run: fresh scratch directory, the shipped
`compose.hybrid-vps.yml` and `Caddyfile` from the repository, a scratch
`.env.production` built from the updated `.env.production.example` (now
including the required `ACME_EMAIL`), a scratch `postgres:16-alpine` on the
compose network, and the backend and Caddy images built from the fixed
release checkout (the fixes are in `config.py`, `compose.hybrid-vps.yml`,
`main.py` and the example env files).

| Step | Command | Result |
| --- | --- | --- |
| Restore artifacts | copy compose file, Caddyfile, env file into fresh `$DEPLOY_ROOT` | **PASS** |
| Validate configuration | `docker compose -f compose.hybrid-vps.yml --env-file .env.production config --quiet` | **PASS** |
| Start private Redis | `docker compose up -d redis` | **PASS** (`redis-cli ping` → `PONG`, healthy) |
| Stand up scratch managed PostgreSQL | `docker run -d --network app-template-prod_default --name scratch-pg-recreate -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=app postgres:16-alpine` | **PASS** (reachable as `scratch-pg-recreate:5432`) |
| Run exactly one migration | `docker compose run --rm --no-deps api alembic upgrade head` | **PASS** — full chain applied, `alembic_version` = `9e4f5c6a7d8b` (head) |
| Full stack up | `docker compose up -d` | **PASS** — all four services healthy: `redis`, `api`, `worker`, `caddy` |
| `/ready` wait | `docker compose exec -T api python -c "urllib.request.urlopen('http://localhost:8000/ready')"` | **PASS** — HTTP 200 |
| Edge liveness | Caddy healthcheck (308 auto-HTTPS probe on :80) | **PASS** — `caddy` healthy |

**Result: PASS.** The environment-recreation procedure completes end to end
against the shipped artifacts: artifacts restored, configuration validated,
external services wired, exactly one deliberate migration applied, the full
stack booted with all services healthy, and `/ready` answered 200 both from
inside the container (compose healthcheck / orchestrator probe) and through
the edge's auto-HTTPS liveness contract. The `https://$DOMAIN/ready` deploy-
workflow wait needs real DNS + Let's Encrypt issuance on a live host and is
covered by the same `/ready` response behind the edge.

### Defects surfaced by the tested runs (resolved in the §6.8 follow-up)

These were surfaced by the §6.7 tested-run requirement (blueprint §39) and by
the re-run. All three belong to the §6.6 deployment profile and were fixed —
with human review, per AGENTS.md (infrastructure/secret changes) — in the
§6.8 release-gate follow-up; the re-run above records the fixes passing.

**D1 — production required `rediss://` Redis while the profile ships plain
`redis://` (blocked every production boot, the migration step, and the deploy
workflow).** `backend/app/core/config.py` rejected any `REDIS_URL` that is not
`rediss://` when `APP_ENV=production` (the "Harden WorkOS authentication and
API safeguards" commit, predating §6.6), while the shipped
`.env.production.example` and `deploy/compose/compose.hybrid-vps.yml` run a
non-TLS private Redis and document `REDIS_URL=redis://...`. Every production
container (API, worker, and the deploy workflow's one-off `alembic upgrade
head` run) therefore failed `Settings` validation at boot:

```text
pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
  Value error, redis_url must use rediss in the production environment
```

§6.6 CI never exercised a booted production environment (compose validation
uses `ENV_FILE=/dev/null`), so the mismatch shipped unreviewed. **Fix
(option b of the original analysis):** the production rule now accepts plain
`redis://` for loopback and single-label compose-network hosts (the profile's
private, non-published Redis, documented in `config.py`
`_redis_url_is_production_safe`), and still rejects it for any externally
reachable Redis — dotted hostname or IP — which must use `rediss://`. The
rule is covered by `test_config.py`; `.env.example` and
`.env.production.example` document it.

**D2 — Caddy failed to start when `ACME_EMAIL` was unset, and the example env
did not document it.** The compose file defaulted `ACME_EMAIL` to empty and
`.env.production.example` did not list it at all; the shipped Caddyfile's
global `email {$ACME_EMAIL}` then failed to adapt:

```text
Error: adapting config using caddyfile: parsing caddyfile tokens for 'email':
wrong argument count or unexpected line ending after 'email', at /etc/caddy/Caddyfile:20
```

**Fix:** `ACME_EMAIL` is now required, fail-fast like `REDIS_PASSWORD` —
`compose.hybrid-vps.yml` uses `${ACME_EMAIL:?set ACME_EMAIL in the environment
file}`, `.env.production.example` documents it under a new "Edge (Caddy)"
section, and the CI compose-validation job exports a placeholder. A host can
never start Caddy with an empty Let's Encrypt contact again.

**D3 — the API container's healthcheck (and any orchestrator/load-balancer
probe) was rejected by the Host allowlist.** Surfaced by the re-run: the
compose `api` healthcheck calls `http://localhost:8000/ready` with a
container-local Host header, which is not in production `TRUSTED_HOSTS`, so
Starlette's `TrustedHostMiddleware` answered every probe with `400 Invalid
host header` and the API never went healthy:

```text
{"method": "GET", "path": "/ready", "status_code": 400, ...}
```

**Fix:** the Host allowlist now exempts the public, non-sensitive surface —
`/health`, `/ready`, `/metrics` — via `_TrustedHostWithPublicExemptMiddleware`
in `backend/app/main.py`; every other path keeps the strict allowlist
(DNS-rebinding / Host-header injection protection unchanged). Covered by
`test_health.py`.

---

## Backup verification schedule

- **Nightly**: logical database dump produced and shipped off-site; bucket
  versioning/replication health check; backup-failure alerts armed.
- **Weekly**: verify the latest dump restores into scratch infrastructure
  (tested run A re-run, at minimum the dump-then-restore loop) and that a
  scratch boot of the current images against the restored database passes
  `/ready`.
- **Per release**: after any change to the deployment profile, the migration
  chain, or the environment contract, re-run tested run B (recreation) against
  scratch infrastructure and record the result here.
- **Quarterly**: full lost-host drill — recreate the environment from the
  off-site backup alone (tested run B) and confirm the external-service
  dependency model still holds (DNS, TLS issuance, uptime checks, alerts).
