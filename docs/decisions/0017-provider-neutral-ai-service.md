# ADR 0017: Provider-Neutral AI Service, Task-Based Routing, Prompt/Version Lifecycle, Structured-Output Contract, Cost/Retention Boundaries

Status: Accepted

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
