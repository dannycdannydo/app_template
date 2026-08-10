# Template v0.7 — AI / LLM Application Service Layer — Scope & Progress Log

## Relationship to other documents

- `Internal_Custom_Application_Starter_Architecture_v2.md` is the long-term **design standard**. It does not yet specify an AI application layer; v0.7 is an explicit, bounded blueprint amendment that applies its existing modular-monolith, provider-adapter, tenancy, jobs, audit, configuration, observability, testing and governance rules to AI.
- `IMPLEMENTATION_GUIDE.md` defines v0.1–v0.6 as the proven first usable template. v0.7 is a **supplementary post-foundation release**, not a revision of that first-usable-template claim. It adds reusable infrastructure only; individual AI product features remain domain modules in derived applications.
- This file is the **scoped contract for the v0.7 release**. It defines exact deliverables, exclusions, acceptance tests and commands. It also serves as a progress log: check items off only after the implement → review → apply-and-commit loop.
- `plans/AI_LARGE_ATTACHMENTS_V0_8_PLAN.md` contains the proposed post-v0.7 large-file/provider-reference work. It is not part of the v0.7 release contract and cannot begin until the v0.7 persistence, retention and reference-only job foundations are reviewed and complete.

---

# 1. Goal of v0.7

A **reliable, provider-independent AI application capability**. After v0.7, a feature can request a named task such as `lease.extract_terms` and receive a validated, auditable result without importing an LLM SDK, selecting a model, formatting a provider request, parsing provider JSON, calculating cost or implementing retry logic itself.

The service chooses the prompt version and a compatible model from task requirements, organisation controls and the model registry. It supports small synchronous operations and durable Dramatiq work for costly or large operations. Bounded file attachments are a first-class input: application code supplies a private storage reference, workers resolve it to short-lived bytes, and capable adapters send it inline without exposing storage credentials or placing bytes on the job broker. Provider regions are explicit, validated deployment configuration; the service never silently routes across regions. It is deliberately an AI workflow layer, **not** an autonomous-agent or retrieval platform.

---

# 2. In Scope

```text
AI service abstraction and task contract
Versioned prompt registry
Task and model capability configuration
Provider adapters: OpenAI, Anthropic, Google Vertex AI, DeepSeek, Azure OpenAI, local/OpenAI-compatible
Bounded inline file attachments with capability-aware routing
Explicit provider-region configuration and residency documentation
Structured Pydantic outputs with repair and bounded retries
Capability/cost-aware model routing and provider fallback
Organisation AI settings, budgets and feature enforcement
Usage, cost, audit and privacy-safe output records
Dramatiq integration for long-running AI work
One demonstrable extraction/classification workflow
```

The v0.1–v0.6 foundation already provides typed settings, organisation-scoped authorisation, provider-neutral interfaces, private storage, durable jobs, audit records, Sentry/metrics and frontend query conventions. v0.7 builds on those conventions. `app/ai/` is a platform package, not a new generic business module; consuming feature modules keep their own routes, permissions, domain records and UX.

Explicit deliverables:

- **Application-facing contract**: `AIService.execute(request: AIRequest) -> AIResult` is the only supported entry point for application code. `AIRequest` carries `task`, text/messages or a private storage reference, optional `output_schema`, validated `organisation_id`, initiating `user_id` and bounded JSON-safe metadata (for example document, feature and workflow identifiers). A storage reference is resolved by the service/job boundary into a provider-neutral attachment; it is never rendered as if it were document content. Application code names a task, never a provider/model. Services, not routers, call it.
- **Task registry and prompts**: checked-in, versioned task definitions under `backend/app/ai/tasks/` and YAML prompt assets under `backend/app/ai/prompts/<domain>/`. A task records its canonical name, prompt name/version, input variables, required capabilities, parameter defaults, output schema import path, retry policy and permitted fallback policy. Prompt files declare name, immutable integer version, system instructions, variables and output contract. The registry validates all definitions at startup/CI; missing variables, duplicate task names/versions and unsafe prompt/schema references fail fast. Prompt versions are append-only: correcting a prompt creates `*_vN.yaml`, not an edit to a released version.
- **Model registry and routing**: a checked-in model registry declares provider, model identifier, capabilities (structured output, vision, documents, tools, reasoning), context window, attachment ceilings, supported parameters, pricing basis and availability state. The router resolves **task requirements → organisation policy/override → eligible model → ordered fallback**, rejecting a model that cannot meet hard requirements or attachment limits. Routing considers capability, configured quality/latency tier and an optional maximum estimated/request cost; it never silently falls back across a provider when the task or organisation disallows it. Initial routing is deterministic configuration policy, not an optimisation/ML system.
- **Provider boundary**: `app/ai/providers/base.py` defines typed `LLMProvider`, `ProviderRequest`, bounded `Attachment` and normalised `ProviderResponse` / error taxonomy. `Attachment` carries a validated display name, MIME type, bytes and SHA-256 digest; the default template caps are 5 MB per attachment and 10 MB combined. Provider SDKs and provider-specific HTTP format, authentication, attachment mapping, streaming mechanics, errors, token reporting and model quirks are confined to adapters. No code outside `app/ai/providers/` imports OpenAI, Anthropic, Google, Azure or DeepSeek SDKs. A fake deterministic provider is the default test adapter.
- **First-class adapters**: real adapters for OpenAI, Anthropic Claude, DeepSeek, Azure OpenAI, and Google Gemini **through Vertex AI only**; no Gemini Developer API / Google AI Studio API key path is introduced. OpenAI/Azure, Anthropic and Vertex map supported bounded attachments to their native inline request forms; local adapters declare only the modalities they actually support and DeepSeek rejects attachments. `VertexAIProvider` uses Google Cloud Application Default Credentials or a workload-identity/service-account credential supplied through the approved deployment secret mechanism, plus project/location settings. A `LocalOpenAICompatibleProvider` targets a configured private OpenAI-compatible endpoint (Ollama, vLLM or SGLang) and is never exposed to browsers. DeepSeek remains its own adapter despite API compatibility. Each adapter declares the capabilities it actually supports rather than pretending providers are interchangeable.
- **Regional control**: OpenAI region and Anthropic inference geography are typed, validated settings; Azure region remains inherent in its configured resource endpoint; Vertex remains pinned by `AI_VERTEX_LOCATION`; DeepSeek documents that it offers no template-controlled regional pinning; local/fake providers inherit their operator-controlled location. Defaults must be honest for ordinary accounts—regional endpoints that require provider approval are explicit opt-ins—and unsupported regions fail configuration validation. Provider fallback never changes region implicitly, and routing metadata records the configured/observed region where the provider exposes it without increasing label cardinality.
- **Structured outputs**: Pydantic is the internal contract. The service asks an adapter for native structured output where supported; otherwise it uses the documented JSON mode/prompt contract, extracts a JSON object, validates it with the requested Pydantic model and records a validation-safe failure. On malformed output it may perform one bounded repair request using the same approved routing/policy path, then bounded task retries; it never returns unvalidated structured data as success. Free text remains available only when a task declares a text result explicitly.
- **Configuration and secrets**: settings include provider enablement and endpoint/project/deployment identifiers only; keys, Azure credentials and Google credential material remain server-side secrets. The environment selects the fake provider for normal tests. Production fails fast if an enabled provider lacks its required configuration, if a local provider endpoint is insecure/publicly reachable, or if the configured default/fallback cannot satisfy declared task requirements. The API/frontend never receive provider credentials or raw provider configuration.
- **Organisation controls**: `organisation_ai_settings` (one row per organisation) holds `enabled`, `allowed_provider_ids`, `allowed_model_ids`, optional provider/model override, monthly budget, retention policy and updated metadata; provider/model IDs are validated against the registry. AI is default-off for new organisations. Platform-managed settings use the existing organisation-feature/configuration and platform-plane conventions; request-time enforcement occurs in `AIService`, never only in a router/UI. A task-level opt-in may be required by a feature, but no new broad `ai.*` organisation permission is added until a second user-facing AI management use case proves it (rule of three).
- **Usage, cost and audit**: every attempted provider execution creates an `ai_requests` record with id, organisation/user ids, task, provider, model, prompt name/version, routing reason, input/output token counts, calculated cost in `NUMERIC`, latency, status, safe error code and timestamps. `ai_outputs` links to the request and stores validated output plus an input/output reference or cryptographic digest—not full sensitive source text by default—along with `human_rating`, `approved` and timestamps. Retaining redacted input/output content is an explicit task + organisation retention-policy choice, has a documented retention/deletion path, and must not put sensitive content in logs, Sentry or audit metadata. Audit events identify actor, task, request id, routing decision, completion/failure and budget denial; they do not duplicate prompts or document contents.
- **Budgets and observability**: before dispatch, the service enforces enabled state, allowed provider/model, task policy and monthly organisation budget using committed successful/attempt cost according to a documented reservation policy. Concurrent requests use a transaction-safe reservation or lock so a budget cannot be materially overrun. AI metrics cover requests, latency, success/failure, tokens, cost, validation failures, retries, budget denials and provider fallback, labelled only with low-cardinality task/provider/model identifiers. Logs bind `ai_request_id`, `task`, provider/model and existing request/job/organisation context, following BP §28's never-log list.
- **Jobs and demonstration workflow**: `execute` supports small bounded synchronous tasks; document-scale work uses `job_type="ai.execute"` on the existing `ai` queue and persists a request/job relationship before enqueueing. Job messages contain a private storage reference and bounded metadata, never file bytes; workers re-read the object on each idempotent attempt, validate size/MIME/digest before dispatch, and release bytes after the provider call. Keep-flow files remain owned by their feature; temporary analyse-only files use an organisation-scoped scratch namespace and the §6.5 retention/deletion job. The template includes one non-product demonstration task (`document.classify` with a small Pydantic schema and fixture prompt) invoked from a protected, organisation-scoped example endpoint or the existing file-processing seam; it proves task → prompt → attachment resolution → router → provider → validation → tracking/audit → result/job flow without pretending to solve commercial-property extraction.

