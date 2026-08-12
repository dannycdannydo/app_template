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
metadata, and reliably deletes provider-side or staging copies.

---

# 2. In Scope

## 2.1 Fixed release decisions

The open decisions in the source plan are resolved for this release:

1. **Vertex default:** a private, same-region Google Cloud Storage staging
   object referenced as `gs://...` is the only Vertex large-file path. There is
   no Vertex managed-file upload abstraction.
2. **Reuse boundary:** a provider-side reference may be reused only by retries
   of one logical AI execution. Reuse across distinct AI requests is prohibited.
3. **Template ceiling:** the non-inline path accepts exactly one
   `application/pdf` file, larger than the applicable inline ceiling and no
   larger than **50 MB** and no larger than **50,000,000 bytes**. Provider
   ceilings may be lower and always win.
4. **URL inputs:** all HTTP(S) URL inputs, including private signed URLs, are
   excluded. v0.8 never turns a private object into a bearer URL for a model.
5. **Organisation policy:** add explicit typed columns to
   `organisation_ai_settings`; do not add an opaque policy blob. Bounded arrays
   may use JSONB consistently with the existing provider/model allowlists.
6. **Provider matrix:** OpenAI and Anthropic implement `provider_upload`;
   Vertex implements `storage_reference`; Azure OpenAI, DeepSeek and local
   adapters fail closed for non-inline files in v0.8. Azure file input remains
   deferred until its `user_data`, expiry and regional behavior reaches the
   same reviewed contract as OpenAI.

These defaults deliberately choose the common 50 MB PDF envelope documented
for Vertex rather than inheriting the much higher upload quotas exposed by
OpenAI or Anthropic. Provider capabilities and limits must be re-verified from
official documentation during each adapter work unit:

- [OpenAI Files API](https://platform.openai.com/docs/api-reference/files)
- [Anthropic Files API](https://platform.claude.com/docs/en/build-with-claude/files)
- [Vertex AI document understanding](https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/document-understanding)
- [Azure OpenAI Responses file input](https://learn.microsoft.com/en-us/azure/ai-services/openai/quickstart?pivots=rest-api&tabs=command-line%2Ckeyless%2Ctypescript-keyless%2Centra)

## 2.2 Transfer contract and policy

- Introduce provider-neutral transfer modes: `inline`, `provider_upload` and
  `storage_reference`. `inline` remains the default and retains all v0.7 limits.
- A non-inline mode is eligible only when the task definition, organisation
  policy, model/provider capability and deployment configuration all allow it.
- Extend task/model registries with explicit supported transfer modes,
  per-mode MIME types and byte ceilings. Startup/CI validation rejects
  inconsistent declarations and any provider-specific field in a task.
- Extend `GET`/`PUT
  /api/v1/platform/organisations/{organisation_id}/ai-settings` with
  `allowed_transfer_modes` and `max_large_attachment_bytes`. Existing optimistic
  concurrency, platform-only authorisation, explicit schemas and generated
  client rules continue to apply. Defaults permit `inline` only.
- Add typed deployment settings for enabled non-inline modes, the template
  ceiling, provider upload expiry, and the Vertex staging project, regional
  bucket and location. Production fails fast on incomplete or incompatible
  configuration.
- The feature-facing `AIRequest` remains unchanged: the caller supplies only a
  task name and private `storage_reference`, never a transfer mode, provider
  file id, `gs://` URI, URL, provider name or credential.

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
  size, MIME type, region, status, idempotency key, expiry/last-used/deleted
  timestamps and a safe error code/bounded metadata. It never stores bytes,
  credentials, request headers, signed URL query strings or raw responses.
- Enforce an idempotency constraint covering organisation, logical request,
  provider, mode and digest. A retry reuses only a live matching record from the
  same logical request and revalidates source digest, provider and region.
- Implement a private GCS staging adapter capable of bounded upload, metadata
  verification and deletion. The staging bucket must be non-public, regional,
  in the configured Vertex location and accessed with workload identity/ADC or
  approved service-account credentials. No browser upload path is added.

## 2.4 Provider behavior

- **OpenAI:** upload with purpose `user_data`, set the shortest supported
  `expires_after`, pass the returned file id through the Responses input-file
  form, and delete after terminal success/failure where possible.
- **Anthropic:** use the beta Files API and file-id document source, then delete
  after terminal success/failure. Because uploaded files otherwise persist
  until deleted, reconciliation is mandatory before this mode can be enabled.
- **Vertex:** stage privately to the configured regional GCS bucket and pass the
  resulting `gs://` reference as Vertex `fileData`. Validate project, bucket,
  object prefix, MIME type and exact region before dispatch.
- **Azure OpenAI, DeepSeek and local:** declare no non-inline mode and reject a
  large attachment before any upload, staging or inference call.
- The fake provider/stager implements deterministic upload, reference, reuse,
  expiry and deletion behavior for the default test suite.

## 2.5 Lifecycle, jobs and operations

- Provider copies and Vertex staging objects are AI-owned derivatives. Their
  deletion never deletes the feature-owned source object or changes its file
  lifecycle.
- Terminal success, permanent failure and exhausted retry attempt cleanup run
  immediately. A scheduled Dramatiq reconciliation task finds expired,
  orphaned and deletion-failed records, retries with bounded backoff and exposes
  a low-cardinality backlog metric.
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
| HTTP(S) URL input, including private signed URLs | A later security-reviewed release with a concrete non-sensitive product use case |
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
2. **Default-deny policy:** inline remains the default. A non-inline dispatch
   occurs only when task, organisation, model/provider and deployment policies
   all allow the same mode; every denial occurs before external transfer.
3. **Bounded input:** exactly one PDF above the inline ceiling and at most
   50,000,000 bytes can use a non-inline mode. Other MIME types, counts and sizes fail
   safely before upload/staging; the large path does not accumulate the whole
   object in worker memory.
4. **Durable idempotency:** retries of one logical request reuse one live
   matching external reference; changed digest/provider/region creates a new
   idempotent transfer, and separate requests never share a reference.
5. **Lifecycle:** success, permanent failure, timeout, worker crash and provider
   deletion failure are covered. Reconciliation exposes and eventually clears
   orphaned references without deleting feature-owned source objects.
6. **Provider closure:** OpenAI upload/use/delete-or-expiry, Anthropic
   upload/use/delete and Vertex stage/reference/delete satisfy one normalized
   fake-backed contract. Azure, DeepSeek and local fail before transfer.
7. **Region and IAM:** Vertex accepts only the configured private same-region
   staging bucket and approved workload identity/ADC credentials; no provider
   path silently changes region, project, endpoint or privacy mode.
8. **Tenant and secret safety:** cross-organisation source/reference access is
   denied. Database rows, broker messages, logs, Sentry and audit metadata
   contain no bytes, credentials, provider headers, raw responses or signed
   URLs. Existing protected settings routes remain in the security matrix.
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
| OpenAI provider upload | §5.4–§5.6 | Scope §6.4 | Internal provider adapter only | Fake upload/use/delete/failure tests plus opt-in non-production contract |
| Anthropic provider upload | §5.4–§5.6 | Scope §6.5 | Internal provider adapter only | Fake upload/use/delete/failure tests plus opt-in non-production contract |
| Vertex private GCS reference | §5.4–§5.7 | Scope §6.6 | Internal storage/provider adapters only | Fake staging/use/delete, IAM/region fail-closed tests plus opt-in non-production contract |
| Cleanup and crash recovery | §5.4–§5.5, §5.8 | Scope §6.7 | Internal Dramatiq job; existing AI result polling remains unchanged | Terminal-path, redelivery, worker-crash, provider-outage, reconciliation and leakage tests |
| Operations and release closure | §5.7–§5.10 | Scope §6.8 | Configuration/docs/metrics; no new route or UI | Production-config, observability/redaction, full gate, provider contracts, e2e and architecture audit |

---

# 6. Progress Log

Check items off only after the implement → review → apply-and-commit loop. Each
subsection is one checkpoint and runs on its own `feature/*` branch. A human
review gate named in a subsection must be recorded before prompt 03 may apply,
commit or merge that checkpoint.

## 6.1 Contract, Provider Decisions and Architecture Amendment

Dependencies: completed v0.7 release.

- [ ] Re-verify the four official provider sources in §2.1; record verification date, supported API/version, retention/deletion behavior, MIME/size limits and regional caveats in ADR-0017 and provider contract fixtures
- [ ] Amend ADR-0017, BP §3/§17/§18/§23/§27–§33 and `ARCHITECTURE.md` with the three transfer modes, fixed provider matrix, 50,000,000-byte PDF boundary, retry-only reuse, ownership/threat model and URL prohibition
- [ ] Add provider-neutral transfer/reference contracts and fake implementations under `app/ai/`; keep `AIRequest` unchanged and strengthen import-boundary tests so feature modules cannot name transfer modes or provider references
- [ ] Registry/config contract tests fail on inconsistent mode, MIME, ceiling, provider, expiry or regional declarations

Human review required before application: secret/IAM handling and provider data
handling decisions.

## 6.2 Organisation Policy, Registry Routing and Settings API

Dependencies: Scope §6.1.

- [ ] Add task/model transfer-mode capabilities and deterministic mode selection: inline first when eligible, otherwise the single policy intersection; fail before external transfer when none is eligible
- [ ] Add `allowed_transfer_modes` (default `inline`) and `max_large_attachment_bytes` (maximum 50,000,000) to `organisation_ai_settings` with an additive Alembic migration, constraints, model/query/service updates and optimistic-concurrency tests
- [ ] Extend explicit schemas for `GET`/`PUT /api/v1/platform/organisations/{organisation_id}/ai-settings`, regenerate the frontend client, and prove platform/cross-plane security and stale-update behavior remain intact
- [ ] Add typed deployment settings and production fail-fast validation for enabled modes, upload expiry and Vertex staging project/bucket/location; test default-deny and every invalid combination

Human review required before application: tenant-isolation, database migration,
platform configuration, secret handling and the additive public API change.

## 6.3 Streaming Transfer and Durable Reference Lifecycle

Dependencies: Scope §6.2.

- [ ] Add bounded object streaming/secure temporary-file support behind `ObjectStorage`; verify source ownership, size, MIME and SHA-256 without accumulating 50 MB in memory and preserve existing adapters/callers
- [ ] Add the organisation-scoped `ai_attachment_references` table, migration, ORM model and complex/reused queries with the §2.3 fields, safe constraints/indexes and idempotency uniqueness
- [ ] Implement transfer orchestration services for create/adopt/reuse/expire/delete with transaction boundaries, safe errors and explicit proof that AI cleanup never deletes the feature source
- [ ] Integration tests cover cross-org denial, concurrent duplicate creation, digest change, expired reference replacement, forbidden persisted fields and rollback/error paths

Human review required before application: tenant-isolation, migration and
provider-reference data handling.

## 6.4 OpenAI Provider Upload

Dependencies: Scope §6.3.

- [ ] Implement streamed OpenAI `user_data` upload, shortest configured `expires_after`, Responses file-id input and delete behind the adapter; no generic AI service code imports provider HTTP shapes
- [ ] Enforce the PDF/50,000,000-byte/model/context/region policy before upload and normalize upload, expiry, use and deletion failures into safe retryable/permanent AI errors
- [ ] Fake-backed tests cover upload/use/delete, retry reuse, terminal cleanup, timeout and deletion failure; opt-in non-production OpenAI contract tests verify current behavior and skip cleanly without credentials

Human review required before enabling the mode: provider data retention,
regional configuration and secret handling.

## 6.5 Anthropic Provider Upload

Dependencies: Scope §6.3.

- [ ] Implement streamed Anthropic beta Files API upload, file-id document source and delete behind the adapter; pin the reviewed beta header/version in one place
- [ ] Enforce the PDF/50,000,000-byte/model/context/inference-geography policy before upload and normalize file-not-found, size, context and deletion failures safely
- [ ] Fake-backed tests cover upload/use/delete, retry reuse and persistent-file cleanup; opt-in non-production Anthropic contract tests verify current behavior and skip cleanly without credentials

Human review required before enabling the mode: beta-provider contract, provider
data retention, inference geography and secret handling.

## 6.6 Vertex Private GCS Reference

Dependencies: Scope §6.3.

- [ ] Add the private GCS staging adapter/configuration with bounded upload, metadata/head and delete; keep Google auth/storage behavior behind adapters and add no browser-facing or public URL path
- [ ] Validate actual bucket project, regional location, private access, approved prefix, object size/MIME/digest and Vertex location before creating a `gs://` `fileData` reference
- [ ] Implement idempotent stage/reference/use/delete behavior and fail closed for multi-region, cross-region, foreign-project, public or mismatched objects
- [ ] Fake-backed tests cover staging and cleanup; opt-in Vertex contract tests use a dedicated non-production project/bucket and ADC/workload identity, never a Gemini API key

Human review required before application/enabling: infrastructure, IAM/secret
handling, tenant isolation and regional data movement.

## 6.7 Durable Jobs, Cleanup Reconciliation and Observability

Dependencies: Scope §6.4–§6.6.

- [ ] Integrate transfer records into synchronous/queued AI execution so broker payloads remain reference-only, retries re-head/re-digest sources and terminal outcomes trigger cleanup without duplicate output/cost records
- [ ] Add a bounded Dramatiq reconciliation job for expired, orphaned and deletion-failed references with idempotent claims, bounded backoff and crash recovery
- [ ] Add safe audit events and low-cardinality metrics for mode selection, transfer outcome/reuse, expiry, deletion and cleanup backlog; redaction tests cover logs, Sentry, audit, rows and broker messages
- [ ] End-to-end integration tests cover success, permanent failure, timeout, worker crash and cleanup-provider outage across all three modes

Human review required before application: background cleanup behavior and
provider-data deletion guarantees.

## 6.8 Operations, Documentation and Release Governance

Dependencies: Scope §6.1–§6.7.

- [ ] Update `.env.example`, `.env.production.example`, README, `SECURITY.md` and the AI runbook with mode enablement, retention/deletion, IAM, regional guarantees, cleanup incidents and emergency disable procedures
- [ ] Confirm Azure/DeepSeek/local non-inline rejection, URL prohibition, provider import boundaries, migration validity, generated-client drift and all fake/provider contract suites
- [ ] Record every required human review, document any dependency justification, run `make check`, relevant opt-in contracts and `make e2e`, and resolve all failures
- [ ] Run `prompts/04-architecture-audit.md`; resolve every CRITICAL/MAJOR finding before marking v0.8 complete or tagging `v0.8.0`

---

# 7. Blueprint Reference Map

Line ranges were verified against the current blueprint headings. Scope §6.1
amends the v0.7 AI passages; later checkpoints follow the amended text plus the
existing governing rules below.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Contracts/architecture | **BP §2–§5** (lines 40–263), **BP §17** (898–1044), **BP §23** (1359–1449), **BP §33–§34** (1906–1994) | Modular boundaries, v0.7 attachment/storage boundary, provider adapters, human review and ADR format |
| **Scope §6.2** Policy/registry/API | **BP §7** (317–371), **BP §9–§13** (432–765), **BP §27** (1534–1603), **BP §31** (1777–1850) | Schema separation, tenant/database/API conventions, typed/default-off AI policy, security matrix |
| **Scope §6.3** Streaming/persistence | **BP §10–§11** (543–643), **BP §17** (898–1044), **BP §23** (1359–1449), **BP §29–§31** (1652–1850) | Constraints/transactions, private object ownership, adapter boundary, audit/security/testing |
| **Scope §6.4** OpenAI upload | **BP §23** (1359–1449), **BP §27–§28** (1534–1651), **BP §30–§33** (1700–1967) | Adapter isolation, typed secrets/regions, never-log rules, file security, tests and dependency/review rules |
| **Scope §6.5** Anthropic upload | **BP §23** (1359–1449), **BP §27–§28** (1534–1651), **BP §30–§33** (1700–1967) | Adapter isolation, inference geography/configuration, retention-safe observability, security and contract tests |
| **Scope §6.6** Vertex GCS reference | **BP §17** (898–1044), **BP §23** (1359–1449), **BP §27–§33** (1534–1967), **BP §38** (2203–2226) | Provider-neutral storage, Vertex adapter/auth boundary, region/IAM controls, environment isolation and human review |
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
