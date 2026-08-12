# ADR 0017: Provider-Neutral AI Service, Task-Based Routing, Prompt/Version Lifecycle, Structured-Output Contract, Cost/Retention Boundaries

Status: Accepted (amended 2026-08-10: bounded inline attachments, storage/lifecycle ownership, regional configuration truthfulness, v0.8 large-file boundary; v0.7 Scope §6.1). Amended 2026-08-11: v0.8 transfer modes and re-verified provider contracts (v0.8 Scope §6.1)

## Context

The v0.1–v0.6 template gives a derived application everything it needs to call
an LLM badly: it has settings, organisation scoping, jobs and audit, but no
seam that would stop a feature module from importing an OpenAI/Anthropic SDK,
hard-coding a model name, parsing provider JSON inline, or silently skipping
cost/usage tracking. The first AI product feature would therefore re-implement
the same provider plumbing in every module and make the template's
provider-neutral, audit-everything, privacy-first rules unenforceable.

v0.7 must add a **reusable AI application layer** — not an agent framework and
not a retrieval platform (those are explicitly deferred). Application code
should be able to request a named task such as `lease.extract_terms` and
receive a validated, auditable result without ever importing an LLM SDK,
selecting a model, formatting a provider request, parsing provider JSON,
calculating cost, or writing retry logic.

## Options considered

### 1. Direct SDK usage in feature modules (rejected)

Each feature imports `openai`/`anthropic` and calls it directly. Fast to start,
but: no uniform structured-output contract, no central cost/usage/audit
records, no organisation-level enforcement, no model swap without code changes,
and it breaks the blueprint's provider-SDK-behind-adapters rule that ADR-0006
and the `app/email/` pattern (ADR-0015, pending v0.6) established. Rejected.

### 2. A thin wrapper around one SDK (rejected)

A single `LLMClient` that hard-wires OpenAI. It keeps provider SDKs out of
feature code but freezes the template on one vendor: model changes, Azure
migration, on-prem (Ollama/vLLM) or cost optimisation all become rewrites
rather than configuration. It also gives no task contract, so "which prompt and
model does this job need" stays implicit. Rejected.

### 3. Agent framework (rejected for v0.7)

Frameworks with planning loops, tools and memory solve a different problem.
They hide the request/response contract behind an orchestration model, make
deterministic costing/tracking harder, and add a large dependency that most
derived applications do not need. The scope explicitly defers autonomous
agents to a future orchestration layer that can sit *above* `AIService`. Rejected for v0.7.

### 4. Provider-neutral `AIService` with task/prompt/model registries (adopted)

A single application-facing entry point — `AIService.execute(request: AIRequest)
-> AIResult` — backed by three checked-in registries:

- **task registry** (`app/ai/tasks/`): canonical name, prompt name/version,
  input variables, required capabilities, parameter defaults, output-schema
  import path, retry policy, fallback policy;
- **prompt registry** (`app/ai/prompts/<domain>/`): versioned YAML with name,
  immutable integer version, system instructions, variables, output contract;
- **model registry** (`app/ai/models/`): provider, model identifier,
  capabilities, context window, pricing basis, availability.

A deterministic capability/cost-aware router resolves
task requirements → organisation policy → eligible model → ordered fallback.
Provider SDKs live only in `app/ai/providers/` adapters behind a typed
`LLMProvider` interface with a normalised `ProviderResponse` and error
taxonomy. A fake deterministic provider is the default test adapter, mirroring
the storage (ADR-0006) and email (`app/email/`, ADR-0015 pending v0.6) precedents.
The checked-in registries use PyYAML's safe loader. PyYAML is a small explicit
runtime dependency because prompt assets are required to be readable,
reviewable YAML; object construction is never enabled and file size/schema
validation happens before definitions enter the application.

## Decision

**Add `app/ai/` as a platform package, not a business module.** Application
code calls only `AIService.execute(request: AIRequest) -> AIResult`; feature
modules name a task, never a provider or model. The AI layer is provider-neutral:
every provider (OpenAI, Anthropic, DeepSeek, Azure OpenAI, Vertex AI Gemini,
local OpenAI-compatible) satisfies the same `LLMProvider` contract and declares
the capabilities it actually supports. No code outside `app/ai/providers/`
imports a provider SDK; an import-boundary test enforces this, exactly like the
storage boto3 rule.

The contract includes:

- **Structured outputs are the internal contract**: the service asks an adapter
  for native structured output where supported, else JSON mode/extraction;
  every result is validated against the requested Pydantic model before it is
  returned. Malformed output may trigger one bounded repair request, then
  bounded task retries; unvalidated structured data is never returned as
  success. Free text exists only when a task explicitly declares a text result.
