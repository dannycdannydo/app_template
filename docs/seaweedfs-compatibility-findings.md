# SeaweedFS P1 Compatibility - Findings

Status: P1 evidence for `plans/01-seaweedfs-storage-testing.md` (checkpoint
P1 - verify SeaweedFS compatibility and pin). Recorded 2026-09-29.

This records the MinIO inventory, the selected SeaweedFS artefact and its
provenance, and the result of the repository's live `storage_integration`
suite against it. It is evidence for the P1 human-review gate: approving it
approves the **candidate** image and configuration for local/CI use only. It
changes no application code, no Compose file and no CI workflow; P2 performs the
migration.

SeaweedFS is a local/CI implementation of the existing S3 boundary. The
`S3Storage` adapter, the `STORAGE_*` settings and the production provider choice
stay unchanged and provider-neutral (ADR-0006, ADR-0014).

## Selected artefact

| Item | Value |
| --- | --- |
| Release | SeaweedFS `4.48` (published 2026-09-28; the maintained upstream release line, weekly cadence) |
| Source commit | `530be3e37337488ecc34d58441e0bc476e121c93` (the `4.48` git tag) |
| Image | `chrislusf/seaweedfs:4.48@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d` (multi-arch index) |
| Mirror | `ghcr.io/chrislusf/seaweedfs:4.48` serves the **same** index digest |
| linux/amd64 manifest | `sha256:aba492e2a4e4c90bff795745e8e660affa1f09e7650f5981bd7bccd1a06cd931` |
| linux/arm64 manifest | `sha256:f1f303474940e9e088edbd2c1658f7ac38cb0dd90f91a2b0a51916d74b8575a1` |
| Licence | Apache-2.0 (GitHub repository licence and the image's `org.opencontainers.image.licenses` label) |
| Registry access | Anonymous pull from Docker Hub and GHCR; no registry credentials |

### Provenance checks performed

- The image's OCI labels name `org.opencontainers.image.source=https://github.com/seaweedfs/seaweedfs`,
  `version=4.48` and `revision=530be3e3…`; `git/ref/tags/4.48` on the upstream
  repository resolves to the same commit.
- `weed version` inside the pinned image prints `4.48 530be3e37 linux amd64`.
- The upstream `container_release_unified.yml` workflow at the `4.48` tag builds
  on tag push and publishes the release image to both
  `ghcr.io/chrislusf/seaweedfs` and `docker.io/chrislusf/seaweedfs`. Both
  registries return the digest above, so the Docker Hub image is the
  upstream-built artefact and not a third-party mirror.
- The repository is not archived, is actively maintained, and releases carry
  per-tag notes on GitHub.

The binary prints a notice about a commercial Enterprise edition. The pinned
image is the Apache-2.0 open-source build; nothing in the template depends on
the Enterprise edition.

## Candidate configuration

The whole `storage_integration` suite passed with this exact command. It is the
recommended starting point for P2.

```sh
docker run --rm \
  --publish 127.0.0.1:9000:9000 \
  --volume <named-volume>:/data \
  --env AWS_ACCESS_KEY_ID=<dev access key> \
  --env AWS_SECRET_ACCESS_KEY=<dev secret key> \
  chrislusf/seaweedfs:4.48@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d \
  mini -dir=/data -s3.port=9000 \
       -s3.allowedOrigins=<exact frontend origin> \
       -master.telemetry=false -admin.ui=false -webdav=false
```

| Concern | Finding |
| --- | --- |
| Mode | `weed mini` runs master, volume, filer and S3 gateway in one process (single node, dev-optimised). The image's default command is already `mini -dir=/data`. |
| S3 endpoint and port | The S3 gateway defaults to `8333`. With `-s3.port=9000` the host and Compose-internal ports stay the same as today's `STORAGE_ENDPOINT_URL`/`STORAGE_PUBLIC_ENDPOINT_URL` defaults (`http://localhost:9000`, `http://<service>:9000`). |
| Credentials | `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` create the admin identity at startup (log: `Added admin identity from AWS environment variables`). **Load-bearing:** without them the S3 gateway accepts anonymous reads (see negative controls). |
| CORS | Server-wide, set by `-s3.allowedOrigins` (comma-separated). **Its default is `*`**, so the flag must always be set to the exact frontend origin. It replaces `MINIO_API_CORS_ALLOW_ORIGIN`. |
| Health | `GET /healthz` (also `/status`, `/readyz`) on the S3 port returns `200` once the S3 router is serving. This is a liveness-only signal: the handler is a static 200. The image ships `curl`, `wget` and `nc`, so a Compose `healthcheck` can use `curl -fsS http://127.0.0.1:9000/healthz`. Startup reached healthy in about 1–2 s. |
| Persistent data | All state (volumes, filer metadata, generated SSE KEK, admin state) lives in `/data`. The entrypoint runs as root only to `chown` `/data`, then drops to `seaweed` (uid 1000). An object written before a restart on a named volume was read back unchanged after the restart. |
| Telemetry | `-master.telemetry` defaults to **on** (reports to `telemetry.seaweedfs.com`); disable it with `-master.telemetry=false`. |
| Other listeners | `mini` also opens master (`9333`), volume (`9340`), filer (`8888`, whose default CORS is `*`), Iceberg (`8181`), Lance (`9101`) and gRPC ports. The filer registers an unauthenticated IAM gRPC service. None of these may be published; only the S3 port is. `-admin.ui=false` disables the unauthenticated admin UI routes (`/` on `23646` returns `404`), but the admin server still **listens** in-container on `23646` (HTTP) and `33646` (gRPC, where anyone who can reach it can register a maintenance worker unauthenticated), so both must never be published. `-webdav=false` removes the WebDAV listener (`7333`). There is no MinIO-style console on `9001`. |
| Bucket creation | `-bucket` can pre-create buckets, but it is **not** used: the suite proves the adapter's lazy `ensure_bucket`. `-s3.autoCreateBucket` (default on, admin identities only) did not affect the lazy-bucket test, which first asserts `head_bucket` fails. |

## Integration-suite results

The command below ran against the candidate on 2026-09-29, both with the default
`8333` port and with the recommended `-s3.port=9000` configuration. Every test
**executed**; none was skipped.

```sh
cd backend && STORAGE_ENDPOINT_URL=http://127.0.0.1:<port> STORAGE_BUCKET=ci-storage-tests \
  STORAGE_REGION=us-east-1 STORAGE_ACCESS_KEY_ID=<key> STORAGE_SECRET_ACCESS_KEY=<secret> \
  STORAGE_CORS_ALLOWED_ORIGIN=https://app.example.com \
  uv run pytest -m storage_integration -rA tests/test_storage_integration.py
```

```text
PASSED test_signed_upload_round_trip
PASSED test_private_bucket_denies_unsigned_requests
PASSED test_ensure_bucket_creates_missing_bucket_lazily
PASSED test_head_missing_object_returns_none
PASSED test_delete_missing_object_is_idempotent
PASSED test_browser_put_from_the_authorised_origin_succeeds
PASSED test_browser_preflight_from_a_forbidden_origin_is_refused
PASSED test_server_side_promotion_and_staging_replay_immutability
8 passed
```

No assertion, fixture or test file was changed.

### Negative controls

These runs show that the suite's security assertions depend on the SeaweedFS
configuration above and are not passing by accident:

| Control | Result |
| --- | --- |
| `-s3.allowedOrigins` omitted (default `*`) | `test_browser_preflight_from_a_forbidden_origin_is_refused` **fails** (`https://evil.example.com` echoed). 7 passed, 1 failed. |
| `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` omitted | `test_private_bucket_denies_unsigned_requests` **fails** (unsigned GET is not denied). 7 passed, 1 failed. |

### Additional direct probes (candidate configuration)

| Probe | Result |
| --- | --- |
| Preflight from the allowed origin | `200`, `Access-Control-Allow-Origin: https://app.example.com`, methods `GET, PUT, POST, DELETE, HEAD`, allow-headers `content-type` |
| Preflight from a forbidden origin | `403`, no `Access-Control-*` headers |
| Unsigned bucket list / service list | `403` / `403` |
| Adapter presigned PUT with tampered signature, different key, wrong `Content-Type` | `403` each |
| Adapter presigned GET after expiry | `403` |
| Split endpoints (`public_endpoint_url=http://localhost:…`, API endpoint `127.0.0.1`) | Presigned PUT to the public host `200` |
| SigV4 presign (`signature_version="s3v4"`): valid PUT/GET, host-swapped, tampered, expired | `200`/`200`, `403`, `403`, `403` |

## Compatibility gaps and observations

There is **no blocking gap**: every S3 behaviour the repository relies on is
tested reliably, so P2 can proceed after review. The following observations
need a decision or care in P2/P3. None of them is a SeaweedFS deficiency.

1. **The presigned URLs are SigV2, not SigV4.** With botocore `1.43.66` and the
   unchanged `S3Storage` client configuration (no explicit
   `signature_version`), presigned URLs use SigV2 query authentication
   (`AWSAccessKeyId`/`Signature`/`Expires`) and path-style addressing
   (`<endpoint>/<bucket>/<key>`). This comes from the client, so the MinIO path
   produced the same URLs. The plan's acceptance wording "SigV4 signed
   upload/download" therefore does not describe the current adapter. SeaweedFS
   verifies both SigV2 and SigV4 correctly. Under SigV2 the `Host` header is not
   signed (a host-swapped URL was accepted), whereas SigV4 binds the host (the
   same swap was rejected). SigV2 is also deprecated by AWS and rejected by
   newer S3 regions.

   **Owner decision (2026-09-29): adopt SigV4.** The adapter will set
   `signature_version="s3v4"` explicitly. This is scheduled as checkpoint **P4**
   of this plan, after the migration, so that P1–P3 prove the replacement
   server against unchanged adapter behaviour. Until P4 lands, the acceptance
   evidence reads "signed (SigV2 query-auth) upload/download".
   SeaweedFS already verifies SigV4 presigned URLs correctly (see the direct
   probes above).

   Consequence: SigV4 caps presigned-URL lifetime at 7 days (shorter under
   temporary credentials). This does not affect the template, whose signed URLs
   last 15 minutes by default and at most 1 hour for uploads. Apps that need
   long-lived links usable without logging in (for example, links to files
   inside a shared export) must **not** embed raw storage URLs. The chosen
   pattern for that future need is app-issued share links: an opaque, hashed,
   expiring and revocable token at a public app route, which records an audit
   event and redirects to a fresh short-lived SigV4 URL. That is a new
   unauthenticated, tenant-data-serving endpoint, so it needs its own design and
   human review (authentication and tenant isolation) when an app requires it.
2. **Presigned PUTs do not bind the body size.** A PUT with a different body
   length than the intent's `size_bytes` was accepted. This is the existing
   adapter contract (size is verified server-side on completion and the object
   is promoted to an immutable final key), not a SeaweedFS difference.
3. **The CI can go green on skips.** The module fixture in
   `test_storage_integration.py` `pytest.skip`s when `ensure_bucket` fails, and
   the CORS cases skip without `STORAGE_CORS_ALLOWED_ORIGIN`. The plan's
   acceptance criterion requires CI to fail on service startup rather than
   silently skip. P2 should make the dedicated CI job fail on any skip, for
   example with an opt-in "required" environment flag that turns the skips into
   failures. The assertions themselves stay unchanged.
4. **Health is liveness only.** `/healthz` does not probe the filer or volume
   backend. In practice the first authenticated operation succeeded straight
   after the first `200`. The fail-on-skip change in item 3 is the backstop that
   stops a half-started server passing CI.
5. **The server has no console.** MinIO's console on `9001` (`MINIO_CONSOLE_PORT`,
   the `make dev` banner) has no enabled equivalent. The SeaweedFS admin UI is
   unauthenticated by default and should stay off. P2 should remove the console
   port, its `.env.example` variable and the banner text.
