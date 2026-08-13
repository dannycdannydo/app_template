# Single-VPS Demo Deployment Profile — Implementation Plan

Status: Proposed (plan only; no implementation started)

Relates to: `Internal_Custom_Application_Starter_Architecture_v2.md` §§35–39,
`docs/decisions/0007-two-deployment-profiles.md`, `TEMPLATE_V0_6_SCOPE.md`
§§6.6–6.8, `SECURITY.md`, `docs/operations.md`, and
`docs/backup-and-recovery.md`

## 1. Goal

Add a low-cost, low-operations deployment option that runs the complete core
application stack on one small Linux VPS while retaining the existing hybrid
VPS profile unchanged.

The new profile is intended for disposable or easily recreated demonstrations,
sales environments, internal previews, and other low-traffic deployments where
simplicity and cost matter more than high availability, point-in-time recovery,
or independent failure domains.

Before adding the profile, fix and test the shared VPS deployment defects found
in the existing hybrid release path so the new option does not copy known
problems.

The target topology is:

```text
Internet
   |
   v
Caddy (:80/:443 only)
   |-- app.<domain>   -> Vue static artifact + FastAPI
   `-- files.<domain> -> MinIO S3 API

Private Compose network
   |-- FastAPI
   |-- Dramatiq worker
   |-- PostgreSQL
   |-- Redis
   |-- MinIO
   `-- Mailpit (optional demo SMTP capture)

Named volumes
   |-- PostgreSQL data
   |-- MinIO objects
   |-- Redis AOF
   `-- Caddy certificates/configuration
```

## 2. Deployment-profile contract

### 2.1 Profiles after this work

| Profile | Purpose | Runs on the host | External dependencies |
| --- | --- | --- | --- |
| Hybrid VPS | Low-cost production baseline | Caddy, frontend, API, worker, Redis | Managed PostgreSQL, object storage, WorkOS, SMTP, monitoring |
| Single-VPS demo | Simplest low-cost demonstration | Caddy, frontend, API, worker, Redis, PostgreSQL, MinIO, optional Mailpit | WorkOS; enabled AI providers; optional external SMTP/monitoring |
| Fully managed | Enterprise/provider deployment | Provider-managed application services | Provider-managed data and supporting services |

The single-VPS profile is additive. It must not change the service topology,
durability promises, or defaults of `compose.hybrid-vps.yml`.

### 2.2 Explicit trade-offs

- The VPS is a single failure domain for the application, database, queue, and
  uploaded objects.
- Host loss may cause total data loss unless a provider snapshot or optional
  off-host backup has been configured.
- No PostgreSQL PITR, MinIO replication, rolling availability, or automatic
  failover is promised by this profile.
- VPS/provider snapshots are an acceptable minimum for disposable demos; they
  are not represented as a production-grade backup system.
- Resource limits and reduced worker concurrency prioritise predictable
  operation on a small host over throughput.
- The security baseline remains mandatory: TLS, private data services,
  non-default credentials, no public database/Redis/MinIO-console ports,
  non-root application containers, log rotation, and secrets outside Git.

### 2.3 Out of scope

- Replacing or self-hosting WorkOS.
- Operating a public outbound mail server. Mailpit may capture demo mail;
  deliverable email continues to use an external SMTP relay.
- Self-hosting Sentry, Prometheus, or an AI model as part of this profile.
- High availability, multi-host orchestration, database replication, or
  zero-downtime migrations.
- Changing application APIs, permissions, tenant isolation, or database
  schemas.
- Reusing `compose.local.yml` as a production file; its published data-service
  ports, development credentials, and development runtime settings make it an
  unsuitable deployment artifact.

## 3. Required governance and sequencing

This work changes infrastructure, secret handling, and backup/recovery
documentation. Human review is required before each work unit is applied.
Every work unit follows `CONTRIBUTING.md` exactly:

```text
implement -> run relevant validation -> reviewer handoff -> review
          -> address findings -> apply and commit
