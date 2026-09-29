# ADR 0014: S3 Adapter as the First Storage Implementation and `documents.*` Gating for Files and Jobs

Status: Accepted (amended 2026-09-29: local/CI test service changed from MinIO to SeaweedFS, production stays provider-neutral, `plans/01-seaweedfs-storage-testing.md` P3; presigned URLs moved to SigV4, P4)

## Context

ADR-0006 defined a provider-neutral `ObjectStorage` interface with adapters for S3-compatible storage, Azure Blob and GCS, but deliberately left the first implementation and the endpoint permission model open. The v0.5 release (files and jobs) had to answer two questions:

1. Which provider adapter ships first, and how does the provider SDK stay behind the interface?
2. Which permission codes gate the new files and jobs endpoints — new `files.*` / `jobs.*` codes, or the existing `documents.*` set seeded in the v0.2 permission plane?

## Options considered

### 1. First storage adapter

- **S3-compatible adapter with boto3 (adopted)**: one S3-compatible adapter covers both deployment profiles' storage needs today (hybrid VPS can run any S3-compatible service; managed Azure gains Blob later). The SDK import is confined to `app/storage/s3.py`; `grep -rn "boto3" backend/app | grep -v "app/storage"` stays empty (blueprint §17 decision, enforced by test). SeaweedFS runs in the local Compose stack so `make dev` exercises the real adapter.
- **Azure Blob first**: only serves the managed-Azure profile; the hybrid profile would still need S3. The blueprint's initial-implementation strategy already names an S3-compatible adapter first.
- **Both adapters at once**: violates the rule of three — no second consumer yet, and a second adapter is real cost before any application needs it.

### 2. Permission gating

- **Reuse `documents.*` (adopted)**: the v0.2 permission plane already seeds `documents.read` / `documents.upload` / `documents.delete`. `documents.upload` gates upload intent and completion, `documents.read` gates file list/detail/download-url and the job endpoints, `documents.delete` gates file deletion. Files and jobs behave exactly like every other org-scoped resource, with no new roles and no permission-model change.
- **New `files.*` and `jobs.*` codes**: `jobs.*` has exactly one producer today (the file module); a generic job permission would be speculative (rule of three — the scope defers it until a second producer appears). New `files.*` codes would duplicate the existing `documents.*` semantics already seeded in the role bundles.
- **Storage-specific roles**: contradicts the org role model — storage access is a per-endpoint capability, not a separate plane.

## Decision

**Ship the boto3 S3-compatible adapter (`S3Storage`) as the first and only storage adapter implementation of the ADR-0006 contract**, wired from settings (`STORAGE_PROVIDER=s3`), validated against a live local/CI S3-compatible service (SeaweedFS); Azure Blob and GCS adapters remain deferred until a deployment or consumer requires them. `STORAGE_PROVIDER=fake` selects the in-memory `FakeObjectStorage` for the test suite and is rejected in production.

**Gate every files and jobs endpoint with the existing `documents.*` permission codes** through the same `require_permission` dependency as all org-scoped routes: `documents.upload` for `POST /api/v1/files` and `POST /api/v1/files/{file_id}/complete`, `documents.read` for `GET /api/v1/files`, `GET /api/v1/files/{file_id}`, `GET /api/v1/files/{file_id}/download-url`, `GET /api/v1/jobs` and `GET /api/v1/jobs/{job_id}`, and `documents.delete` for `DELETE /api/v1/files/{file_id}`. No new permission codes, roles or authorisation planes are introduced; a generic `jobs.*` permission waits for a second job producer (rule of three).

### Amendment (2026-09-29: local/CI test service is SeaweedFS, not MinIO)

- **MinIO was the original local/CI test service.** When this ADR was accepted
  in v0.5, the adapter was validated against MinIO locally and in CI; the
  Decision wording above now names SeaweedFS because that is the current state,
  not because the v0.5 adapter was originally proven against it.
- **The pinned SeaweedFS service replaced it.** The unavailable MinIO community
  image was retired; a pinned SeaweedFS artifact now runs the local Compose
  stack and the CI storage-integration job, exercising the unchanged
  `S3Storage` behavior (`plans/01-seaweedfs-storage-testing.md` P2). The
  `storage_integration` assertions were not weakened.
