# ADR 0018: Google Gemini through Vertex AI Only (No Gemini Developer API / Google AI Studio)

Status: Accepted (amended 2026-08-10: regional configuration truthfulness and in-region attachments; v0.7 Scope §6.1/§6.3)

## Context

The v0.7 AI layer must offer a Google Gemini adapter. Google exposes Gemini
through two materially different paths:

1. **Gemini Developer API / Google AI Studio** — a consumer-facing API keyed
   (`GEMINI_API_KEY`) endpoint that is quick to start but is not designed for
   enterprise data handling: no per-tenant credential scoping, weaker
   data-residency guarantees, and a key model that invites secrets leaking into
   frontend configuration.
2. **Vertex AI (Gemini)** — Google Cloud's production platform: models run in a
   dedicated Google Cloud project/location with workload-identity or
   service-account credentials, VPC/SCP controls, data-residency and regional
   endpoints, and full IAM (per-project, per-service-account) scoping.

The template's deployment profiles (managed Azure and portable VPS, ADR-0007)
and its security rules (no client credentials, server-side secrets only,
tenant isolation) make the developer-API path an outlier: it would be the only
integration that authenticates with a bare API key rather than a managed
credential, and it cannot express the regional/data-residency controls the
template's production guidance requires.

## Options considered

### 1. Gemini Developer API with `GEMINI_API_KEY` (rejected)

Simplest to demonstrate, but: a plain API key with no IAM scoping, no per-tenant
credential isolation, weaker residency guarantees, and a documented pattern of
leaking the key into browser bundles. It also duplicates Vertex's model surface
under a different protocol, so a later migration would be a rewrite rather than
an adapter change. Rejected.

### 2. Both Vertex AI and the Developer API (rejected)

Two Google paths means two SDKs, two auth models and two test matrices for one
provider. It would also keep the insecure key path alive in the template for no
feature benefit. Rejected.

### 3. Vertex AI only, via ADC / workload identity / service account (adopted)

Gemini access is **Vertex AI only**. The `VertexAIProvider` authenticates
through Google Cloud Application Default Credentials or a workload-identity /
service-account credential supplied through the approved deployment secret
mechanism, plus configured project/location settings. No `GEMINI_API_KEY`
setting, no Google AI Studio / developer-API endpoint implementation exists
anywhere in the template, and an opt-in test asserts this.

## Decision

**Google Gemini is reached exclusively through the Vertex AI API.** The
adapter requires Google Cloud project and location settings plus server-side
credentials (ADC, workload identity, or a service-account key injected via the
deployment's secret mechanism). Region/location is explicit configuration so
deployments can pin a data-residency location. The Gemini Developer API and
Google AI Studio are excluded; a repository test asserts no developer-API
endpoint or `GEMINI_API_KEY` setting exists, and the opt-in contract test runs
against a dedicated Google Cloud project/location only.

## Consequences

- Google credential material is server-side only: never in the frontend, never
  in logs or `ai_requests` records, never in `.env.example` with a value.
- Deployments must have a Google Cloud project, a chosen location (data
  residency), and a workload identity / service account; the runbook and
  deployment docs cover the exact mechanism for both managed and VPS profiles.
- Regional endpoints mean the adapter must honour the configured location when
  constructing requests; cross-region failover is explicitly out of scope
  (routing/prices are reviewed configuration).
- The opt-in `test-ai-contracts` CI job uses dedicated non-production Google
  credentials and a dedicated project/location; it skips cleanly when those are
  absent.

### Amendment (v0.7 regional configuration truthfulness, Scope §6.1/§6.3)

- **Explicit region everywhere**: the same "region is reviewed configuration,
  never implicit" rule that pins Vertex to its location setting applies to
  every provider. OpenAI region and Anthropic inference geography are typed,
  validated settings; Azure's region is inherent in its configured resource
  endpoint; DeepSeek documents that it offers no template-controlled regional
  pinning; local and fake providers inherit their operator-controlled
  location. Defaults must be honest for ordinary accounts — regional endpoints
  that require provider approval are explicit opt-ins — and unsupported
  regions fail configuration validation.
- **No implicit cross-region fallback**: routing/fallback never moves a request
  across regions, and provider fallback never changes region implicitly.
  Routing metadata records the configured or observed region only where the
  provider exposes it, without increasing label cardinality.
- **In-region attachments**: v0.7 inline attachments resolved from storage are
  sent only to the provider endpoint the deployment actually configured; for
  Vertex this means the adapter serves attachments through the same
  project/location that the request targets. No attachment path stages bytes
  to, or reads from, another region, and no adapter receives a signed URL or
  storage credential (large-file and provider-reference modes are deferred to
  v0.8).

---