```

Do not combine all work into one unreviewed change. Do not commit or mark a work
unit complete until its review has passed. Assign the work to a versioned scope
subsection before implementation so all new code and documentation use a
version-prefixed scope citation.

The units below are ordered dependencies. Shared hybrid fixes land first; the
single-VPS profile builds only on the reviewed result.

## 4. Work unit 1 — Repair and harden the shared VPS release path

### 4.1 Frontend artifact layout

Fix the mismatch between the deployment workflow and Caddy mount contract:

- `deploy-vps.yml` currently archives the contents of `frontend/dist` and
  extracts them directly into `releases/<sha>/`.
- `compose.hybrid-vps.yml`, `docs/operations.md`, and
  `docs/backup-and-recovery.md` expect the files at
  `releases/<sha>/frontend/` through `releases/current/frontend`.
- Establish `releases/<sha>/frontend/` as the one canonical contract and
  extract the archive into that directory.
- Keep checksum verification before extraction and switch the `current`
  symlink only after the artifact has been verified and staged successfully.
- Make repeated deployment of the same SHA idempotent without mixing stale
  files into the artifact directory.

Add a CI assertion that packages a small fixture artifact, executes the same
staging logic, and proves `releases/current/frontend/index.html` exists. A
Compose syntax check alone is not sufficient.

### 4.2 Domain propagation

- Make the application domain an explicit, required deployment input for a
  real deployment.
- Pass it into the remote Compose invocation or persist it in the host
  environment file; do not let the workflow's public check use one domain
  while Caddy silently receives the `localhost` default.
- Reconcile `.env.production.example`, workflow inputs, and Compose
  interpolation so each value has one documented source of truth.
- Retain a deliberate localhost value only for CI/scratch validation.

### 4.3 Environment-specific frontend builds

- Ensure the frontend build receives the selected staging or production
  environment's `VITE_WORKOS_CLIENT_ID`, redirect URI, and API base URL.
- Do not rely accidentally on repository-level variables when GitHub
  environment-level separation is intended.
- Fail before packaging when required public production build settings are
  missing.
- Record the public build settings alongside the release metadata so a
  reviewer can identify which environment an artifact targets without
  exposing secrets.

### 4.4 Registry and host prerequisites

- Document and validate the remote host's registry authentication when images
  are private. The runner's registry login does not authenticate the VPS.
- Add a fail-fast remote pull/authentication check before migration or service
  recreation.
- Document Docker Engine, Compose, `flock`, SSH user/group access, firewall,
  DNS, release-directory ownership, and registry-login prerequisites in one
  host-bootstrap section.
- Keep private-key and registry credentials out of command output and release
  metadata.

### 4.5 Deployment verification

Extend deployment verification beyond the API database probe:

- Verify all expected Compose services are running and healthy.
- Verify the public `/ready` endpoint.
- Verify `/` returns the staged frontend rather than an empty Caddy document
  root or error page.
- Verify the worker process is healthy.
- Keep `/ready` itself focused on application readiness unless a separate
  design review deliberately expands its contract; deployment smoke checks
  can test the wider topology without coupling every API request to storage or
  SMTP availability.
- Correct inaccurate operational comments, including any claim that
  `docker compose run --no-deps` detaches a migration container from the
  Compose network.

### 4.6 Acceptance criteria

- A scratch hybrid deployment serves the expected `index.html` and `/ready`.
- The domain supplied to the workflow is the domain present in the effective
  Caddy configuration.
- A missing required frontend setting or unavailable private image fails before
  migration.
- Artifact-layout, Compose, Caddy, and deployment smoke validations run in CI.
- Existing rollback retains and serves the previous frontend artifact.
- Hybrid deployment documentation matches observed commands and paths.
- Human review of infrastructure and secret-handling changes is recorded.

## 5. Work unit 2 — Record the single-VPS demo architecture decision

### 5.1 Architecture and ADR changes

- Add the single-VPS demo profile to blueprint §35 as a distinct deployment
  profile with its intended use and explicit non-production durability
  posture.
- Amend or supersede ADR-0007. Its current “two deployment profiles” decision
  does not describe the new third option.
- Keep one backend image and the provider-neutral application/storage
  interfaces unchanged, consistent with blueprint §36.
- State that the demo profile is a deployment topology, not a new application
  environment value: the containers still run with `APP_ENV=production` so
  debug mode and fake production adapters remain prohibited.
- Add a versioned scope work unit and reference map covering BP §§27, 28,
  35–39 and the relevant security sections before implementation begins.

### 5.2 Naming and files

Use names that make the reduced resilience obvious:

```text
deploy/compose/compose.single-vps-demo.yml
deploy/caddy/Caddyfile.single-vps-demo
.env.single-vps-demo.example
.github/workflows/deploy-single-vps-demo.yml
```

Prefer a separate workflow initially. This avoids making the proven hybrid
path conditional on many topology-specific branches. Shared, tested staging
logic may be extracted into scripts or reusable workflow steps if doing so
reduces duplication without obscuring either profile.

### 5.3 Acceptance criteria

- The blueprint and ADR unambiguously distinguish hybrid, demo, and managed
  profiles.
- The demo limitations cannot reasonably be mistaken for hybrid-profile
  guarantees.
- No application API, ORM model, permission, or migration change is introduced.
- Architecture/infrastructure changes receive human review before application.

## 6. Work unit 3 — Add the single-VPS Compose topology

Create `compose.single-vps-demo.yml` by following production patterns from the
hybrid profile and taking only the PostgreSQL/MinIO service concepts—not the
development exposure or credentials—from `compose.local.yml`.

### 6.1 Shared application services

- Reuse the reviewed Caddy/frontend artifact contract from work unit 1.
- Reuse the immutable backend image for API, migration, and worker commands.
- Retain restart policies, health checks, graceful shutdown, bounded JSON log
  rotation, and non-root backend execution.
- Run one API and one worker by default.
- Default `WORKER_CONCURRENCY` to 2 for this profile while leaving the hybrid
  default unchanged.
- Publish only Caddy ports 80 and 443. Do not publish API, worker, PostgreSQL,
  Redis, MinIO API, MinIO console, Mailpit SMTP, or Mailpit UI ports.

### 6.2 PostgreSQL

- Use the same supported PostgreSQL major version as local/CI unless the
  architecture decision deliberately pins a different production version.
- Require non-default database name, username, and password through the demo
  environment file.
- Connect API, worker, and migration containers over the private Compose
  network.
- Add a durable named volume, health check, restart policy, resource limit,
  and log rotation.
- Ensure `DATABASE_URL` and the PostgreSQL service credentials cannot drift
  silently. Because dotenv values are not recursively expanded, either derive
  the URL in Compose from required inputs or add a validation step that proves
  both settings agree without printing the password.
- Do not publish port 5432; document `docker compose exec` and an optional SSH
  tunnel for administrative access.

### 6.3 Redis

- Follow the hybrid Redis configuration: required password, private network,
  AOF, memory cap, eviction policy, health check, and persistent volume.
- Reduce the demo memory cap only if integration testing proves the chosen
  value supports the API limiter and demonstration jobs.
- Do not represent Redis as a durable source of truth.

### 6.4 MinIO

- Pin the MinIO image to a reviewed version; do not use an unbounded floating
  production tag.
- Require non-default root credentials and a bucket name.
- Add a durable named volume, health check, restart policy, resource limit,
  and log rotation.
- Keep the MinIO console private. Document access through an SSH tunnel or
  `docker compose exec` when necessary.
- Add a one-shot, idempotent `minio-init` service using a pinned MinIO client
  image to create the private bucket and apply the reviewed browser CORS
  policy. Verify the exact client command against the pinned release during
  implementation.
- The bucket must never be made anonymously readable or writable.

### 6.5 Demo email

- Provide Mailpit behind an explicit Compose profile such as `demo-mail` or
  enable it by default only if the simplest documented command remains clear.
- Configure the application through the real SMTP adapter
  (`EMAIL_PROVIDER=smtp`, `SMTP_HOST=mailpit`); do not weaken production
  validation by allowing the fake adapter.
- Keep its UI and SMTP port private. Document an SSH tunnel for viewing
  captured messages.
- Document the environment switch to an external SMTP relay when real delivery
  is required.

### 6.6 Resource baseline

Document an initial target of 2 vCPU, 4 GB RAM, and 40–80 GB SSD for a quiet
demo, with images built in CI rather than on the VPS. Set limits that fit that
host while preserving operating-system and filesystem-cache headroom. Document
4 vCPU / 8 GB as the safer choice for larger demonstrations or concurrent
file/job activity. Local AI inference is outside these figures.

### 6.7 Acceptance criteria

- `docker compose config` fails when required database, Redis, MinIO, domain,
  or ACME settings are absent and succeeds with documented placeholders.
- Only ports 80 and 443 are published in the effective configuration.
- A fresh empty-volume boot makes PostgreSQL, Redis, and MinIO healthy.
- The MinIO initializer is idempotent and leaves the bucket private.
- API, migration, and worker containers can resolve and authenticate to all
  required internal services.
- Container limits fit the documented minimum host.
- Restarting or recreating containers preserves PostgreSQL and MinIO data.
- No hybrid Compose behavior changes.

## 7. Work unit 4 — Add storage ingress and browser-upload support

### 7.1 Caddy sites

Create a demo-specific Caddyfile with two required hostnames:

- `APP_DOMAIN`: serve the versioned Vue artifact and reverse-proxy the existing
  API, health, readiness, and metrics paths.
- `STORAGE_DOMAIN`: reverse-proxy the S3 API to MinIO without exposing the
  administration console.

Retain TLS, HSTS, security headers, compression, log formatting, and the pinned
rate-limiting implementation. Apply a separately reviewed upload limit/rate
policy that does not break legitimate signed PUT requests.

### 7.2 Storage settings and signatures

- Set `STORAGE_ENDPOINT_URL=http://minio:9000` for backend operations.
- Set `STORAGE_PUBLIC_ENDPOINT_URL=https://${STORAGE_DOMAIN}` for browser-facing
  pre-signed URLs.
