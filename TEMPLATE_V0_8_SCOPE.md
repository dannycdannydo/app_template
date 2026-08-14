# Template v0.8 — Large AI Attachments and Reference Transfer Modes — Scope & Progress Log

## Relationship to other documents

- `Internal_Custom_Application_Starter_Architecture_v2.md` remains the long-term design standard. v0.8 amends its v0.7 AI attachment boundary; it does not replace the storage, jobs, tenancy, security or adapter rules.
- `TEMPLATE_V0_7_SCOPE.md` is the completed foundation: private storage references, bounded inline attachments, durable AI jobs, organisation policy, retention and provider-region configuration already exist.
- `plans/AI_LARGE_ATTACHMENTS_V0_8_PLAN.md` is the design source that proposed this follow-up. This file resolves its open decisions and is the executable release contract.
- `IMPLEMENTATION_GUIDE.md` defines the original v0.1–v0.6 foundation. Like v0.7, v0.8 is a supplementary post-foundation release.

---

# 1. Goal of v0.8

Allow `AIService` to process a deliberately bounded class of files that are too
large for v0.7's inline path without exposing provider concepts to feature
modules. The service selects a policy-approved transfer mode, streams or stages
the private object with bounded memory, records only safe durable reference
metadata, and applies the mode-specific expiry, deletion or deployer-owned
lifecycle contract without deleting the feature-owned source.

---

# 2. In Scope

## 2.1 Fixed release decisions

The open decisions in the source plan are resolved for this release:

1. **Vertex default:** a private, same-region Google Cloud Storage staging
   object referenced as `gs://...` is the only Vertex large-file path. The
   deployer provisions the bucket, IAM and lifecycle policy; the application
   does not create/configure the bucket or use the Gemini Developer Files API.
2. **Reuse boundary:** a provider-side reference may be reused only by retries
   of one logical AI execution. Reuse across distinct AI requests is prohibited.
3. **Inline threshold and template ceiling:** inline is eligible only when the
   aggregate raw attachment bytes do not exceed **5,000,000 bytes**. The
   non-inline path accepts exactly one `application/pdf` above that threshold
   and no larger than **50,000,000 bytes**. Provider/model ceilings may be lower
   and always win. Base64 and JSON expansion is not counted as raw attachment
   bytes and must be allowed for separately by HTTP/gateway body limits.
4. **Managed URL boundary:** a retained feature-owned object in private
   S3-compatible storage may use a service-minted, exact-object, read-only,
   short-lived signed URL when the provider supports URL ingestion. The URL is
   created just before dispatch and never returned to the caller, persisted,
   audited or logged. Caller-supplied HTTP(S) URLs remain excluded.
5. **Organisation policy:** add explicit typed columns to
   `organisation_ai_settings`; do not add an opaque policy blob. Bounded arrays
   may use JSONB consistently with the existing provider/model allowlists.
6. **Provider matrix:** OpenAI and Anthropic implement `provider_upload` for
   transient sources and `managed_signed_url` for retained S3-compatible
   sources where the deployed provider/model supports URL ingestion. Vertex
   implements `storage_reference` through its configured GCS staging bucket.
   Azure OpenAI, DeepSeek and local adapters fail closed for non-inline files
   in v0.8. Azure file input remains deferred until its `user_data`, expiry,
   URL-fetch and regional behavior reaches the same reviewed contract.

These defaults deliberately choose the common 50 MB PDF envelope documented
for Vertex rather than inheriting the much higher upload quotas exposed by
OpenAI or Anthropic. Provider capabilities and limits must be re-verified from
official documentation during each adapter work unit:

- [OpenAI Files API](https://platform.openai.com/docs/api-reference/files)
- [OpenAI file inputs](https://platform.openai.com/docs/guides/pdf-files)
- [Anthropic Files API](https://platform.claude.com/docs/en/build-with-claude/files)
- [Anthropic PDF support](https://platform.claude.com/docs/en/build-with-claude/pdf-support)
- [Vertex AI document understanding](https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/document-understanding)
- [Azure OpenAI Responses file input](https://learn.microsoft.com/en-us/azure/ai-services/openai/quickstart?pivots=rest-api&tabs=command-line%2Ckeyless%2Ctypescript-keyless%2Centra)
- [Amazon S3 presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)
- [Google Cloud Object Lifecycle Management](https://cloud.google.com/storage/docs/lifecycle)

## 2.2 Transfer contract and policy

- Introduce provider-neutral transfer modes: `inline`, `provider_upload`,
  `managed_signed_url` and `storage_reference`. `inline` remains the default
  but is eligible only through the 5,000,000-byte aggregate raw threshold.
- A non-inline mode is eligible only when the source lifecycle, task
  definition, organisation policy, model/provider capability and deployment
  configuration all allow it.
- Extend task/model registries with explicit supported transfer modes,
  per-mode MIME types and byte ceilings. Startup/CI validation rejects
  inconsistent declarations and any provider-specific field in a task.
- Extend `GET`/`PUT
  /api/v1/platform/organisations/{organisation_id}/ai-settings` with
  `allowed_transfer_modes` and `max_large_attachment_bytes`. Existing optimistic
  concurrency, platform-only authorisation, explicit schemas and generated
  client rules continue to apply. Defaults permit `inline` only.
- Add typed deployment settings for enabled non-inline modes, template ceiling,
  provider upload expiry, managed signed-URL TTL (default 900 seconds, maximum
  1,800), and the Vertex staging project, user-provisioned bucket and location.
  The aggregate inline threshold cannot be configured above 5,000,000 bytes.
  Production fails fast on incomplete or incompatible configuration.
- The feature-facing `AIRequest` remains unchanged: the caller supplies only a
  task name and private `storage_reference`, never a transfer mode, provider
  file id, `gs://` URI, URL, provider name or credential. It cannot request or
  override a transfer mode.

## 2.3 Streaming, staging and durable state

- Inspect object metadata and authorise organisation ownership before selecting
  a mode or reading content. Non-inline transfers use a bounded streaming or
  secure temporary-file seam; a 50 MB source is never accumulated in Python
  memory or embedded in JSON.
- Add a provider-neutral staging/upload interface. Provider-specific HTTP and
  Google Cloud behavior stays behind adapters; feature modules and generic AI
  orchestration do not construct provider requests or cloud URIs.
- Add an organisation-scoped `ai_attachment_references` record with UUIDv7 and
  UTC timestamps. It stores the logical request id, provider, transfer mode,
  opaque external id or `gs://` URI, source storage reference, SHA-256 digest,
  size, MIME type, source lifecycle, region, status, idempotency key, expiry/last-used/deleted
  timestamps and a safe error code/bounded metadata. It never stores bytes,
  credentials, request headers, a managed signed URL or its query string, or
  raw responses. A managed URL is minted anew for a dispatch/retry and is not a
  reusable durable reference.
- Enforce an idempotency constraint covering organisation, logical request,
  provider, mode and digest. A retry reuses only a live matching record from the
  same logical request and revalidates source digest, provider and region.
- Implement a private GCS staging adapter capable of bounded upload, metadata
  verification and best-effort terminal deletion. The staging bucket must be
  non-public, regional, in the configured Vertex location and accessed with
  workload identity/ADC or approved service-account credentials. Its globally
  unique name is supplied through typed configuration; no browser upload path
  or application bucket creation/configuration is added.
- Extend the private S3-compatible storage seam to mint a managed download URL
  only after organisation ownership, immutable object identity, size, MIME and
  digest validation. URLs are HTTPS, read-only, exact-object and short-lived;
  query strings are redacted from every log/error/telemetry boundary.

## 2.4 Provider behavior

- **OpenAI:** for a transient source, upload with purpose `user_data`, set the
  shortest supported `expires_after`, pass the file id through Responses and
  delete after terminal success/failure where possible. For a retained private
  S3 source, prefer a just-in-time managed signed URL when the deployed
  provider/model contract supports file URL input.
- **Anthropic:** for a transient source, use the beta Files API and file-id
  document source, then delete after terminal success/failure. Because uploaded
  files otherwise persist until deleted, provider-file reconciliation is
  mandatory. For a retained private S3 source, prefer a just-in-time managed
  signed URL when its URL document-source contract is supported.
- **Vertex:** stage privately to the configured regional GCS bucket and pass the
  resulting `gs://` reference as Vertex `fileData`. Validate project, bucket,
  object prefix, MIME type and exact region before dispatch. The deployer must
  configure GCS Object Lifecycle Management to delete live objects with
  `age = 1`; lifecycle execution is asynchronous and is a cleanup backstop,
  not an exact 24-hour deletion guarantee.
  Configuration uses the existing Vertex project/location and ADC boundary,
  plus a typed `VERTEX_TEMP_GCS_BUCKET` name. File-based deployments may point
  `GOOGLE_APPLICATION_CREDENTIALS` at approved service-account credentials;
  workload identity remains preferred where available.
- **Azure OpenAI, DeepSeek and local:** declare no non-inline mode and reject a
  large attachment before any upload, staging or inference call.
- The fake provider/stager implements deterministic upload, reference, reuse,
  expiry and deletion behavior for the default test suite.

## 2.5 Lifecycle, jobs and operations

- Provider copies and Vertex staging objects are AI-owned derivatives. Their
  deletion never deletes the feature-owned source object or changes its file
  lifecycle.
- Terminal success, permanent failure and exhausted retry attempt cleanup run
  immediately for provider uploads and may attempt best-effort Vertex staging
  deletion. A scheduled Dramatiq reconciliation task covers provider-hosted
  file references, but never GCS staging objects or managed signed URLs.
- Managed signed URLs expire through their short TTL and never own/delete the
  retained feature source. Vertex GCS lifecycle configuration is entirely
  deployer-owned: the application runs no GCS cleanup cron/reconciliation job.
  Deployers seeking Files-API-like ephemeral storage must disable soft delete,
  versioning and conflicting retention/holds, or explicitly accept their
  longer retention semantics.
- Broker messages remain reference-only. Retries re-head and re-digest the
  private source before reuse or upload; worker memory and concurrency remain
  bounded independently of provider quotas.
- Metrics and audit events cover selected transfer mode, upload/staging outcome,
  reuse, expiry and cleanup without recording content or bearer material.
- Runbooks document retention, IAM, incident cleanup, regional guarantees,
  disabling a compromised mode and recovering a cleanup backlog.

---

# 3. Out of Scope (Explicitly Deferred)

| Capability | Deferred to |
| --- | --- |
| Caller-supplied HTTP(S) URLs and redirects to unvalidated fetch targets | A later security-reviewed external-URL ingestion capability |
| Public or caller-visible signed URLs | Prohibited; v0.8 only mints backend-to-provider URLs for authorised retained objects |
| Reuse of provider file ids across logical AI requests | First product needing a durable provider-side document library and explicit ownership/deletion UX |
| Azure OpenAI non-inline file input | After `user_data`, expiry/deletion and regional parity are verified against the deployed API version |
| Large-file support for DeepSeek or local/OpenAI-compatible providers | A future adapter with an authenticated, private and tested transfer capability |
| Large images, audio, video, office formats or multiple large files per request | A later modality-specific release; v0.8 non-inline is one PDF only |
| Public buckets, public object ACLs, browser-to-provider upload or frontend provider credentials | Prohibited |
| OCR, parsing, chunking, embeddings, vector stores and RAG | Separate document/retrieval capabilities |
| Permanent provider-side document libraries | Product-owned capability, not transient AI execution infrastructure |
| Automatic cross-region upload/inference fallback | Prohibited |

---

# 4. Commands That Must Work

All v0.7 commands remain part of the gate:

```bash
make dev
make migrate
make lint
make typecheck
make test
make format
make generate-client
make validate-execution-contracts
make test-ai-contracts
make e2e
make check
```

`make test-ai-contracts` must continue to skip cleanly without dedicated
non-production credentials. OpenAI, Anthropic and Vertex large-file contract
tests run only against dedicated non-production accounts/projects and use
non-sensitive fixture PDFs. Migration validity, generated-client drift and all
fake-provider transfer tests are mandatory in the normal CI gate.

---

# 5. Acceptance Criteria

1. **Unchanged feature boundary:** a feature supplies only `task` and a private
   `storage_reference`; transfer mode selection and every provider/cloud
   identifier remain internal to `AIService` and its adapters.
2. **Deterministic default-deny policy:** aggregate raw attachment bytes at or
   below 5,000,000 use inline. Above that threshold, a transient source prefers
   a provider upload, a retained private S3 source prefers a managed signed URL
   where supported, and Vertex uses configured GCS staging. Dispatch occurs
   only when source lifecycle, task, organisation, model/provider and deployment
   policies allow the selected mode.
3. **Bounded input:** exactly one PDF above 5,000,000 and at most
   50,000,000 bytes can use a non-inline mode. Other MIME types, counts and sizes fail
   safely before upload/staging; the large path does not accumulate the whole
   object in worker memory.
4. **Durable idempotency:** retries of one logical request reuse one live
   matching external reference; changed digest/provider/region creates a new
   idempotent transfer, and separate requests never share a reference.
5. **Lifecycle:** success, permanent failure, timeout, worker crash and provider
   deletion failure are covered. Reconciliation exposes provider-file orphans;
   managed URLs expire; Vertex relies on the documented deployer-owned one-day
   lifecycle backstop and has no application cleanup scheduler. No AI cleanup
   path deletes feature-owned source objects.
6. **Provider closure:** OpenAI and Anthropic upload/use/delete-or-expiry and
   managed-URL ingestion, plus Vertex stage/reference and lifecycle-contract
   documentation,
   satisfy normalized fake-backed contracts. Azure, DeepSeek and local fail
   before transfer.
7. **Region and IAM:** Vertex accepts only the configured private same-region
   staging bucket and approved workload identity/ADC credentials; no provider
   path silently changes region, project, endpoint or privacy mode.
8. **Tenant and secret safety:** cross-organisation source/reference access is
   denied. Database rows, broker messages, logs, Sentry and audit metadata
   contain no bytes, credentials, provider headers, raw responses or managed
   signed URLs/query strings. Caller-supplied URLs are rejected. Existing
   protected settings routes remain in the security matrix.
9. **API/configuration:** the platform AI-settings `GET`/`PUT` schemas expose the
   typed transfer policy with optimistic concurrency; production validation
   rejects unsafe/incomplete mode configuration and generated types are clean.
10. **Operations and governance:** metrics, alerts and runbooks cover transfer
    and cleanup health; official provider behavior is dated and cited; required
    human reviews are recorded; `make check`, migration validation, relevant
    provider contracts and the release architecture audit are green.

## 5.1 Capability traceability

| Source requirement | Acceptance | Owning checkpoint | API/frontend surface | Required test evidence |
| --- | --- | --- | --- | --- |
| Provider-neutral transfer selection | §5.1–§5.3 | Scope §6.1–§6.2 | Internal `AIService`; no new feature endpoint or frontend consumer | Registry, routing, policy-intersection, pre-dispatch denial and import-boundary tests |
| Organisation transfer policy | §5.2, §5.9 | Scope §6.2 | Existing platform `GET`/`PUT /api/v1/platform/organisations/{organisation_id}/ai-settings`; extended explicit request/response schemas; generated types only, no new view | API integration, optimistic concurrency, default-off, platform/cross-plane and generated-client drift tests |
| Durable reference/idempotency boundary | §5.3–§5.5, §5.8 | Scope §6.3 | Internal persistence/service surface; no public endpoint | Migration, PostgreSQL tenant isolation, concurrency, retry reuse, digest mismatch, expiry and source-ownership tests |
| Vertex private GCS reference | §5.4–§5.7 | Scope §6.4 | Internal storage/provider adapters only | Fake staging/use/delete, IAM/region fail-closed and documented-lifecycle tests plus opt-in non-production contract |
| OpenAI provider upload / managed URL | §5.4–§5.6, §5.8 | Scope §6.5 | Internal provider/storage adapters only | Fake upload/use/delete and retained-source URL tests plus opt-in non-production contract |
| Anthropic provider upload / managed URL | §5.4–§5.6, §5.8 | Scope §6.6 | Internal provider/storage adapters only | Fake upload/use/delete and retained-source URL tests plus opt-in non-production contract |
| Cleanup and crash recovery | §5.4–§5.5, §5.8 | Scope §6.7 | Provider-file Dramatiq reconciliation only; existing AI result polling remains unchanged | Terminal-path, redelivery, worker-crash, provider-file reconciliation, URL expiry and leakage tests |
| Operations and release closure | §5.7–§5.10 | Scope §6.8 | Configuration/docs/metrics; no new route or UI | Production-config, observability/redaction, full gate, provider contracts, e2e and architecture audit |

---

# 6. Progress Log

Check items off only after the implement → review → apply-and-commit loop. Each
subsection is one checkpoint and runs on its own `feature/*` branch. A human
review gate named in a subsection must be recorded before prompt 03 may apply,
commit or merge that checkpoint.

## 6.1 Contract, Provider Decisions and Architecture Amendment

Dependencies: completed v0.7 release.

- [x] Re-verify the official provider sources in §2.1 plus official S3 signed-URL and GCS lifecycle documentation; record verification date, supported API/version, retention/deletion behavior, MIME/size limits and regional caveats in ADR-0017 and provider contract fixtures
- [x] Amend ADR-0017, BP §3/§17/§18/§23/§27–§33 and `ARCHITECTURE.md` with the four transfer modes, 5,000,000-byte aggregate inline threshold, 50,000,000-byte PDF ceiling, source-lifecycle selection, retry-only provider-reference reuse, managed-URL threat model and caller-URL prohibition
- [x] Add provider-neutral transfer/reference contracts and fake implementations under `app/ai/`; keep `AIRequest` unchanged and strengthen import-boundary tests so feature modules cannot name transfer modes or provider references
- [x] Registry/config contract tests fail on inconsistent mode, source lifecycle, MIME, threshold/ceiling, provider, expiry/TTL or regional declarations

Human review required before application: secret/IAM handling and provider data
handling decisions.

> **Recorded human review and application authorisation (AGENTS.md — secret/IAM
> handling and provider data handling decisions).**
> On 2026-08-12, after the v0.8 Scope §6.1 review requested changes (inline
> allowed-mode intersection, Anthropic delete-only lifecycle, per-mode model
> limits, complete inconsistency gates, BP §29–§32 amendments and a green
> `make e2e`), the repository owner explicitly authorised the implementer to
> apply those corrections and complete the validation/PR/merge workflow. The
> corrected checkpoint is approved for application:
>
> 1. **Provider-neutral transfer contracts** (`app/ai/transfer.py`,
>    `app/ai/staging.py`) and the re-verified provider fixture
>    (`app/ai/contracts/providers.yaml`, verified 2026-08-11) with per-mode
>    MIME/byte limits, the reviewed source-lifecycle matrix, two distinct
>    provider retention kinds (automatic expiry with bounds vs delete-only
>    with terminal deletion/reconciliation) and cited regional sources; no
>    provider SDK, secret, migration, public API or frontend consumer is
>    introduced by this checkpoint.
> 2. **Registry/config consistency gates** (per-mode `transfer_mode_limits`,
>    required regional declarations, lifecycle pinning) that fail fast on
>    inconsistent mode, lifecycle, MIME, threshold/ceiling, provider,
>    expiry/TTL or regional declarations; `AIRequest` remains unchanged and
>    import-boundary tests keep transfer/provider concepts inside `app/ai/`.
> 3. **ADR-0017, BP §3/§17/§18/§23/§27–§33 and `ARCHITECTURE.md` amendments**
>    covering the four modes, thresholds, selection, retry-only reuse,
>    managed-URL threat model and caller-URL prohibition, bounded to the
>    contract's threat model and tests.

No authentication, permission-model, tenant-isolation, destructive-migration
or public-API changes are introduced by this work unit.

## 6.2 Organisation Policy, Registry Routing and Settings API

Dependencies: Scope §6.1.

- [x] Add task/model transfer-mode capabilities and deterministic mode selection: inline at or below 5,000,000 aggregate raw bytes; above it prefer provider upload for transient sources, managed signed URL for retained private S3 sources, and GCS staging for Vertex; fail before external transfer when no permitted/provider-supported mode is eligible
- [x] Add `allowed_transfer_modes` (default `inline`) and `max_large_attachment_bytes` (maximum 50,000,000) to `organisation_ai_settings` with an additive Alembic migration, constraints, model/query/service updates and optimistic-concurrency tests
- [x] Extend explicit schemas for `GET`/`PUT /api/v1/platform/organisations/{organisation_id}/ai-settings`, regenerate the frontend client, and prove platform/cross-plane security and stale-update behavior remain intact
- [x] Add typed deployment settings and production fail-fast validation for enabled modes, upload expiry, managed signed-URL TTL and Vertex staging project/user-provisioned bucket/location; test default-deny and every invalid combination without creating or configuring cloud infrastructure

Human review required before application: tenant-isolation, database migration,
platform configuration, secret handling and the additive public API change.

> **Recorded human review and application authorisation (AGENTS.md — §6.2
> tenant-isolation, database-migration, platform-configuration,
> secret-handling and additive-public-API categories).**
> On 2026-08-12, after the v0.8 Scope §6.2 review requested changes (multi-model
> routing was not closed over effective transfer-mode eligibility: a candidate
> survived on any fitting non-inline mode instead of a mode actually eligible
> under the task, lifecycle, organisation, deployment and inline threshold),
> the repository owner explicitly authorised the implementer to apply those
> corrections and complete the validation/PR/merge workflow. The corrected
> checkpoint is approved for application:
>
> 1. **Deterministic policy-aware mode selection** (`select_transfer_mode_for_policy`
>    in `app/ai/transfer.py`, `AIService._select_transfer_mode` in
>    `app/ai/service.py`): routing and mode selection are one coherent decision —
>    each candidate survives only when at least one mode is eligible under the
>    current size/MIME/count, task, lifecycle, organisation policy, deployment
>    configuration, the model's reviewed inline/per-mode limits and its
>    provider's contract, with deterministic ordering preserved among eligible
>    candidates and multi-model regression tests for both reproduced cases.
> 2. **Organisation settings columns** (`allowed_transfer_modes`,
>    `max_large_attachment_bytes`) with the additive Alembic migration,
>    constraints, model/query/service mapping and concurrency evidence.
> 3. **Explicit GET/PUT schemas** for
>    `/api/v1/platform/organisations/{organisation_id}/ai-settings`, the
>    regenerated frontend client, and the platform/cross-plane security matrix
>    plus stale-update behavior.
> 4. **Typed deployment settings and production fail-fast validation**
>    (`app/ai/deployment.py`) for enabled modes, upload expiry, managed
>    signed-URL TTL and the Vertex staging bucket, with default-deny and every
>    invalid combination tested without creating or configuring cloud
>    infrastructure.

## 6.3 Streaming Transfer and Durable Reference Lifecycle

Dependencies: Scope §6.2.

- [x] Add bounded object streaming/secure temporary-file support behind `ObjectStorage`; verify source ownership, size, MIME and SHA-256 without accumulating 50 MB in memory and preserve existing adapters/callers
- [x] Add just-in-time managed download-URL minting for retained private S3 objects behind `ObjectStorage`; require exact immutable object identity, HTTPS/read-only/short TTL and query-string redaction, and never return or persist the URL
- [x] Add the organisation-scoped `ai_attachment_references` table, migration, ORM model and complex/reused queries with the §2.3 fields, safe constraints/indexes and idempotency uniqueness
- [x] Implement transfer orchestration services for create/adopt/reuse/expire/delete with transaction boundaries, safe errors and explicit proof that AI cleanup never deletes the feature source
- [x] Integration tests cover cross-org denial, concurrent duplicate creation, digest change, expired reference replacement, forbidden persisted fields and rollback/error paths

Human review required before application: tenant-isolation, migration and
provider-reference data handling.

> **Recorded human review and application authorisation (AGENTS.md — §6.3
> tenant-isolation, database-migration and provider-reference-data-handling
> categories).**
> On 2026-08-12, after the v0.8 Scope §6.3 review requested changes (exact
> immutable-identity/digest validation missing from just-in-time managed-URL
> minting; an untracked provider copy when durable persistence fails after a
> successful stage; deletion trusting a stale reference instead of the
> authoritative live row; `adopt` touching a terminal row after replacement;
> plus the non-blocking constraint/provider/expire-delete composition items),
> the repository owner explicitly authorised the implementer to apply those
> corrections and complete the validation/PR/merge workflow. The corrected
> checkpoint is approved for application:
>
> 1. **Bounded streaming/secure temporary-file support** (`stream_object` on
>    `ObjectStorage` with S3/fake adapters, `StreamedSource` with incremental
>    SHA-256 and head/read race detection) verifying ownership, size, MIME and
>    digest without accumulating the 50 MB ceiling in memory; existing
>    adapters/callers preserved.
> 2. **Managed download-URL minting** (`mint_managed_download_url` /
>    `TransferOrchestrator.mint_managed_url`) re-heading the retained source
>    and re-streaming it bounded so the incremental SHA-256 must equal the
>    durable digest before a short-TTL HTTPS GET URL is minted; the URL is
>    never persisted, returned or logged and is redacted at every boundary.
> 3. **Organisation-scoped `ai_attachment_references` table** (additive
>    migration `c3d4e5f6a7b8`, ORM model, org-scoped queries) with the §2.3
>    fields, a partial unique index on live rows for retry-only reuse, the
>    three non-inline transfer modes as a database invariant and forbidden
>    URL/bytes columns; `alembic check` clean.
> 4. **Transfer orchestration** (`TransferOrchestrator` +
>    `SQLTransferReferenceStore`) with a compensated stage→persist boundary,
>    authoritative live-row deletion (never a stale caller reference),
>    live-row adoption after expired replacement, an expire→delete sweep that
>    composes safely, and proof that AI cleanup never deletes the feature
>    source.
> 5. **Integration tests** covering cross-org denial, concurrent duplicate
>    creation, digest change, expired replacement, forbidden persisted fields,
>    rollback/error paths and the review's blocking regressions.
>
> No public API, frontend consumer or `PROTECTED_ROUTES` change is introduced
> by this work unit (internal persistence/service surface only).

## 6.4 Vertex Private GCS Reference

Dependencies: Scope §6.3.

- [x] Add the private GCS staging adapter/configuration with bounded upload, metadata/head and best-effort terminal delete; keep Google auth/storage behavior behind adapters and add no browser-facing or public URL path, bucket creation or lifecycle mutation
- [x] Validate actual bucket project, regional location, private access, approved prefix, object size/MIME/digest and Vertex location before creating a `gs://` `fileData` reference
- [x] Implement idempotent stage/reference/use and best-effort terminal delete behavior; fail closed for multi-region, cross-region, foreign-project, public or mismatched objects; document that deployers configure live-object deletion with `age = 1` and that lifecycle execution is asynchronous
- [x] Fake-backed tests cover staging and best-effort deletion without an application GCS reconciliation job; opt-in Vertex contract tests use a user-provisioned dedicated non-production project/bucket and ADC/workload identity, never a Gemini API key

Human review required before application/enabling: infrastructure, IAM/secret
handling, tenant isolation and regional data movement. **Approved** (human
review recorded for all four §6.4 gate categories: infrastructure, IAM/secret
handling, tenant isolation and regional data movement).

## 6.5 OpenAI Provider Upload and Managed URL

Dependencies: Scope §6.3.

- [x] Implement streamed OpenAI `user_data` upload, shortest configured `expires_after`, Responses file-id input and delete for transient sources; implement managed file-URL input for retained private S3 sources behind the adapter; no generic AI service code imports provider HTTP shapes
- [x] Enforce the PDF/50,000,000-byte/model/context/region policy before upload and normalize upload, expiry, use and deletion failures into safe retryable/permanent AI errors
- [x] Fake-backed tests cover source-lifecycle selection, upload/use/delete, managed-URL fetch, URL non-persistence/redaction/expiry, retry reuse, terminal cleanup, timeout and deletion failure; opt-in non-production OpenAI contract tests verify current behavior and skip cleanly without credentials

Human review required before enabling the mode: provider data retention,
regional configuration and secret handling.

> **Recorded human review and application authorisation (AGENTS.md — §6.5
> provider-data-retention, regional-configuration and secret-handling
> categories).**
> On 2026-08-13, after the v0.8 Scope §6.5 review returned `APPROVED` (no
> must-fix or should-fix blocking items), the human reviewer explicitly
> approved the checkpoint for application. The three §6.5 gate categories are
> covered by the reviewed surface: the OpenAI upload store and Responses-API
> dispatch enforce the provider retention contract (`user_data` purpose,
> `expires_after` bounds 1 h..30 d, provider-reported `expires_at` as the
> durable expiry, best-effort terminal delete) and the region/model/context
> policy before any upload; the managed URL travels only in the in-memory
> `ProviderRequest.managed_url` field and is never persisted, logged or
> returned; and no secret handling is introduced beyond the existing
> `AI_OPENAI_API_KEY` adapter credential.
>
> No authentication, permission-model, tenant-isolation, destructive-migration
> or public-API change is introduced by this work unit (internal
> provider/storage-adapter surface only).

## 6.6 Anthropic Provider Upload and Managed URL

Dependencies: Scope §6.3.

- [x] Implement streamed Anthropic beta Files API upload, file-id document source and delete for transient sources; implement managed URL document source for retained private S3 sources behind the adapter; pin the reviewed beta header/version in one place
- [x] Enforce the PDF/50,000,000-byte/model/context/inference-geography policy before upload and normalize file-not-found, size, context and deletion failures safely
- [x] Fake-backed tests cover source-lifecycle selection, upload/use/delete, managed-URL fetch, URL non-persistence/redaction/expiry, retry reuse and persistent-file cleanup; opt-in non-production Anthropic contract tests verify current behavior and skip cleanly without credentials

> **Lessons learned from the OpenAI build (Scope §6.5) that apply here.** OpenAI
> and Anthropic have similar shapes, so the OpenAI work is the reference for the
> general methodology of this work unit: streamed provider upload, file-id or
> URL document source, just-in-time managed URL, retry reuse and terminal
> cleanup all follow the Scope §6.5 approach. One OpenAI finding changes the
> local transient path: a managed signed URL minted from our private
> S3-compatible storage cannot serve transient files in a local environment —
> local S3 storage is not reachable from the provider's network, so the URL
> never resolves for Anthropic. When a transient source runs locally, the
> adapter uploads the object to the scratch GCS staging directory and provides a
> signed URL to that GCS object as the document source instead. The Anthropic
> managed-URL path must therefore support this local-transient scratch-GCS
> behavior alongside the beta Files API upload, and the fake-backed and opt-in
> contract tests must cover the local transient path.

Human review required before enabling the mode: beta-provider contract, provider
data retention, inference geography and secret handling.

> **Recorded human review and application authorisation (AGENTS.md — §6.6
> beta-provider-contract, provider-data-retention, inference-geography and
> secret-handling categories).**
> On 2026-08-14, after review of the complete §6.6 implementation and its
> provider-neutral PDF-inspection correction, the human reviewer explicitly
> approved the work for commit, push and merge. The reviewed surface covers
> the pinned Anthropic Files beta contract, delete-only provider retention,
> configured inference geography, API-key handling behind the adapter, and
> the documented `pypdf` runtime dependency. No authentication,
> permission-model, tenant-isolation, destructive-migration or public-API
> change is introduced by this work unit.

## 6.7 Durable Jobs, Cleanup Reconciliation and Observability

Dependencies: Scope §6.4–§6.6.

- [x] Integrate transfer records into synchronous/queued AI execution so broker payloads remain reference-only, retries re-head/re-digest sources and terminal outcomes trigger cleanup without duplicate output/cost records
- [x] Add a bounded Dramatiq reconciliation job only for expired, orphaned and deletion-failed provider-file references with idempotent claims, bounded backoff and crash recovery; prove it never processes managed URLs, GCS staging objects or feature sources
- [x] Add safe audit events and low-cardinality metrics for mode selection, transfer outcome/reuse, expiry, deletion and cleanup backlog; redaction tests cover logs, Sentry, audit, rows and broker messages
- [x] End-to-end integration tests cover success, permanent failure, timeout, worker crash and cleanup-provider outage across all four modes and both transient/retained source lifecycles

Human review required before application: background cleanup behavior and
provider-data deletion guarantees.

> **Recorded human review and application authorisation (AGENTS.md — background
> cleanup behavior and provider-data deletion guarantees).**
> On 2026-08-14, after review of the complete §6.7 implementation (PR #72),
> the repository owner explicitly approved the background cleanup behavior and
> provider-data deletion guarantees for application. The reviewed surface
> covers the bounded Dramatiq reconciliation sweep that processes only
> expired, orphaned and deletion-failed provider-file references with
> idempotent claims and bounded backoff, best-effort terminal deletion of
> provider-hosted copies with the safe `provider_reference_deletion_failed`
> error code stamped on durable rows, the documented operational schedule,
> and the low-cardinality audit events/metrics that never carry content,
> request ids, object keys or signed URLs. Managed URLs, Vertex GCS staging
> objects and feature-owned sources are never processed by the sweep. No
> authentication, permission-model, tenant-isolation, destructive-migration
> or public-API change is introduced by this work unit.

## 6.8 Operations, Documentation and Release Governance

Dependencies: Scope §6.1–§6.7.

- [x] Update `.env.example`, `.env.production.example`, README, `SECURITY.md` and the AI runbook with mode enablement, 5 MB threshold, signed-URL TTL/redaction, provider retention/deletion, Vertex project/bucket/location/IAM, the required console-configured `age = 1` lifecycle, asynchronous deletion and soft-delete/versioning/retention caveats
- [x] Confirm Azure/DeepSeek/local non-inline rejection, caller-supplied URL prohibition, managed-URL controls, absence of application GCS bucket/lifecycle/cleanup automation, provider import boundaries, migration validity, generated-client drift and all fake/provider contract suites
- [x] Record every required human review, document any dependency justification, run `make check`, relevant opt-in contracts and `make e2e`, and resolve all failures
- [x] Run `prompts/04-architecture-audit.md`; resolve every CRITICAL/MAJOR finding before marking v0.8 complete or tagging `v0.8.0`

---

# 7. Blueprint Reference Map

Line ranges were verified against the current blueprint headings. Scope §6.1
amends the v0.7 AI passages; later checkpoints follow the amended text plus the
existing governing rules below.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Contracts/architecture | **BP §2–§5** (lines 40–263), **BP §17** (898–1044), **BP §23** (1359–1449), **BP §27–§33** (1534–1994) | Modular boundaries, v0.7 attachment/storage boundary, provider adapters, configuration/never-log, audit/security/testing/tooling, human review and ADR format |
| **Scope §6.2** Policy/registry/API | **BP §7** (317–371), **BP §9–§13** (432–765), **BP §27** (1534–1603), **BP §31** (1777–1850) | Schema separation, tenant/database/API conventions, typed/default-off AI policy, security matrix |
| **Scope §6.3** Streaming/persistence | **BP §10–§11** (543–643), **BP §17** (898–1044), **BP §23** (1359–1449), **BP §29–§31** (1652–1850) | Constraints/transactions, private object ownership, adapter boundary, audit/security/testing |
| **Scope §6.4** Vertex GCS reference | **BP §17** (898–1044), **BP §23** (1359–1449), **BP §27–§33** (1534–1967), **BP §38** (2203–2226) | Provider-neutral storage, Vertex adapter/auth boundary, region/IAM controls, environment isolation and human review |
| **Scope §6.5** OpenAI upload | **BP §23** (1359–1449), **BP §27–§28** (1534–1651), **BP §30–§33** (1700–1967) | Adapter isolation, typed secrets/regions, never-log rules, file security, tests and dependency/review rules |
| **Scope §6.6** Anthropic upload | **BP §23** (1359–1449), **BP §27–§28** (1534–1651), **BP §30–§33** (1700–1967) | Adapter isolation, inference geography/configuration, retention-safe observability, security and contract tests |
| **Scope §6.7** Jobs/cleanup/metrics | **BP §18** (1045–1150), **BP §28–§31** (1604–1850) | Durable idempotent jobs, retries/queues, safe metrics/logs/audit, security and integration tests |
| **Scope §6.8** Operations/governance | **BP §28** (1604–1651), **BP §30–§34** (1700–1994), **BP §37–§38** (2160–2226), **BP §42** (2337–2358) | Never-log/security controls, reviews/ADRs, CI, environment separation, template validation |

---

# 8. Status

```text
Release:    v0.8.0 (large AI attachments and reference transfer modes)
State:      planned
Started:    —
Completed:  —
```

When every §6 checkpoint is checked after review and every §5 criterion is
verified, update the backend/frontend versions, mark this status complete and
tag `v0.8.0` from the reviewed release-bookkeeping commit.