6. **The configuration surface changes.** `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`
   become `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` on the container, and
   `MINIO_API_CORS_ALLOW_ORIGIN` becomes the `-s3.allowedOrigins` flag (Compose
   can interpolate `${STORAGE_CORS_ALLOWED_ORIGIN}` into `command`). The health
   path changes from `/minio/health/live` to `/healthz`. The data volume should
   be renamed (for example `seaweedfs_data`), because SeaweedFS cannot read a
   MinIO volume. Local MinIO objects are disposable dev data and are not
   migrated (the plan excludes data migration).
7. **Application code needs no change.** The AI subsystem's local-storage-seam
   detection (`gcs_managed_url`) classifies endpoints by host shape
   (loopback, private or single-label Compose name), not by the string
   `minio`, so a `seaweedfs:9000` internal endpoint is treated identically.

## MinIO reference inventory

The search command was `grep -rniI minio` (excluding `node_modules`, `.git` and `.venv`).

### Controls local startup (P2 changes)

- `deploy/compose/compose.local.yml`: the `minio` service (image, `command`,
  `MINIO_ROOT_*`, `MINIO_API_CORS_ALLOW_ORIGIN`, ports `9000`/`9001`,
  `minio_data` volume, `/minio/health/live` health check); `depends_on: minio`
  and `STORAGE_ENDPOINT_URL=http://minio:9000` defaults on `api`, `worker` and
  `coordinator`; `minioadmin` `STORAGE_*` defaults; the frontend CSP comment;
  the header comment; the `minio_data` volume declaration.
