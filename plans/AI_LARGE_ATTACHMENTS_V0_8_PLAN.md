# Template v0.8 — Large AI Attachments and Reference Transfer Modes — Plan

Status: Proposed follow-up to v0.7; not yet an approved release contract

Depends on: `TEMPLATE_V0_7_SCOPE.md` (§6.2–§6.7), ADR-0017, ADR-0018,
`Internal_Custom_Application_Starter_Architecture_v2.md` (BP §17, §18, §23,
§27–§28, §31–§33)

## 1. Goal

Extend the bounded v0.7 attachment seam for files that should not be copied
inline into a provider request. v0.8 adds explicit, policy-controlled transfer
modes for large files and reusable provider references without changing the
application-facing rule: feature services supply a private storage reference
to `AIService` and never call provider file APIs directly.

v0.7 remains the required foundation. It owns:

- provider-neutral attachments and the `documents` capability;
- 5 MB per-file and 10 MB combined inline limits;
- server-side storage-reference resolution and SHA-256 digests;
- references rather than bytes in Dramatiq messages and persistence;
- keep versus temporary scratch-file lifecycle;
- explicit provider-region configuration and no implicit cross-region routing.

## 2. In scope

- Provider-neutral transfer policy: `inline`, `provider_upload` or
  `storage_reference`, with `inline` remaining the safe default.
- Provider-hosted temporary uploads where an adapter offers an authenticated
  file API, including provider file identifier, expiry and cleanup semantics.
- Direct cloud-object references where the provider can use workload identity
  or narrowly scoped IAM, principally Vertex `gs://` references.
- Provider-supported URL input only for explicitly non-sensitive flows under
  organisation policy; private signed URLs remain disabled by default.
- Files above the v0.7 inline ceiling, bounded by reviewed template and
  provider-specific limits.
- Durable upload/reference state needed for retries, idempotency, expiry and
  cleanup without storing file bytes in the database or job broker.
- Same-region validation between configured inference endpoints and any
  provider/cloud file staging location where the provider supports it.
- Metrics, audit events, runbooks and contract tests for every enabled transfer
  mode.

## 3. Out of scope

| Capability | Boundary |
| --- | --- |
| Core inline attachment support | ships in v0.7 |
| Automatic transfer-mode selection based only on provider convenience | policy must explicitly permit every non-inline mode |
| Public buckets or making private source objects public | prohibited |
| Private signed URLs as a template default | prohibited; bearer-token disclosure and expiry make them an explicit exception |
| Cross-region upload or inference fallback | prohibited |
| OCR, parsing, chunking, embeddings or RAG | separate domain/retrieval capabilities |
| Permanent provider-side document library | first product requiring reusable provider files must define ownership and deletion obligations |
| Browser-to-provider upload or provider credentials in the frontend | prohibited |

## 4. Design constraints

### 4.1 Application and provider boundaries

`AIRequest` continues to carry a private `storage_reference`. `AIService`
authorises and resolves the object, validates organisation ownership, size,
MIME type and digest, then selects a transfer mode allowed by all of:

1. task policy;
2. organisation AI policy;
3. routed model/provider capability;
4. deployment configuration and regional constraints.

Only adapter code translates the resulting provider-neutral transfer request
into provider APIs. Features never receive provider file identifiers and never
construct provider URLs.

### 4.2 Transfer modes

| Mode | Intended use | Security/lifecycle rule |
| --- | --- | --- |
| `inline` | v0.7-sized one-shot inputs | default; bytes live only for the provider call |
| `provider_upload` | large one-shot inputs accepted by a provider file API | adapter uploads with server credentials; record opaque id, digest and expiry; delete when supported or rely on a documented hard provider expiry |
| `storage_reference` | cloud objects the provider can read using IAM, such as same-project `gs://` objects | no public ACL; least-privilege identity; region and project/bucket policy validated before dispatch |
| URL input | explicitly non-sensitive objects accepted by a provider | opt-in exception only; never mint a private signed URL by default |

The service fails before dispatch when no permitted mode can carry the input.
It does not silently downgrade privacy controls, upload to another region, or
fall back from an authenticated reference to a public URL.

### 4.3 Durable state and idempotency

Introduce a durable provider-file/reference record only if implementation
needs state beyond the existing `ai_requests` / `ai_outputs` rows. The record
must be organisation-scoped and contain only:

- provider and opaque external identifier/URI;
- source storage reference and SHA-256 digest;
- transfer mode, status and idempotency key;
- created, expiry, deleted and last-used timestamps;
- safe provider error code and bounded non-sensitive metadata.