- Confirm that generated signatures validate through Caddy without host or path
  rewriting.
- Add `https://${STORAGE_DOMAIN}` to the frontend CSP `connect-src` policy.
- Configure MinIO CORS for the exact `https://${APP_DOMAIN}` origin, required
  methods/headers, and no wildcard credentials policy.
- Keep API CORS and trusted hosts limited to the application domain; do not add
  MinIO merely to satisfy an unrelated backend allowlist.

### 7.3 End-to-end validation

Add an integration smoke test covering:

1. application requests an upload intent;
2. browser-equivalent client sends an OPTIONS preflight to the public storage
   endpoint;
3. client PUTs using the signed URL and declared content type;
4. application completes and heads the object through the internal endpoint;
5. worker processes the file;
6. a signed download returns the original bytes;
7. an unsigned read remains denied.

Use stubbed WorkOS authentication or the repository's existing authenticated
test conventions; CI must not call real WorkOS.

### 7.4 Acceptance criteria

- Both hostnames receive valid effective Caddy configurations.
- Browser preflight, signed upload, completion, processing, and signed download
  pass through the deployed topology.
- No MinIO credential or private service port reaches the frontend.
- Unsigned object access and public MinIO-console access are denied.
- Hybrid Caddy behavior and CSP remain unchanged unless a shared, reviewed fix
  is required.