---

# 3. Out of Scope (Explicitly Deferred)

| Capability | Deferred to |
| --- | --- |
| Autonomous agents, planning loops, tool execution, browser/computer use and agent memory | post-v0.7; a future orchestration layer may consume `AIService` |
| Retrieval, embeddings, chunking, vector-store management and RAG | post-v0.7; a retrieval service supplies context to an AI task and remains separate (BP §24) |
| Chat UI, conversational memory and real-time token streaming UI | first product that requires it |
| Fine-tuning, model training, evaluation dashboards, A/B experimentation and automatic model optimisation | post-v0.7; v0.7 only retains consented, privacy-safe evaluation records |
| Provider account provisioning, billing, marketplace procurement and key rotation automation | operations/provider administration |
| A generic AI administration frontend | post-v0.7; platform APIs/settings and documented procedures ship first |
| OCR, malware scanning, document parsing or a production lease-extraction feature | product/domain modules; v0.7 supplies only the integration seam and example task |
| Automatic PII detection/redaction engine | post-v0.7; v0.7 supplies an explicit redaction hook and safe retention/logging rules |
| Unbounded retries, provider racing, automatic cross-region routing or dynamic price scraping | explicitly excluded; routing/prices are reviewed configuration |
| Direct Google Gemini Developer API / Google AI Studio integration | explicitly excluded; Gemini access is Vertex AI only |
| Provider-hosted file uploads, Files API identifiers, signed/public URLs, direct `gs://` references and reusable external file handles | v0.8; v0.7 sends only bounded inline attachments after resolving private storage server-side |
| Attachments above 5 MB each or 10 MB combined, provider-specific large-file ceilings and automatic inline/reference-mode selection | v0.8; v0.7 uses one conservative template limit and fails before dispatch |