It must never contain source bytes, credentials, signed URL query strings,
provider headers or raw responses. Retried jobs reuse a live reference only
when provider, digest, organisation and regional policy still match; otherwise
they create a new idempotent upload.

Any database change requires an Alembic migration. The migration, tenant
isolation, secret/IAM behavior and provider data handling require recorded
human review before application.

### 4.4 Lifecycle

- Keep-flow source objects remain owned by the feature. Deleting an AI-side
  provider copy never deletes the feature object.
- Temporary source objects remain governed by the v0.7 scratch retention job.
- Provider uploads use the shortest supported retention and are deleted after
  terminal success/failure when the provider supports deletion.
- A reconciliation job detects expired, orphaned or deletion-failed provider
  references and retries cleanup with bounded backoff.
- Audit records identify the provider, transfer mode, reference record and
  outcome without recording content or bearer credentials.

### 4.5 Provider paths to evaluate

Provider behavior and limits change. Re-verify official documentation and
non-production contract behavior immediately before implementation.

| Provider | Candidate v0.8 path | Required decision |
| --- | --- | --- |
| OpenAI / Azure OpenAI | provider file id or approved URL input on Responses-capable deployments | retention/deletion API, regional availability and Azure parity |
| Vertex AI | managed file upload, registered GCS object or direct same-project `gs://` reference | select one default large-file path; validate location, IAM, expiry and retry semantics |
| Anthropic | provider-supported URL/file mechanisms available to the configured account | determine whether the security and regional contract is sufficient |
| Local | implementation-specific private object/file bridge | remain disabled unless a reviewed adapter capability exists |
| DeepSeek | none unless official document input support exists | continue fail-fast rejection |

## 5. Proposed work units

### 5.1 Contract, ADR and policy

- Amend the v0.8 release scope and ADR-0017 with transfer modes, ownership,
  lifecycle and threat model.
- Decide organisation policy fields, maximum supported sizes and whether
  provider-hosted reuse is allowed at all.
- Define provider/reference capability metadata without leaking provider
  concepts into feature modules.

### 5.2 Persistence and lifecycle

- Add reviewed migration/models/queries/services for durable external file
  references if required.
- Implement idempotent upload creation, expiry checks, terminal cleanup and
  reconciliation.
- Prove organisation isolation and that feature-owned source objects are never
  deleted by AI cleanup.

### 5.3 Adapter implementations

- Implement each approved provider mode behind its adapter.
- Validate size, MIME, region/location, project/account and expiry before use.
- Normalise upload, reference-expiry and deletion failures into safe AI errors.

### 5.4 Jobs, observability and operations

- Keep all job messages reference-only and bound worker memory/concurrency.
- Add low-cardinality metrics for transfer mode, upload outcome, cleanup
  backlog and reference expiry.
- Document provider-side retention, incident cleanup, IAM, regional guarantees
  and how to disable a compromised transfer mode.

## 6. Acceptance criteria

1. A feature still supplies only a private storage reference and task name;
   provider file APIs and identifiers remain internal to the AI layer.
2. Inline remains the default. Every other transfer mode requires explicit
   task, organisation and deployment permission.
3. Oversized or unsupported inputs fail before inference without generating a
   public object or signed URL.
4. Broker messages, database rows, logs, Sentry and audit metadata contain no
   attachment bytes, credentials or bearer URL query strings.
5. Retried work is idempotent and cannot create unbounded duplicate uploads or
   duplicate cost/output records.
6. Cleanup handles success, permanent failure, timeout, worker crash and
   provider deletion failure; reconciliation exposes orphaned references.
7. Cross-organisation reference use is denied and covered by integration
   tests. Any protected routes join the mandatory security matrix.
8. Provider contract tests cover upload/use/delete or expiry behavior against
   dedicated non-production accounts and skip cleanly without credentials.
9. Region/location compatibility is validated and no path performs automatic
   cross-region transfer or inference fallback.
10. `make check`, migration validation and the relevant provider contract
    suite are green, followed by architecture and required human review.

## 7. Decisions required before implementation

1. Which single large-file path should be the template default for Vertex:
   managed upload or same-project GCS reference?
2. Should provider-hosted identifiers ever be reusable across AI requests, or
   only within retries of one logical request?
3. Which MIME types and maximum sizes does the template support independently
   of higher provider ceilings?
4. Are URL inputs useful enough to retain as an explicit exception, or should
   v0.8 exclude them entirely?
5. Does organisation policy need dedicated columns or a bounded, typed policy
   object, and what migration/compatibility consequences follow?

Do not start v0.8 implementation until v0.7 §6.5 retention/persistence and
§6.6 reference-only job execution are implemented, reviewed and green.
