# 01 — SeaweedFS for Local S3 Development and Integration Tests

Status: Active

Replace the unavailable MinIO community image in local development and CI with
SeaweedFS as the S3-compatible test server. Keep the application's provider-
neutral `ObjectStorage` interface and production S3 configuration unchanged.
The work starts by proving that SeaweedFS supports the exact storage behaviours
the repository relies on; only then does it replace the existing service.

## Goal

Make `make dev`, the Docker Compose full-stack path and the CI storage-integration
job reproducible without depending on MinIO's withdrawn community container
images, while retaining meaningful tests of signed S3 uploads, private access,
bucket creation, object lifecycle and browser CORS.

## Agreed scope

### Outcome and boundary

- Use SeaweedFS's single-node `weed mini` mode for local development and
  service-backed CI tests.
- Keep the `S3Storage` adapter, `STORAGE_*` application settings, bucket
  semantics and production-provider choice unchanged. SeaweedFS is a local/CI
  implementation of the existing S3 boundary, not a new application storage
  provider or production infrastructure commitment. The single sanctioned
  adapter change is P4's explicit SigV4 presigning, which happens only after the
  migration is complete (see Decisions and assumptions).
- Pin the SeaweedFS release and image by immutable digest once selected. Define
  health checks, S3 endpoint, credentials, persistent local data and CORS setup
  explicitly rather than relying on an unpinned `latest` image or permissive
  defaults.
- Run the same `storage_integration` tests locally and in CI. Do not weaken or
  skip signed-request, private-bucket, CORS or object-lifecycle assertions just
  to accommodate the replacement.

### Migration journey

1. Inventory the local Compose service, CI service job, storage integration
   tests, settings examples, tests of deployment wiring and docs that name
   MinIO-specific behavior.
2. Prove the candidate SeaweedFS release can pass the full integration suite,
   including presigned PUT/GET, authenticated access, unsigned denial, lazy
   bucket creation, copy/promotion, and allowed/forbidden browser origins.
3. Record the tested image digest, command, health endpoint, endpoint/port,
   credentials and S3/CORS configuration. Confirm the chosen release and
   licensing/provenance are suitable for this repository's local/CI use.
4. Replace MinIO in `deploy/compose/compose.local.yml` and the dedicated CI
   workflow; update service dependencies, ports, health checks, volume names,
   CORS provisioning and test environment configuration together.
5. Update `.env.example`, operations/release instructions, tests and relevant
   ADRs/area guides so they describe SeaweedFS where the implementation has
   changed. Keep history clear that production supports S3-compatible providers
   generally and is not coupled to SeaweedFS.
6. Run the integration suite against SeaweedFS, then the required local gates,
   including Compose validation, `make check` and `make e2e` where listed by the
   applicable contract.

## Out of scope

- Changing the production object-storage provider, endpoint, bucket policy,
  retention, credentials or backup design.
- Adding a SeaweedFS application adapter or provider-specific application code.
- Moving production objects or migrating data between storage products.
- Introducing LocalStack or a paid cloud emulator, cloud credentials in CI, or
  a new storage dependency in the backend runtime.
- Replacing the S3 integration tests with mocks, reducing coverage, weakening
  CORS/private-bucket checks, or treating a skipped integration suite as proof.
- Building and maintaining a private MinIO image as the long-term solution.

## Decisions and assumptions

- **The application remains S3-provider neutral.** ADR-0006 and ADR-0014 keep
  provider SDKs behind `app/storage`; this work changes only the local/CI S3
  server and its supporting documentation/configuration.
- **SeaweedFS must pass the existing behavioral contract.** Compatibility is
  demonstrated by the repository's live integration tests, not inferred from
  general S3-compatibility claims. Any required test-server setup may change,
  but observable safety assertions may not be removed.
- **Pin and verify the artifact.** Select a maintained SeaweedFS release with a
  public image, verify its source/release correspondence and license, and pin
  the immutable image digest in both Compose and CI. Document the upgrade
  procedure; do not silently follow `latest`.
- **MinIO is not a reliable image source.** The current CI job fails before
  tests because the pinned Quay image returns `unauthorized`; the official
  community repository says it is source-only and is archived. This plan
  avoids treating a third-party mirror or another MinIO registry as a durable
  dependency.
- **Presigned URLs move to SigV4 in P4 (owner decision, 2026-09-29).** P1
  found that `S3Storage` sets no `signature_version`, so botocore presigns with
  SigV2 query authentication, which does not sign `Host` and is deprecated by
  AWS. The owner chose SigV4. It is sequenced as P4, after the storage server
  migration, so that P1–P3 prove the replacement server against unchanged
  adapter behaviour. SigV4 caps presigned-URL lifetime at 7 days; this is
  accepted because the template's signed URLs last at most 1 hour. Long-lived
  links for users who aren't logged in (for example, file links inside shared
  exports) are out of scope here. When an app needs them, they will use
  app-issued share links that redirect to a fresh SigV4 URL, not raw storage
  URLs (`docs/seaweedfs-compatibility-findings.md`, finding 1).
