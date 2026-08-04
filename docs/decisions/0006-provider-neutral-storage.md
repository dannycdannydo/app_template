# ADR 0006: Provider-Neutral Storage

Status: Accepted

## Context

Applications built from this template need object storage (user uploads, exports, documents), but the target deployment profiles (hybrid VPS and managed Azure) imply different object-storage backends (S3-compatible, Azure Blob). Storing files behind a concrete SDK would couple the template to one provider.

## Options considered

- **Provider-neutral interface with adapters**: define a storage interface in the template and implement adapters per provider (S3-compatible, Azure Blob). Applications depend on the interface.
- **Single provider SDK (S3 only)**: simplest, but Azure Blob deployments would need a different path, splitting applications by deployment target.
- **Storage abstraction library (e.g. libcloud-style)**: adds a dependency that the template would not control; the template's own interface is small and explicit.

## Decision

Use a **provider-neutral storage interface** in the template (`storage/`), with adapters for S3-compatible storage and Azure Blob Storage. Signed upload URLs are part of the interface contract (v0.4). All object storage access in applications goes through the interface; provider SDKs stay behind adapters.

## Consequences

- The same application code runs on the hybrid VPS profile (S3-compatible, e.g. MinIO) and the managed Azure profile (Blob Storage).
- Adapter work is duplicated only at the boundary, and only when a new provider is genuinely needed.
- Files are treated as untrusted input at the storage boundary (see `SECURITY.md`).

---
