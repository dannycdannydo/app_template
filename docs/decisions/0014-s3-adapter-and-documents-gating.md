# ADR 0014: S3 Adapter as the First Storage Implementation and `documents.*` Gating for Files and Jobs

Status: Accepted

## Context

ADR-0006 defined a provider-neutral `ObjectStorage` interface with adapters for S3-compatible storage, Azure Blob and GCS, but deliberately left the first implementation and the endpoint permission model open. The v0.5 release (files and jobs) had to answer two questions:

1. Which provider adapter ships first, and how does the provider SDK stay behind the interface?
2. Which permission codes gate the new files and jobs endpoints — new `files.*` / `jobs.*` codes, or the existing `documents.*` set seeded in the v0.2 permission plane?

## Options considered

### 1. First storage adapter

- **S3-compatible adapter with boto3 (adopted)**: one S3-compatible adapter covers both deployment profiles' storage needs today (hybrid VPS can run MinIO or any S3 service; managed Azure gains Blob later). The SDK import is confined to `app/storage/s3.py`; `grep -rn "boto3" backend/app | grep -v "app/storage"` stays empty (blueprint §17 decision, enforced by test). MinIO runs in the local Compose stack so `make dev` exercises the real adapter.
- **Azure Blob first**: only serves the managed-Azure profile; the hybrid profile would still need S3. The blueprint's initial-implementation strategy already names S3 + MinIO first.
- **Both adapters at once**: violates the rule of three — no second consumer yet, and a second adapter is real cost before any application needs it.

### 2. Permission gating

- **Reuse `documents.*` (adopted)**: the v0.2 permission plane already seeds `documents.read` / `documents.upload` / `documents.delete`. `documents.upload` gates upload intent and completion, `documents.read` gates file list/detail/download-url and the job endpoints, `documents.delete` gates file deletion. Files and jobs behave exactly like every other org-scoped resource, with no new roles and no permission-model change.
- **New `files.*` and `jobs.*` codes**: `jobs.*` has exactly one producer today (the file module); a generic job permission would be speculative (rule of three — the scope defers it until a second producer appears). New `files.*` codes would duplicate the existing `documents.*` semantics already seeded in the role bundles.
- **Storage-specific roles**: contradicts the org role model — storage access is a per-endpoint capability, not a separate plane.

## Decision

**Ship the boto3 S3-compatible adapter (`S3Storage`) as the first and only storage adapter implementation of the ADR-0006 contract**, wired from settings (`STORAGE_PROVIDER=s3`), validated against MinIO locally and in CI; Azure Blob and GCS adapters remain deferred until a deployment or consumer requires them. `STORAGE_PROVIDER=fake` selects the in-memory `FakeObjectStorage` for the test suite and is rejected in production.

**Gate every files and jobs endpoint with the existing `documents.*` permission codes** through the same `require_permission` dependency as all org-scoped routes: `documents.upload` for `POST /api/v1/files` and `POST /api/v1/files/{file_id}/complete`, `documents.read` for `GET /api/v1/files`, `GET /api/v1/files/{file_id}`, `GET /api/v1/files/{file_id}/download-url`, `GET /api/v1/jobs` and `GET /api/v1/jobs/{job_id}`, and `documents.delete` for `DELETE /api/v1/files/{file_id}`. No new permission codes, roles or authorisation planes are introduced; a generic `jobs.*` permission waits for a second job producer (rule of three).

## Consequences

- The template runs on any S3-compatible service (MinIO locally, AWS S3 or compatible providers in production) through one adapter; the interface keeps the Azure/GCS door open without paying its cost now.
- Storage and job endpoints are tenant-scoped exactly like `records`: cross-organisation ids resolve to 404, viewer writes are denied, and the whole surface is covered by the mandatory security suite.
- The files module is the sole job producer; `GET /api/v1/jobs*` is gated by `documents.read` because reading job state is part of managing a file upload. When a second producer lands, a `jobs.*` code set and a migration of the gate can be added without an API break (the endpoints do not change, only the dependency).
- `dramatiq` (ADR-0004) and `boto3` are the only new runtime dependencies and both are justified: Dramatiq is the durable job pipeline, boto3 is the S3 adapter SDK confined to `app/storage/s3.py` (blueprint §32 dependency rules). The test-only `moto` library was evaluated for the S3 unit tests and rejected because moto 5.x does not intercept boto3 clients that pass an explicit `endpoint_url` (which this adapter always does); the adapter's logic is unit-tested with mocked clients instead, and the real provider behaviour is proven by the MinIO-backed `storage_integration` tests.
- Blueprint §17, §18, §30 and §31 were amended with the interface implementation, the durable-record service, the shipped file-security controls and the expanded security-test matrix (see the v0.5 scope §6.7).

---