## 8. Work unit 5 — Add the single-VPS deployment workflow

Create `deploy-single-vps-demo.yml`, reusing the reviewed artifact and image
contracts without changing the hybrid workflow's default trigger behavior.

### 8.1 Inputs and artifacts

- Accept environment, ref, host, registry, release directory, application
  domain, and storage domain inputs.
- Use a distinct deployment concurrency group and Compose project name so the
  profile cannot collide with hybrid or local stacks.
- Build and publish immutable backend and Caddy images plus the versioned
  frontend artifact.
- Build the frontend with the chosen environment's public WorkOS settings.
- Verify checksums and stage the artifact at the canonical
  `releases/<sha>/frontend/` path.

### 8.2 First deployment and update order

Under the existing deployment lock:

1. validate the environment and effective Compose configuration;
2. verify registry access and pull immutable images;
3. start/await PostgreSQL, Redis, and MinIO;
4. run and verify the idempotent MinIO initializer;
5. run exactly one deliberate `alembic upgrade head`;
6. recreate API, worker, and Caddy;
7. wait for container health;
8. verify public `/ready`, frontend `/`, and MinIO health through the storage
   hostname;
9. retain the previous release for application rollback.

Do not run migrations automatically in the API startup command. A failed
migration must stop deployment before the application image is activated.

### 8.3 Rollback semantics

- Application/frontend rollback follows the existing immutable image and
  release-symlink model.
- Database migrations remain forward-only. Explicitly state that an
  application rollback does not undo a schema change.
- Do not claim database rollback when no pre-deployment snapshot/dump exists.
- Retain volumes across release cleanup and `docker compose down`; reserve
  `down --volumes` for an explicitly destructive demo reset command with a
  prominent warning.

### 8.4 Acceptance criteria

- A fresh VPS can deploy from documented prerequisites and one workflow run
  plus creation of the protected environment file.
- A second deployment updates the application without losing database rows or
  objects.
- A deliberately invalid image, migration, domain, or frontend artifact fails
  before reporting success.
- Public smoke tests cover the UI, API readiness, and storage edge.
- Hybrid tags and workflow dispatch behavior remain unchanged.
- Infrastructure and secret-handling review is recorded.

## 9. Work unit 6 — Demo operations, reset, and recovery documentation

Add a separate runbook section or `docs/single-vps-demo.md`; do not dilute the
hybrid production runbook's guarantees.

Document:

- DNS for application and storage hostnames.
- Host bootstrap and firewall configuration allowing only SSH, HTTP, and HTTPS.
- Environment-file creation, permissions, secret generation, WorkOS redirect
  URI/origin setup, deployment, status checks, logs, and upgrades.