- **Human infrastructure review applies.** The selected container image,
  local service configuration and CI workflow are infrastructure changes and
  require repository-owner review before application.

## Commands that must work

The application command surface remains unchanged. The storage service must
continue to be brought up through the existing local commands:

- `make dev` starts the infrastructure set and the native app processes.
- `make dev-docker` starts the full-stack profile and uses the internal Compose
  service endpoint for API/worker/coordinator storage access.
- `cd backend && uv run pytest -m storage_integration` runs the real-server
  storage tests when the configured endpoint is available.
- `make check` and `make e2e` retain their existing meaning and must not gain a
  dependency on an external cloud account.

## Acceptance criteria

- A clean local Compose startup pulls the pinned SeaweedFS image, reaches a
  healthy S3 endpoint, and creates/retains its named local data volume.
- Host-native development and the full-container profile both address the
  service correctly; presigned URLs use the browser-reachable host, while
  server-side calls use the Compose service endpoint where appropriate.
- The full `storage_integration` suite passes against SeaweedFS, including
  signed upload/download (SigV2 query-auth through P3; SigV4 with a
  host-bound signature after P4), unsigned read/write denial, lazy bucket
  creation, head/delete behavior, copy/promotion and allowed/forbidden CORS
  origins.
- CI starts the pinned SeaweedFS artifact without registry credentials, waits
  for a deterministic health signal, runs the same integration suite, and
  fails on service startup or test failure rather than silently skipping.
- Compose and deployment-boundary tests assert SeaweedFS configuration,
  endpoint, CORS policy, credentials and volume wiring; no stale MinIO-specific
  runtime setting remains in the active local/CI paths.
- Production `STORAGE_*` configuration and `S3Storage` behavior remain
  provider-neutral and unchanged, except that P4 makes presigned URLs
  explicitly SigV4.
- Relevant documentation identifies SeaweedFS as the local/CI test service
  and states that it is not the required production storage provider.

## Implementation checkpoints

### P1 — Verify SeaweedFS compatibility and pin

- [x] Inventory every MinIO reference that controls local startup, CI, tests,
  environment examples, runbooks and accepted storage decisions.
- [x] Select a maintained SeaweedFS version and verify its image provenance,
  license, immutable digest, S3 endpoint, startup/health procedure and supported
  single-node `weed mini` mode.
- [x] Run the current storage integration suite against the candidate server. Prove
  signed URL host style, authentication/private access, bucket creation,
  copy/promotion and the allowed/denied CORS-origin tests. Record any server
  setup differences without weakening assertions.
- [x] Document compatibility gaps or stop before migration if the required S3
  behaviors cannot be tested reliably.

Dependencies: owner approval to begin the infrastructure work; P1 evidence is
required before changing the existing MinIO configuration.

Human review required before application: selected image/version/digest,
artifact provenance and license, and evidence that the existing security-
relevant storage assertions pass.

### P2 — Migrate local Compose and CI

- [x] Replace the local MinIO service with pinned SeaweedFS configuration, keeping
  host/native and Compose-internal S3 endpoints aligned with `STORAGE_*`.
- [x] Provide the local data volume, startup command, health check, credentials and
  non-wildcard browser CORS configuration needed by the existing workflow.
- [x] Replace the CI MinIO container step with the same pinned SeaweedFS artifact
  and configuration; run the entire `storage_integration` marker against it.
- [x] Update `make dev`/`make dev-docker` dependencies, deployment-boundary tests,
  `.env.example`, CI comments and operations/release docs. Remove MinIO-specific
  settings only when no remaining local/CI consumer needs them.
- [x] Preserve storage integration coverage and keep application production
  configuration provider-neutral.

Dependencies: P1 complete and reviewed.

Human review required before application: local infrastructure/Compose changes,
CI image and service changes, and any new environment or credential handling.

### P3 — Verify, document and retire MinIO test dependencies

- [ ] Run the service-backed integration suite from a clean local environment and
  on the PR CI workflow; confirm no test is skipped due to endpoint or CORS
  configuration.
- [ ] Run `make validate-execution-contracts`, Compose validation, the required
  complete local validation gate and end-to-end suite for this work unit.
- [ ] Search the repository for active MinIO image, service-name, port,
  credentials, CORS and documentation references; retain only historical
  context or examples explicitly marked as such.
- [ ] Update ADR-0008/ADR-0014 and operations/release documentation to reflect the
  tested local/CI service while retaining their provider-neutral production
  decisions.

Dependencies: P2 complete and reviewed.

Human review required before application: final documentation/decision updates
and confirmation that local developer workflows and CI now use the same tested
storage contract.

### P4 — Presign storage URLs with SigV4