- **Prompt versions are immutable and append-only**: correcting a prompt
  creates `*_vN.yaml`, never edits a released version. Registries validate at
  startup and in CI.
- **Organisation controls**: `organisation_ai_settings` (default-off) gates
  provider/model allowance, override, budget and retention; enforcement happens
  in `AIService`, never only in a router.
- **Usage/cost/audit**: every attempted provider execution writes an
  `ai_requests` row (task, provider, model, prompt name/version, tokens, cost,
  latency, status, safe error code); `ai_outputs` stores validated output plus
  references/digests by default, not full sensitive source text. Retention is a
  per-organisation policy with a documented deletion path; content never
  reaches logs, Sentry or audit metadata.
- **Budgets**: monthly organisation budgets are enforced before dispatch with a
  transaction-safe reservation so a budget cannot be materially overrun.
- **Jobs**: small bounded tasks run synchronously; document-scale work enqueues
  an `ai.execute` Dramatiq job on the `ai` queue with the existing durable
  record-then-enqueue lifecycle (BP §18).

### Amendment (v0.7 attachment/regional, Scope §6.1)

- **Bounded inline attachments**: a feature may supply a private
  `storage_reference` for document-scale work; `AIService` (or the `ai.execute`
  job boundary) authorises and resolves that object into a provider-neutral
  `Attachment` carrying only a validated display name, MIME type, bytes and
  SHA-256 digest. The default template limits are 5 MB per attachment and
  10 MB combined. The reference is resolved to bytes before dispatch and is
  never rendered as if it were document content; no adapter ever receives a
  private storage credential, a signed URL or an object path. Attachment bytes
  exist only in worker memory for the duration of one provider call: they are
  never persisted in `ai_requests`/`ai_outputs`, placed on the job broker, or
  written to logs, Sentry or audit metadata.
- **Storage and lifecycle ownership**: keep-flow source objects remain owned by
  their feature; AI cleanup never deletes them. Temporary analyse-only objects
  live in the organisation-scoped AI scratch namespace and are governed by the
  v0.7 retention job. Durable records persist storage references and digests,
  never attachment bytes. Every job retry re-reads and revalidates the
  referenced object so retries stay idempotent.
- **Regional configuration truthfulness**: provider regions are explicit,
  validated deployment configuration — OpenAI region and Anthropic inference
  geography are typed settings, Azure's region is inherent in its configured
  resource endpoint, Vertex stays pinned by its location setting, DeepSeek
  documents that it offers no template-controlled regional pinning, and
  local/fake providers inherit their operator-controlled location. Defaults are
  honest for ordinary accounts (regional endpoints that require provider
  approval are explicit opt-ins), unsupported regions fail configuration
  validation, and routing/fallback never implicitly changes region. Routing
  metadata records the configured or observed region only where the provider
  exposes it, without increasing label cardinality.
- **v0.8 large-file boundary**: inline is the only v0.7 transfer mode.
  Provider-hosted uploads, provider file identifiers, direct `gs://`
  references, URL inputs and larger ceilings are deferred to v0.8
  (`plans/AI_LARGE_ATTACHMENTS_V0_8_PLAN.md`); oversized or unsupported inputs
  fail before dispatch in v0.7.

### Amendment (v0.8 transfer modes and re-verified contracts, Scope §6.1)

v0.8 adds **four provider-neutral transfer modes** — `inline`,
`provider_upload`, `managed_signed_url` and `storage_reference` — without
changing the application-facing rule: a feature still supplies only a task
name and a private `storage_reference` (Scope §2.2). Transfer mode selection
and every provider/cloud identifier remain internal to `AIService` and its
adapters; the caller can never request or override a mode, and no provider
file id, `gs://` URI, URL or provider name appears in `AIRequest`. The
contracts live in `app/ai/transfer.py` and `app/ai/staging.py` behind an
import-boundary test that feature modules cannot name transfer modes or
provider references (Scope §6.1 checkbox 3).