- Access to PostgreSQL, MinIO console, and Mailpit through SSH tunnels without
  publishing their ports.
- Disk inspection and cleanup that cannot remove named data volumes by
  accident.
- A clearly labelled destructive “reset the disposable demo” procedure that
  identifies the exact Compose project and volumes and requires explicit
  operator confirmation. Do not make reset part of normal deployment.
- Optional VPS snapshots as the simplest whole-host recovery mechanism.
- Optional nightly `pg_dump` and MinIO mirror/sync to an off-host destination
  for demos that become valuable. Include restore commands and a small scratch
  restore test before describing these copies as backups.
- Lost-host recreation both with and without a snapshot/backup.
- Promotion guidance: move PostgreSQL and object storage to the hybrid profile
  before the environment acquires production availability or recovery
  requirements.

Update `README.md`, `.env.single-vps-demo.example`, `SECURITY.md`, and the ADR
with consistent links and warnings. Do not add real credentials or encourage
committing the deployment environment file.

### Acceptance criteria

- A new operator can distinguish disposable reset, application rollback, and
  data recovery.
- Every command names an explicit deployment directory/project; no destructive
  command targets a broad path or unresolved variable.
- Documentation does not claim PITR, replication, or guaranteed RPO/RTO.
- Optional backups have a tested restore procedure before being called valid.
- Backup/recovery and secret-handling documentation receives human review.

## 10. Work unit 7 — Release validation and final audit

Run the complete repository and deployment validation after all preceding work
units have individually passed review:

- `make check`
- `make e2e`
- hybrid Compose validation with a clean environment
- single-VPS Compose validation, including required-variable failure cases
- both Caddy image/configuration validations
- backend image build
- frontend artifact layout/rollback tests
- scratch hybrid deployment smoke test
- fresh-volume single-VPS deployment smoke test
- single-VPS restart/persistence test with marker database and object data
- browser-equivalent MinIO CORS and signed upload/download test
- network exposure assertion proving only 80/443 are published
- secret scan and container scan through the existing CI gates

Perform an architecture and security review focused on:

- accidental weakening of the hybrid profile;
- secrets exposed through Compose interpolation, workflow logs, artifacts, or
  frontend build variables;
- database or MinIO ports published to the host;
- public MinIO bucket/console access;
- signature-host/CORS/CSP disagreement;
- migration ordering and misleading rollback claims;
- volume deletion during deploy or release cleanup;
- resource limits that exceed the documented minimum host;
- documentation that could cause a demo profile to be mistaken for a
  production-resilient deployment.

The work is complete only when both deployment profiles validate independently,
the scratch end-to-end runs pass, the documentation matches the observed
commands, and the required human reviews are recorded.

## 11. Proposed work-unit checklist

- [ ] WU1 — Repair shared frontend staging, domain/config propagation, registry prerequisites, and deployment smoke checks
- [ ] WU2 — Amend blueprint/scope and supersede or update ADR-0007 for the explicit demo profile
- [ ] WU3 — Add private PostgreSQL, Redis, MinIO, optional Mailpit, persistent volumes, and resource limits in `compose.single-vps-demo.yml`
- [ ] WU4 — Add dual-domain Caddy ingress, MinIO initialization/CORS, CSP, and signed-transfer integration coverage
- [ ] WU5 — Add the immutable, locked, migration-aware single-VPS deployment workflow and rollback behavior
- [ ] WU6 — Add environment, bootstrap, operations, reset, snapshot, optional backup/restore, and promotion documentation
- [ ] WU7 — Run full quality gates, both-profile scratch deployments, persistence/security audit, and final human review

## 12. Decisions to confirm during WU2 review

The plan recommends these defaults; change them only through the architecture
review, not ad hoc during Compose implementation:

1. Use `app.<domain>` and `files.<domain>` rather than path-prefixing MinIO.
   S3 signatures cover host and path, so a dedicated hostname is the simpler
   and less fragile ingress contract.
2. Keep `APP_ENV=production` even for disposable demos.
3. Use Mailpit for captured demo email and an external SMTP relay for real
   delivery; do not operate a public mail server in this profile.
4. Keep the MinIO console private and accessible only through an SSH tunnel.
5. Use provider snapshots as the documented minimum demo recovery option, with
   off-host logical/object backups optional but tested when enabled.
6. Start with a separate deployment workflow, sharing only already-tested
   artifact/staging helpers with the hybrid workflow.
7. Treat WorkOS and enabled AI providers as external dependencies; self-hosting
   either requires a separate reviewed design.