- [ ] Set `signature_version="s3v4"` explicitly in the `S3Storage` client
  `Config`, so both the data client and the public-endpoint pre-signing client
  produce SigV4 presigned PUT/GET URLs (`X-Amz-Algorithm=AWS4-HMAC-SHA256`,
  `host` in `X-Amz-SignedHeaders`). No other adapter behaviour, `STORAGE_*`
  setting, TTL or key layout changes.
- [ ] Add unit tests in `backend/tests/test_storage_s3.py` that assert SigV4
  query parameters on upload and download URLs, including the split
  public-endpoint case, and that configured TTL bounds stay under the SigV4
  7-day maximum.
- [ ] Extend `backend/tests/test_storage_integration.py` (adding assertions,
  never weakening them) to prove, against the pinned SeaweedFS, that signed
  URLs are SigV4 and that a signed URL replayed against a different host is
  refused with `403`. The CI `storage-integration` job must run these with no
  skips.
- [ ] Confirm that consumers of adapter-issued URLs are unaffected: the
  browser direct-upload path (`frontend/src/lib/upload.ts`), the AI transfer
  and managed-signed-URL paths, and the e2e storage journey.
- [ ] Update ADR-0014 with the signature-version decision, the 7-day presign
  cap and the share-link pattern for long-lived links. Also update the
  `ARCHITECTURE.md` storage section, `backend/AGENTS.md` where it states
  adapter rules, and the status of finding 1 in
  `docs/seaweedfs-compatibility-findings.md`.

Dependencies: P3 complete and reviewed; owner decision recorded in Decisions
and assumptions.

Human review required before application: the change to the storage adapter's
signing behaviour (security-relevant signed-URL semantics) and the ADR-0014
amendment.

## Reference map

| Checkpoint | Governing sections and documents |
| --- | --- |
| P1 | BP §§17, 32, 36, 42, 44; ADR-0006; ADR-0008; ADR-0014; `.github/workflows/ci.yml`; `backend/tests/test_storage_integration.py`; SeaweedFS release/image and S3 compatibility documentation. |
| P2 | BP §§17, 32, 36, 42; ADR-0008; ADR-0014; `deploy/compose/compose.local.yml`; `.env.example`; `.github/workflows/ci.yml`; `backend/tests/test_deployment_boundaries.py`; `backend/tests/test_storage_integration.py`; `docs/operations.md`. |
| P3 | BP §§17, 31, 36, 42, 44; ADR-0006; ADR-0008; ADR-0014; `CONTRIBUTING.md`; `docs/operations.md`; `docs/release-checklist.md`; `backend/AGENTS.md`. |
| P4 | BP §§17, 30, 31; ADR-0006; ADR-0014; `backend/app/storage/s3.py`; `backend/tests/test_storage_s3.py`; `backend/tests/test_storage_integration.py`; `docs/seaweedfs-compatibility-findings.md` (finding 1); `ARCHITECTURE.md`; `backend/AGENTS.md`. |

## API, data and security impact

- No application endpoint, API schema, database table, migration, permission or
  tenant-isolation policy changes.
- No production storage endpoint, bucket, data-retention or credential
  semantics change. The only adapter change is P4's SigV4 presigning. It
  strengthens signed URLs (the host is signed, and signing no longer relies on
  a deprecated scheme) and limits presigned-URL lifetime to 7 days.
- Local/CI container images, ports, credentials, CORS configuration and named
  volumes are infrastructure. Do not expose the S3 service outside the local
  development/CI network or permit wildcard origins.
- Signed URL behavior, private-bucket denial and allowed/forbidden browser
  origins remain security acceptance criteria and must be tested against the
  replacement server.

## Validation plan

- P1: inspect SeaweedFS release provenance and license; run
  `cd backend && uv run pytest -m storage_integration` against the candidate;
  retain the complete test output and verify each test executes rather than
  skips.
- P2: run `docker compose -f deploy/compose/compose.local.yml config`, the
  storage integration suite against the local service and CI-equivalent
  environment, plus focused deployment-boundary and Compose tests.
- P3: run the complete `make check`, `make e2e`, migration/compose checks
  required by the active repository contract, and the service-backed storage
  integration job on the PR.
- P4: run the focused `test_storage_s3.py` tests and
  `uv run pytest -m storage_integration` against the pinned SeaweedFS with no
  skips, then the complete gate in prompt 03 and the PR `storage-integration`
  job.

## Review and delivery

- Follow `CONTRIBUTING.md`: implement, focused checks, independent review,
  complete local gate, then apply-and-commit on a dedicated
  `feature/seaweedfs-storage-tests` branch.
- Do not alter or bypass the storage integration assertions to achieve green
  CI. Diagnose any SeaweedFS behavior mismatch and record an explicit decision
  before changing the test contract.
- Obtain repository-owner review for infrastructure, image provenance/licence
  and CI changes before merging. Merge only when local validation and PR CI are
  green.
- This plan is the single `Active` execution contract: the owner scheduled the
  migration on 2026-09-29. Each checkpoint (P1–P4) is a separate work unit
  with its own repository-owner review gate; set `Status: Complete` only when
  every checkpoint is checked.