- **SeaweedFS is a local/CI test service only.** It is not a required
  production storage provider. Production remains provider-neutral behind the
  `S3Storage` adapter and `STORAGE_PROVIDER=s3`, and may run against any
  S3-compatible service.

### Amendment (2026-09-29: presigned URLs use SigV4)

- **`S3Storage` signs presigned URLs with SigV4.** Both boto3 clients
  (the data client and the public-endpoint pre-signing client) set
  `signature_version="s3v4"` explicitly, so presigned PUT/GET URLs use
  `AWS4-HMAC-SHA256` query authentication. Without it botocore presigned S3
  URLs with the deprecated SigV2 scheme (`plans/01-seaweedfs-storage-testing.md`
  finding 1). SigV4 signs the request host (`host` appears in
  `X-Amz-SignedHeaders`), so a URL replayed against a different host is refused;
  SigV2 did not bind the host.
- **An empty `STORAGE_REGION` resolves to `us-east-1` in the adapter.** SigV4
  signs the region into `X-Amz-Credential`, so the credential scope must be
  deterministic rather than left to botocore's ambient
  `AWS_REGION`/`AWS_DEFAULT_REGION`/profile resolution. An empty `region` is now
  resolved to `us-east-1` explicitly, matching the `storage_region` setting's own
  documented fallback (`app/core/config.py`) and the bucket-creation behaviour
  that already treated empty as `us-east-1`. This is the only adapter behaviour
  change beyond `signature_version`; no `STORAGE_*` setting, TTL or object-key
  layout changed. **Operator consequence:** a deployment whose bucket is outside
  `us-east-1` must set `STORAGE_REGION` explicitly — leaving it empty and relying
  on ambient AWS configuration now produces a URL signed for the wrong region,
  which S3 rejects.
- **7-day presign cap.** SigV4 caps a presigned URL's lifetime at 7 days
  (shorter under temporary credentials). The template's signed URLs are minutes
  — 15 minutes by default, at most 1 hour for the browser upload capability
  (`storage_upload_url_ttl_seconds`, `le=3600`), and 1,800 s for AI managed
  URLs — so the cap is never reached, and browser uploads still bind the
  declared `Content-Type`.
- **Long-lived links use app-issued share links, not raw storage URLs.** A link
  that must work without a logged-in session (for example a file inside a
  shared export) must not embed a storage URL. The chosen pattern is an opaque,
  hashed, expiring and revocable token at a public app route that records an
  audit event and redirects to a fresh short-lived SigV4 URL. That is a new
  unauthenticated, tenant-data-serving endpoint, so it needs its own design and
  human review (authentication and tenant isolation) when an app requires it.

## Consequences

- The template runs on any S3-compatible service (SeaweedFS locally, AWS S3 or compatible providers in production) through one adapter; the interface keeps the Azure/GCS door open without paying its cost now.
- Storage and job endpoints are tenant-scoped exactly like `records`: cross-organisation ids resolve to 404, viewer writes are denied, and the whole surface is covered by the mandatory security suite.
- The files module is the sole job producer; `GET /api/v1/jobs*` is gated by `documents.read` because reading job state is part of managing a file upload. When a second producer lands, a `jobs.*` code set and a migration of the gate can be added without an API break (the endpoints do not change, only the dependency).
- `dramatiq` (ADR-0004) and `boto3` are the only new runtime dependencies and both are justified: Dramatiq is the durable job pipeline, boto3 is the S3 adapter SDK confined to `app/storage/s3.py` (blueprint §32 dependency rules). The test-only `moto` library was evaluated for the S3 unit tests and rejected because moto 5.x does not intercept boto3 clients that pass an explicit `endpoint_url` (which this adapter always does); the adapter's logic is unit-tested with mocked clients instead, and the real provider behaviour is proven by the SeaweedFS-backed `storage_integration` tests.
- Blueprint §17, §18, §30 and §31 were amended with the interface implementation, the durable-record service, the shipped file-security controls and the expanded security-test matrix (see the v0.5 scope §6.7).

---