## 3.1 Boundary contract

```text
Feature service → AIService → task/prompt registry + model router → provider adapter
      │             │      │
      │             │      └→ ai_requests / ai_outputs / audit / metrics
      │             └→ ObjectStorage reference → bounded in-memory attachment
      └→ Retrieval service (optional future) → context supplied to AIRequest
```

The AI layer has no knowledge of vector databases or retrieval implementations. A future agent-orchestration layer sits above `AIService` and invokes approved tools through its own security model; it does not bypass task routing, organisation policy, tracking or audit.

---

# 4. Commands That Must Work

All v0.1–v0.6 commands remain quality gates. The default suite uses the fake provider and requires no commercial provider account. Provider-contract integrations are opt-in CI jobs and use only dedicated non-production credentials/projects.

```bash
make dev                 # existing local stack; fake AI provider by default
make worker              # existing Dramatiq worker, including the ai queue/tasks
make migrate             # creates/upgrades AI tables and constraints
make lint                # Ruff + ESLint/oxlint
make typecheck           # Pyright + vue-tsc
make test                # pytest/Vitest; fake provider only
make test-ai-contracts   # opt-in adapter contract tests against configured non-production providers
make generate-client     # regenerates API types, no drift
make e2e                 # Playwright critical journeys
make check               # full local gate, including task/prompt/model registry validation
```

`make test-ai-contracts` must skip cleanly with an explicit message when its non-production credentials are absent; it is required in protected CI only when those secrets and provider projects are deliberately configured. It must use a dedicated Google Cloud project/location and Vertex AI credentials, never a Gemini API key.

---

# 5. Acceptance Criteria

v0.7 is done when **all** of the following are true:

1. **Provider-independent task execution**: an example feature service calls `AIService.execute(task="document.classify", storage_reference=...)` and has no provider/model/SDK reference. The service resolves the private object to a bounded attachment rather than rendering its reference as content. The resolved result reports task, request id, validated output, safe usage/cost data and routing metadata. Repository checks prove provider SDK imports occur only under `app/ai/providers/`.
2. **Registry correctness and versioning**: the checked-in task, prompt and model registries validate at startup and in CI; duplicate/released prompt versions, missing template variables, unknown output schemas, incompatible task/model capabilities, attachment ceilings and unknown provider/model overrides fail with actionable errors. A task change can move between eligible OpenAI, Anthropic, Azure OpenAI or Vertex Gemini models through reviewed configuration without feature-code changes; document input cannot route to DeepSeek or another model lacking the `documents` capability.
3. **Adapters, attachments, region and the Google boundary**: OpenAI, Anthropic, DeepSeek, Azure OpenAI, Vertex AI Gemini and local/OpenAI-compatible adapters satisfy the same contract with normalised responses/errors and truthful capability declarations. Contract tests prove supported attachments use provider-native inline forms, unsupported modalities fail before dispatch, no private signed URL is generated, and OpenAI/Anthropic/Vertex/Azure regional configuration is honoured without automatic cross-region fallback. Fake-provider tests cover every adapter contract. Opt-in integration tests run each configured provider against a dedicated non-production account/project. Google tests authenticate to Vertex AI through ADC/workload identity or service-account credentials and assert that no Gemini Developer API endpoint/key setting exists.
4. **Structured result safety**: a valid provider JSON result becomes the declared Pydantic model; malformed JSON/schema failures trigger no more than one repair attempt and the task's bounded retry policy; terminal validation failure returns a safe domain error, updates the durable request/job state and records a non-sensitive failure. Invalid data is never returned as a successful structured result.
5. **Router and policy**: model selection satisfies task hard capabilities and attachment ceilings and only uses organisation-allowed providers/models; configured deterministic fallback is used only for eligible transient/provider failures and never silently changes a required region. Disabled AI, a forbidden override, unsupported capability or MIME type, oversized input, excessive estimated cost or exhausted budget is rejected before provider dispatch. Tests cover routing, fallback/no-fallback, attachment limits, pricing calculation and concurrent budget enforcement.
6. **Organisation isolation and settings**: Alembic migrations create `organisation_ai_settings`, `ai_requests` and `ai_outputs` with UUIDv7, UTC timestamp, foreign-key/index/constraint conventions; default AI state is off. All settings and request/output reads enforce the organisation boundary; cross-organisation ids return the established safe response. New protected routes join `PROTECTED_ROUTES`, including the platform cross-plane matrix where applicable, and human review is recorded for the tenant/configuration and secret-handling work.
7. **Tracking, privacy and auditability**: each attempt has an `ai_requests` row with the specified task/provider/model/prompt/usage/cost/latency/outcome data; `ai_outputs` stores a storage reference and cryptographic digest, never attachment bytes. Output and scratch-file retention respect the organisation policy; keep-flow objects remain owned by their feature. Ordinary source content, prompts, provider headers/keys and raw responses do not appear in logs, Sentry or audit metadata. Tests assert redaction and retention/deletion behaviour; audit records identify who initiated each AI action and its result without leaking content.
8. **Background work**: document-scale AI execution creates a durable job and AI request before enqueueing to the `ai` queue, with a storage reference rather than file bytes in the broker message. The worker re-reads and revalidates the object on retry, survives a retry without duplicate terminal output/cost record, updates progress/status and can be polled through the existing jobs API. Small bounded execution stays synchronous only under documented input/time limits. Worker metrics/log context include `job_id` and `ai_request_id`; worker memory is bounded by attachment limits and configured concurrency.
9. **Configuration and operations**: `.env.example`, deployment examples and runbooks document every `AI_*`, provider region/inference geography and Vertex setting without credential material; production validation rejects incomplete/misconfigured enabled adapters, unsupported regions, test/fake providers and publicly unsafe local endpoints. Documentation distinguishes configured endpoint location from contractual data-residency guarantees. Cost/pricing data has an owner, effective date and change-review procedure. Sentry/metrics dashboards and alerts cover provider failures, validation failures, queue backlog, budget denials, spend and latency.
10. **Governance and release quality**: ADRs record the provider-neutral AI layer/routing contract and the Vertex-only Google decision; `ARCHITECTURE.md`, `API_CONVENTIONS.md`, `SECURITY.md`, README and the blueprint are amended; all dependencies are pinned/justified; `make check`, registry validation, migrations, generated-client drift, AI fake-provider tests and relevant opt-in contracts are green. An architecture review finds no CRITICAL or MAJOR issue.

---

# 6. Progress Log

Check items off only after review. Work is ordered so contracts and safety controls exist before real providers and the demonstration workflow.

## 6.1 Blueprint Amendment, ADRs and Core Contracts

- [x] Amend BP §3, §4, §5, §18, §23, §27, §28, §31–§33 and `ARCHITECTURE.md` to establish AI as a provider-neutral platform capability; retain the modular monolith and no-direct-SDK rules
- [x] ADR-0017: provider-neutral AI service, task-based routing, prompt/version lifecycle, structured-output contract, cost/retention boundaries and why this is not an agent framework
- [x] ADR-0018: Google Gemini through Vertex AI only (ADC/workload identity/service account), with Gemini Developer API/AI Studio excluded; document regional/data-residency and credential consequences
- [x] `app/ai/` package skeleton: typed request/result/error schemas, `AIService`, task/model/prompt registry interfaces and `FakeLLMProvider`; no public HTTP endpoint yet
- [x] Tests: service contract, fake-provider determinism and an import-boundary/architecture test preventing provider SDK imports outside `app/ai/providers/`
- [x] v0.7 attachment/regional amendment: update ADR-0017, ADR-0018, the blueprint and `ARCHITECTURE.md` with the bounded inline attachment contract, storage/lifecycle ownership, regional configuration truthfulness and the v0.8 large-file boundary before implementing the remaining amendment work

## 6.2 Prompt, Task and Model Registries

