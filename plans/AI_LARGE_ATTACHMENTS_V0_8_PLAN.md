# Template v0.8 — Large AI Attachments and Reference Transfer Modes — Plan

Status: Superseded as an execution contract by `TEMPLATE_V0_8_SCOPE.md`; retained as the design source

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
- bounded inline attachments (5 MB per file and 10 MB combined in v0.7); v0.8
  tightens transfer selection to a 5,000,000-byte aggregate raw-attachment
  threshold so larger inputs use a non-inline mode;
- server-side storage-reference resolution and SHA-256 digests;
- references rather than bytes in Dramatiq messages and persistence;
- keep versus temporary scratch-file lifecycle;
- explicit provider-region configuration and no implicit cross-region routing.

## 2. In scope

- Provider-neutral transfer policy: `inline`, `provider_upload`,
  `managed_signed_url` or `storage_reference`, with `inline` remaining the
  default for attachments whose aggregate raw bytes do not exceed 5,000,000.
- Provider-hosted temporary uploads where an adapter offers an authenticated
  file API, including provider file identifier, expiry and cleanup semantics.
- Service-minted, read-only, short-lived signed URLs for feature-owned files
  already retained in the application's private S3-compatible storage, when
  the selected provider supports URL ingestion.
- A user-provisioned private Google Cloud Storage staging bucket for Vertex
  `gs://` references. The deployer configures bucket lifecycle deletion in the
  Google Cloud console; the application does not create the bucket or run a
  scheduled GCS cleanup job.
- Files above the v0.8 5,000,000-byte inline threshold, bounded by reviewed template and
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
| Caller-selected transfer modes | source lifecycle, policy, provider capability and deployment configuration determine the mode |
| Public buckets or making private source objects public | prohibited |
| Caller-supplied HTTP(S) URLs | prohibited; v0.8 accepts no arbitrary external fetch target |
| Application-created Vertex buckets or lifecycle policies | deployer provisions the bucket, IAM and lifecycle rule outside the application |
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
| `inline` | transient or retained inputs with aggregate raw attachment bytes at or below 5,000,000 | bytes live only for the provider call; base64/JSON expansion is accounted for separately in request-body and gateway configuration |
| `provider_upload` | input above 5,000,000 bytes whose source is transient and whose provider has a reviewed file API | adapter uploads with server credentials; record opaque id, digest and expiry; delete when supported or rely on a documented hard provider expiry |
| `managed_signed_url` | input above 5,000,000 bytes already retained in private application S3-compatible storage, when the provider supports URL ingestion | `AIService` mints an exact-object, read-only, short-lived URL only after tenant/source validation; the full URL is never returned, persisted, audited or logged |
| `storage_reference` | Vertex input above 5,000,000 bytes staged in the configured private GCS bucket and passed as `gs://...` | no public ACL; least-privilege identity; project/bucket/location policy validated before dispatch; deployer-owned lifecycle deletion is the cleanup backstop |

The service fails before dispatch when no permitted mode can carry the input.
It does not silently downgrade privacy controls, upload to another region, or
accept a URL supplied by a caller. A managed signed URL is a temporary bearer
capability generated by the service for one already-authorised immutable
object; it is not an application-facing input.

### 4.3 Durable state and idempotency

Introduce a durable provider-file/reference record only if implementation
needs state beyond the existing `ai_requests` / `ai_outputs` rows. The record
must be organisation-scoped and contain only:

- provider and opaque external identifier/URI;
- source storage reference and SHA-256 digest;
- transfer mode, status and idempotency key;
- created, expiry, deleted and last-used timestamps;
- safe provider error code and bounded non-sensitive metadata.

It must never contain source bytes, credentials, a managed signed URL or its
query string, provider headers or raw responses. Retried jobs reuse a live
reference only
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
  file references and retries cleanup with bounded backoff.
- Managed signed URLs expire without a cleanup record. The source object keeps
  its feature-owned lifecycle and is never deleted by AI cleanup.
- Vertex staging objects use a deployer-configured GCS lifecycle rule with an
  age of one day as the authoritative cleanup backstop. Lifecycle execution is
  asynchronous, so this is eligibility after one day rather than an exact
  24-hour deletion guarantee. The application may attempt terminal deletion
  but runs no scheduled GCS cleanup or reconciliation job.