- **Reviewed constants (Scope §2.1)**: inline is eligible only when the
  aggregate raw attachment bytes do not exceed 5,000,000. The non-inline path
  accepts exactly one `application/pdf` above that threshold and at most
  50,000,000 bytes; provider/model ceilings are lower and always win
  (Anthropic's 32 MB request-payload ceiling is recorded in the fixture).
  Managed signed-URL TTL defaults to 900 seconds with a 1,800-second maximum.
- **Source-lifecycle selection (Scope §2.2, §5.2)**: above the threshold a
  transient source prefers `provider_upload`, a retained private S3 source
  prefers `managed_signed_url`, and Vertex uses its configured private GCS
  staging bucket (`storage_reference`). A mode is eligible only when source
  lifecycle, task definition, organisation policy, model/provider capability
  and deployment configuration all allow it; otherwise the service fails
  before any external transfer — it never silently downgrades privacy
  controls, uploads to another region, or accepts a caller-supplied URL
  (caller-supplied HTTP(S) URLs remain prohibited).
- **Retry-only provider-reference reuse (Scope §2.1)**: a provider-side
  reference may be reused only by retries of one logical AI execution, and
  only while the digest, provider, mode, organisation and region still match;
  separate requests never share a reference.
- **Managed-URL threat model (Scope §2.3)**: a retained feature-owned object
  may use a service-minted, exact-object, read-only, short-lived signed URL
  minted just before dispatch when the provider supports URL ingestion. The
  URL and its query string are never returned to the caller, persisted,
  audited or logged; a URL is a temporary bearer capability, never a durable
  reference.
- **Lifecycle and cleanup (Scope §2.5)**: provider copies and GCS staging
  objects are AI-owned derivatives — deletion never deletes the feature-owned
  source object. Terminal cleanup runs immediately for provider uploads; a
  scheduled Dramatiq reconciliation job covers provider-hosted file
  references only (never managed URLs, GCS staging objects or feature
  sources). Vertex staging relies on a deployer-owned GCS Object Lifecycle
  Management rule (`age = 1`, asynchronous) as the cleanup backstop; the
  application creates/configures no bucket and runs no GCS cleanup scheduler.
- **Re-verified provider contracts (Scope §6.1 checkbox 1)**: on 2026-08-11
  the official provider and cloud-storage documentation was re-verified and
  recorded in `app/ai/contracts/providers.yaml` (verification date, supported
  API/version, retention/deletion behavior, MIME/size limits and regional
  caveats per provider, plus the S3 presigned-URL and GCS lifecycle facts).
  Key verified findings: OpenAI `user_data` files persist until deleted unless
  `expires_after` is set (anchor `created_at`, 3,600–2,592,000 seconds);
  Anthropic's Files API is beta (`files-api-2025-04-14`), files persist until
  deleted, and PDF inference is capped at a 32 MB request payload; Vertex
  documents a 50 MB per-file limit with `gs://` `fileData` in the same
  project; Azure's Responses API does not support the `user_data` purpose, so
  Azure remains fail-closed for non-inline files in v0.8. Provider retention
  is modelled as two distinct lifecycle kinds rather than one shared shape:
  automatic hard expiry with recorded bounds (`expires_after`, OpenAI) and
  delete-only persistence with no automatic expiry (`until_deleted`,
  Anthropic), where explicit terminal deletion and the reconciliation job are
  the only removal paths — a delete-only provider never declares expiry
  bounds it does not have. The loader fails
  fast on any inconsistent mode, source-lifecycle, MIME, threshold/ceiling,
  provider, expiry/TTL or regional declaration (Scope §6.1 checkbox 4), and
  the registry bundle validator rejects task/model declarations the reviewed
  contracts cannot support.
- **Human review**: the v0.8 transfer-mode work units touch tenant isolation
  (organisation transfer policy and durable references), secret/IAM handling
  (provider uploads, managed URLs, Vertex staging credentials), database
  migrations and provider data handling; every checkpoint names its review
  gate and prompt 03 cannot apply it until the review is recorded
  (BP §33, Scope §6).

## Consequences

- Feature code contains zero provider/SDK/model references; provider or model
  changes are configuration and review, not code rewrites (acceptance criterion
  §5.2).
- The template ships a working `document.classify` demonstration task proving
  the full task → prompt → router → provider → validation → tracking/audit →
  result/job flow without pretending to solve commercial-property extraction.
- New provider adapters are additive: each implements `LLMProvider` and
  registers in the factory; nothing outside `app/ai/providers/` changes.
- The AI layer adds a deliberate amount of machinery (registries, router,
  records). That cost is the price of keeping the blueprint's provider-neutral,
  tenant-isolated, auditable rules true for AI; the scope explicitly defers
  everything the layer does not need (agents, RAG, chat streaming).
- Human review is required (BP §33) for the tenant/configuration work
  (`organisation_ai_settings`), secret handling (provider credentials) and any
  new provider SDK dependencies.
- Attachments are capability-gated and bounded: a task can only route to a
  model that declares the `documents` capability and the per-model inline
  attachment ceiling; incompatible modality, MIME type and size combinations
  are rejected before provider dispatch rather than silently downgraded.
  Adapters that cannot carry documents (DeepSeek; local until a reviewed
  capability exists) fail fast, preserving the "no fake interchangeability"
  rule.
- The v0.7 inline seam deliberately stops short of large-file and
  provider-reference support; those modes add durable external-file state,
  upload/cleanup jobs and IAM/expiry policy that belong to a separate reviewed
  release (v0.8 plan), so v0.7 never has to mint signed URLs, manage provider
  file identifiers or place bytes on the broker.