- [x] Versioned YAML prompt registry under `app/ai/prompts/` and checked-in task/model configurations under `app/ai/tasks/` / `app/ai/models/`; definitions include all fields in §2 and carry explicit schema identifiers
- [x] Safe template rendering: allowlisted variables, no arbitrary template execution, length/input limits and no secret interpolation; startup/CI validation for duplicate versions, unresolved variables and incompatible requirements
- [x] Initial `document.classify` task + Pydantic output schema + fixture prompts, models and test data; use only non-sensitive sample content
- [x] Deterministic capability/cost router with ordered fallback, context/input budget calculation and reviewed pricing metadata
- [ ] v0.7 attachment amendment: add `documents` capability and per-model inline attachment ceilings; reject incompatible modality, MIME type and size combinations before provider dispatch

## 6.3 Provider Adapters and Configuration

- [x] `LLMProvider` contract and normalised provider request/response/error taxonomy (including token/latency data, retryability and structured-output capability)
- [x] OpenAI, Anthropic, DeepSeek and Azure OpenAI adapters; SDK/HTTP client imports isolated; provider-specific models, endpoint/deployment naming and errors contained
- [x] `VertexAIProvider` for Gemini through the Vertex AI API only; settings for Google Cloud project/location and approved server credentials; no `GEMINI_API_KEY`, Google AI Studio or developer-API endpoint implementation
- [x] `LocalOpenAICompatibleProvider` for privately reachable Ollama/vLLM/SGLang-compatible servers, with explicit TLS/network/allowlist safeguards; fake remains the test default
- [x] Typed `AI_*` settings, provider factories and production fail-fast validation; pin/document dependencies and add opt-in non-production adapter-contract CI jobs
- [ ] v0.7 attachment amendment: add provider-neutral `Attachment` and inline mappings for supported OpenAI/Azure, Anthropic and Vertex requests; local/DeepSeek reject unsupported documents and no adapter receives a private storage credential or signed URL
- [ ] v0.7 regional amendment: add validated OpenAI region and Anthropic inference-geography settings, retain endpoint-declared Azure and location-declared Vertex regions, document providers without pinning, and prohibit implicit cross-region fallback

## 6.4 Structured Outputs, Retry and Safety Controls

- [ ] Native structured-output path plus JSON extraction/Pydantic validation fallback; text result opt-in is explicit in the task definition
- [ ] Bounded repair attempt and retry policy separated between malformed output, transient provider error and permanent validation/policy failure; no retry storm or unbounded cost
- [ ] Input normalisation, max-size/context checks and redaction hook before external dispatch; resolve private storage references into validated attachments, enforce 5 MB per-file / 10 MB combined limits, compute SHA-256 digests and propagate only approved metadata to adapters
- [ ] Unit/integration tests for successful validation, malformed output, repair success/failure, timeout/rate-limit error translation, idempotency and no content leakage

## 6.5 Organisation Controls, Persistence and Audit

- [ ] Alembic migration and models/queries/services for `organisation_ai_settings`, `ai_requests` and `ai_outputs`; UUIDv7, UTC, `NUMERIC` cost, foreign keys, indexes and check constraints; persist input/output storage references and digests where applicable, never attachment bytes
- [ ] Platform-gated organisation AI-settings management API with explicit response schemas; default-off state, allowed-provider/model lists, override, budget and retention controls; add all routes to the security matrix
- [ ] Service-enforced policy/budget reservation and monthly accounting; cross-org, disabled, forbidden-model and exhausted-budget tests
- [ ] Privacy-safe retention/deletion jobs and audit event catalogue; records contain references/digests by default, not document contents; keep-flow objects remain feature-owned and temporary analyse-only objects use the organisation-scoped AI scratch namespace

## 6.6 Jobs, Example Integration and API/Frontend Surface