- Deployers wanting Files-API-like ephemeral Vertex staging disable GCS soft
  delete, object versioning and conflicting holds/retention policies, or accept
  the longer recovery/retention behavior those controls introduce.
- Audit records identify the provider, transfer mode, reference record and
  outcome without recording content or bearer credentials.

### 4.5 Provider paths to evaluate

Provider behavior and limits change. Re-verify official documentation and
non-production contract behavior immediately before implementation.

| Provider | Candidate v0.8 path | Required decision |
| --- | --- | --- |
| OpenAI | provider file id for transient sources; managed S3 signed URL for retained sources when the deployed model supports URL file input | retention/deletion API, URL-fetch behavior, regional availability and redaction |
| Azure OpenAI | no v0.8 non-inline path unless parity is separately reviewed | continue fail-fast rejection for this release |
| Vertex AI | upload to a user-provisioned private GCS staging bucket and pass a `gs://` reference | validate bucket location, IAM, lifecycle prerequisite and retry semantics; never use the Gemini Developer Files API |
| Anthropic | provider file id for transient sources; managed S3 signed URL for retained sources | validate beta Files API cleanup, URL-fetch behavior, inference geography and redaction |
| Local | implementation-specific private object/file bridge | remain disabled unless a reviewed adapter capability exists |
| DeepSeek | none unless official document input support exists | continue fail-fast rejection |

## 5. Proposed work units

### 5.1 Contract, ADR and policy

- Amend the v0.8 release scope and ADR-0017 with transfer modes, ownership,
  lifecycle and threat model.
- Decide organisation policy fields, maximum supported sizes and whether
  provider-hosted reuse is allowed within retries of one logical execution.
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

- Implement each approved provider mode behind its adapter, including
  just-in-time signed-URL minting through the storage boundary rather than in
  feature code.
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
2. Inline is selected only when aggregate raw attachment bytes are at or below
   5,000,000. Larger transient inputs prefer a reviewed provider Files API;
   larger retained S3 inputs prefer a managed signed URL where supported;
   Vertex uses its configured private GCS staging bucket.
3. Oversized or unsupported inputs fail before inference without generating a
   public object or accepting a caller-supplied URL.
4. Broker messages, database rows, logs, Sentry and audit metadata contain no
   attachment bytes, credentials or bearer URL query strings.
5. Retried work is idempotent and cannot create unbounded duplicate uploads or
   duplicate cost/output records.
6. Provider-upload cleanup handles success, permanent failure, timeout, worker
   crash and deletion failure; managed URLs expire; Vertex documents and
   requires the deployer-owned one-day lifecycle prerequisite without an
   application GCS cleanup scheduler.
7. Cross-organisation reference use is denied and covered by integration
   tests. Any protected routes join the mandatory security matrix.
8. Provider contract tests cover upload/use/delete or expiry behavior against
   dedicated non-production accounts and skip cleanly without credentials.
9. Region/location compatibility is validated and no path performs automatic
   cross-region transfer or inference fallback.
10. `make check`, migration validation and the relevant provider contract
    suite are green, followed by architecture and required human review.

## 7. Resolved implementation decisions

1. The inline threshold is 5,000,000 aggregate raw attachment bytes. It is not
   the large-file ceiling; v0.8 retains its reviewed PDF ceiling and applies
   lower provider/model limits.
2. Transient sources above the threshold prefer a provider Files API. Retained
   S3-compatible source objects prefer a service-minted signed URL where the
   provider supports URL ingestion. Callers never supply URLs.
3. Vertex remains Vertex AI only. Its large-file path is a private `gs://`
   object in a deployer-provisioned, configured GCS staging bucket; Gemini
   Developer API and its Files API remain excluded.
4. The deployer configures a GCS delete lifecycle with `age = 1` in Google
   Cloud. The application neither creates/configures the bucket nor runs a GCS
   cleanup scheduler. Lifecycle deletion is asynchronous and soft-delete,
   versioning, holds and retention settings remain the deployer's explicit
   responsibility.
5. Provider-hosted identifiers are reusable only within retries of one logical
   execution. Organisation policy remains explicit and typed rather than an
   opaque policy blob.

Do not start v0.8 implementation until v0.7 §6.5 retention/persistence and
§6.6 reference-only job execution are implemented, reviewed and green.