- `Makefile`: `dev` and `dev-reset` start the `minio` service by name; banner
  text names the MinIO console on `9001`; comments.
- `.env.example`: the storage block comments and `minioadmin` defaults;
  `MINIO_PORT`, `MINIO_CONSOLE_PORT`, `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD`;
  the AI local-development example comment.

### Controls CI (P2 changes)

- `.github/workflows/ci.yml`: the header comment; the `storage-integration` job
  (name, comments, `minioadmin` env, the `docker run` of
  `quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z`, the `/minio/health/live`
  wait loop, the step names).

### Tests (P2 changes)

- `backend/tests/test_deployment_boundaries.py`:
  `test_local_minio_allows_the_configured_browser_origin` asserts the `minio`
  service's `MINIO_API_CORS_ALLOW_ORIGIN`.
- `backend/tests/test_storage_integration.py`: the docstrings and comments, and
  the `minioadmin` credential fallbacks.
- `backend/pyproject.toml`: the `storage_integration` marker description and
  comment.
- These references are generic sample values and comments that need no
  behaviour change. P3 can reword or keep them as examples:
  `tests/conftest.py`, `test_storage_s3.py` (`http://minio.local:9000`),
  `test_config.py`, `test_ai_gcs_managed_url.py`, `test_ai_adapters.py`,
  `test_files.py`, `test_files_db.py`, `frontend/e2e/helpers.ts`.