- [ ] `ai.execute` Dramatiq queue/task integrated with the durable jobs service; request/job linkage, idempotency key, bounded concurrency/timeouts, status/progress and retry handling; messages carry storage references rather than bytes and every retry re-reads/revalidates the referenced object
- [ ] Protected organisation-scoped demonstration endpoint/service for `document.classify` (sync only within limits; async otherwise), explicit response schemas and security-suite coverage; no generic arbitrary-prompt endpoint
- [ ] Generated client and `src/queries/` composables only where the demonstration requires polling/result display; no component or Pinia store imports the API client directly
- [ ] Tests: synchronous and queued paths, polling, organisation isolation, audit/usage rows, broker payload contains no attachment bytes, retry re-reads storage, keep/temporary lifecycle behavior, and a mocked Playwright journey if a UI is introduced

## 6.7 Operations, Documentation and Release Governance

- [ ] Metrics/logging/Sentry instrumentation with safe low-cardinality labels and `ai_request_id`; dashboards/alerts and a runbook for provider outage, budget response, prompt rollback, model rollback and retention deletion
- [ ] `.env.example`, `.env.production.example`, README and deployment docs describe secret injection, provider enablement, provider regions/inference geography, Vertex project/location/identity, attachment limits and lifecycle, local-provider network controls and non-production contract-test credentials
- [ ] `SECURITY.md` covers prompt injection as untrusted input, external-data disclosure/redaction, provider data handling, output validation, audit/retention, no client credentials and approval boundaries
- [ ] Dependencies, migrations, provider credentials/secrets, platform configuration/tenant isolation and any public endpoints receive recorded human review; CI/quality gate green and architecture audit clean

---

# 7. Blueprint Reference Map

The blueprint has no AI-specific section before this release. The v0.7 blueprint amendment becomes the authoritative AI material; until then, implementers read only the existing sections below for the relevant concern.

## Disambiguating section numbers

- **Scope §6.x** is a subsection of this document.
- **BP §N** is a section of `Internal_Custom_Application_Starter_Architecture_v2.md`.

Never use an unprefixed section number in new AI code/docstrings. New citations use `v0.7 Scope §6.x` or `BP §N`.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Contracts and ADRs | **BP §2** (lines 40–58), **BP §4–§6** (lines 131–268), **BP §34** (lines 1811–1837) | Modular-monolith boundary, service/router responsibilities, adapter rule and ADR format |
| **Scope §6.2** Registries and routing | **BP §7** (lines 270–383), **BP §10** (lines 496–575), **BP §27** (lines 1423–1470) | Pydantic separation, JSONB limits, typed configuration and fail-fast configuration |
| **Scope §6.3** Providers/configuration | **BP §17** (lines 851–974), **BP §23** (lines 1285–1342), **BP §32–§33** (lines 1707–1810) | Provider-neutral adapter precedent, adapter ownership, dependencies and human review |
| **Scope §6.4** Structured outputs/safety | **BP §7** (lines 270–383), **BP §13** (lines 669–717), **BP §28** (lines 1472–1513) | Pydantic validation, safe error translation, Sentry/logging and never-log rules |
| **Scope §6.5** Organisation controls/tracking | **BP §9–§11** (lines 385–595), **BP §27–§29** (lines 1424–1561) | Tenant boundary, database/transaction conventions, organisation feature enforcement and audit events |
| **Scope §6.6** Jobs/API/frontend | **BP §12** (lines 597–667), **BP §14–§16** (lines 719–849), **BP §18** (lines 975–1075), **BP §26** (lines 1397–1422) | Explicit API schemas, frontend query boundary, durable jobs/ai queue and polling |
| **Scope §6.7** Operations/governance | **BP §28** (lines 1472–1513), **BP §31–§33** (lines 1640–1810), **BP §37–§38** (lines 2003–2067), **BP §42** (lines 2180–2201) | Metrics/log security, test strategy, review rules, deployment/environment separation and template validation |

---

# 8. Status

```text
Release:    v0.7.0 (AI / LLM application service layer)
State:      planned
Started:    —
Completed:  —
```

When every acceptance criterion in §5 is met and every §6 box is checked after review, update the version recording in `pyproject.toml` and `frontend/package.json`, tag `v0.7.0`, and record the completed blueprint amendment/versioned references.