### Application comments (no behaviour; P3 rewording)

- `backend/app/storage/s3.py`, `backend/app/storage/fake.py`,
  `backend/app/core/config.py` (the `storage_endpoint_url` description),
  `backend/app/ai/{runtime,service,transfer_orchestrator}.py`,
  `backend/app/ai/providers/{gcs_managed_url,anthropic_upload}.py`,
  `backend/app/ai/README.md`, `frontend/src/lib/upload.ts`.

### Runbooks, guides and decisions (P2/P3 changes)

- `README.md` (the local stack, the console URL, the command table),
  `ARCHITECTURE.md` (the storage section), `SECURITY.md` (the firewall note),
  `docs/operations.md` (the storage CORS paragraph), and
  `docs/release-checklist.md` (the storage integration items).
- ADR-0006, ADR-0008 and ADR-0014 (accepted storage and local-development
  decisions) and ADR-0015 (a cross-reference to the MinIO tests).

### Historical (retain as history)

- `TEMPLATE_V0_1_SCOPE.md` to `TEMPLATE_V0_6_SCOPE.md`, `IMPLEMENTATION_GUIDE.md`
  and blueprint §17 "MinIO local support" (the original release contracts and
  initial strategy), and completed plans such as
  `plans/2026-08-27-critical-job-reliability-closure.md`.
- `plans/SINGLE_VPS_DEMO_DEPLOYMENT_PLAN.md` (`Status: Proposed`) names MinIO
  as a future production demo service. That is a production-profile decision,
  outside this plan's local/CI boundary, and is left for the owner.

## Upgrade procedure (for P2 to adopt)

1. Pick the new upstream release tag and read its release notes for changes to
   S3, CORS, `mini` flags or health endpoints.
2. Resolve the multi-arch index digest on Docker Hub and confirm GHCR serves the
   same digest. Confirm that the OCI `revision` label equals the tag's commit
   and that the licence is unchanged.
3. Update the digest in Compose and CI together. Run
   `uv run pytest -m storage_integration` with `STORAGE_CORS_ALLOWED_ORIGIN`
   set against the new image. Every test must execute and pass, and the two
   negative controls must still fail.
4. Record the new version and digest in the evidence and ask for human review
   (infrastructure change).
