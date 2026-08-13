# Reusable Lease Intelligence Package Plan

Status: Draft

## Goal

Create a separately versioned Python package that lets applications built from
the completed v0.8 template reuse a well-defined lease-abstraction capability:
immutable, reviewed prompts; provider-neutral structured extraction contracts;
deterministic normalisation and validation; and a privacy-safe evaluation
harness. A consuming application installs a pinned package release, explicitly
registers the capability with its own `AIService`, and may add client-owned
profiles, rules and workflows without editing or forking package code.

The package is a domain capability, not part of the generic application
template. It must remain independently releasable so improvements can be
adopted by multiple client applications through controlled dependency-update
pull requests.

## Agreed scope

- A dedicated distribution named `lease-intelligence` with import namespace
  `lease_intelligence`, hosted outside the base-template source tree.
- Python 3.13+, Pydantic 2, `src/` packaging, strict typing, Ruff, Pyright,
  pytest, reproducible `uv` locking, wheel/sdist builds and semantic versions.
- A provider-neutral `LeaseAbstract` result schema with explicit schema version,
  prompt version and package version provenance.
- Fact extraction rather than legal conclusions. Every material extracted fact
  carries its normalised value, bounded supporting evidence, source page where
  available, confidence/review state and an explicit absent/ambiguous state.
- Immutable, package-resource prompt versions and task metadata compatible with
  the template's named-task, structured-output and document-capability model.
- Deterministic normalisers and cross-field validators for dates, terms, money,
  frequencies, parties and clause facts. Model output is never accepted without
  Pydantic validation and the package validation pipeline.
- Explicit extension points based on composition: named extraction profiles,
  field/section definitions, normalisers, validation rules and post-processors.
  Client extensions live in the client repository and never modify installed
  package resources.
- An explicit host-registration seam. The package exposes a typed capability
  manifest and resources; the derived application maps/registers them with its
  own `AIService`. There is no provider selection, provider SDK, storage access,
  database access, web framework or automatic plugin discovery in the package.
- A synthetic or properly licensed/redacted evaluation corpus, versioned golden
  abstracts, deterministic offline scoring and optional live-model evaluation.
- Field-level and document-level quality gates covering schema validity,
  required-field coverage, normalised-value accuracy, evidence support,
  unsupported-assertion rate and regression from an approved baseline.
- A reference consumer fixture proving installation, explicit registration,
  document-reference execution and client-owned extension without copying or
  patching the package.
- Release documentation, compatibility policy, changelog, migration notes and a
  controlled upgrade procedure for consuming applications.

## Findings and evidence

- The template already routes feature code through
  `AIService.execute(AIRequest(task=...))`; features do not select providers or
  construct provider requests (`TEMPLATE_V0_7_SCOPE.md` §2).
- `backend/app/ai/registry.py` already defines validated task and prompt models,
  immutable integer prompt versions, safe rendering, structured schema
  resolution and bundle validation. Its current production loader reads one
  in-application registry root, so an external capability requires an explicit
  composition/registration contract rather than copied YAML.
- `backend/app/ai/tasks/schemas.py`, `tasks/document_classify.yaml` and
  `prompts/document/classify_v1.yaml` demonstrate the current schema → task →
  prompt relationship but intentionally contain no production domain model.
- v0.8 supplies the bounded large-document transport modes needed by the first
  consumer while keeping storage references, provider uploads and signed URLs
  inside the host AI layer.
- Blueprint §43 explicitly excludes leases and other speculative commercial-
  property models from the base template. The reusable capability must
  therefore be a separate package consumed by derived applications.
- Prompt reuse without versioned schemas and evaluations would distribute
  regressions as quickly as improvements. Prompt, schema, normalisation and
  evaluation versions must be released as one tested unit.

## Out of scope

| Capability | Boundary |
| --- | --- |
| Adding lease models or prompts to the base template | prohibited by blueprint §43 |
| Client application database models, migrations or retention policy | owned by each consuming application |
| Client routes, permissions, tenancy, jobs, audit events or frontend | implemented and reviewed in the consuming application |
| Provider SDKs, credentials, model routing, file transfer or storage | supplied by the host application's v0.8 `AIService` boundary |
| OCR, document repair, page rendering, RAG, embeddings or vector stores | separate preprocessing/retrieval capabilities |
| Legal advice, enforceability opinions or autonomous decisions | package extracts reviewable facts only |
| A universal property/lease workflow or CRM | client-product concern |
| Runtime download of unreviewed prompts or automatic “latest” upgrades | prohibited; consumers pin reviewed releases |
| Client documents or production outputs in the shared repository | prohibited unless independently approved, redacted and licensed |
| Central multi-tenant lease-extraction service | reconsider only after a demonstrated operational need; library first |
| Automatic Python entry-point discovery | excluded initially; consuming apps register packages explicitly |

## Decisions and assumptions

The following decisions block `Status: Active` and must be recorded before P1
starts:

1. **v0.8 dependency:** `TEMPLATE_V0_8_SCOPE.md` is complete, reviewed and
   tagged, and its final task/attachment/registry interfaces have been checked
   against this plan.
2. **Repository and distribution:** approve the dedicated repository owner,
   package registry (recommended: private Python registry), distribution name,
   access model and release-signing/provenance policy. The package must not be
   implemented inside the base template.
3. **Initial legal/document boundary:** approve the first supported lease type,
   language and jurisdiction. Recommended v0.1 boundary: English-language UK
   commercial property leases, extracting express document facts only.
4. **Canonical field contract:** a lease-domain reviewer approves the v0.1
   sections and required/optional fields before schemas or prompts are frozen.
   The recommended sections are document identity, parties/roles, premises,
   execution/commencement/expiry, term, rent/payment, rent review, break rights,
   deposits/guarantees, repair, insurance, service charge, use, alienation and
   notices/governing-law facts.
5. **Host registration contract:** approve explicit registry composition as the
   integration model. If v0.8 cannot compose package-provided task/prompt/schema
   resources without copying them, create and complete a separate generic
   app-template extension-seam plan before activating this plan.
6. **Evaluation data:** approve corpus ownership/licensing, redaction standard,
   storage location and who may access it. Shared-package CI uses only synthetic
   or distributable fixtures; client-confidential regression sets remain in
   client repositories.
7. **Quality gate and live-eval budget:** a domain reviewer approves metric
   definitions, per-section thresholds, the reference provider/model matrix and
   the maximum spend for manual/scheduled live evaluations.

Assumptions once those decisions are approved:

- Package releases are immutable and follow semantic versioning. A schema-
  incompatible change is a major version; a new prompt or compatible optional
  field is a minor version; implementation-only fixes are patches.
- Released prompt files are append-only. Improvements add `*_vN.yaml`; they do
  not edit the bytes of an already released prompt version.
- Consuming applications pin an exact compatible version and upgrade through a
  reviewed pull request with their own integration and client regression tests.
- Client customisation uses named profiles and registered strategies. Monkey
  patching, editing site-packages and unconstrained prompt concatenation are not
  supported extension mechanisms.

## Commands that must work

P1 creates these commands in the dedicated package repository; later
checkpoints keep them green:

```bash
make format
make lint
make typecheck
make test
make test-evals-offline
make build
make check
```

Live evaluation is opt-in, credentialed and budget-bounded:

```bash
make test-evals-live
```

`make check` must run formatting verification, lint, strict typing, unit tests,
offline evaluation validation/scoring and package build without network access
or provider credentials. Live evaluation must skip cleanly when its explicit
enable flag or dedicated non-production credentials are absent.

## Acceptance criteria

1. A fresh Python 3.13 project can install a pinned wheel, explicitly register
   the package capability and request the canonical lease-extraction task
   through a fake host without importing any app-template or provider package.
2. The canonical v0.1 result is a strict Pydantic schema with immutable schema
   versioning, explicit absent/ambiguous states and evidence/confidence metadata;
   unknown fields and unvalidated model output fail closed.
3. The base prompt treats lease content as untrusted data, requests only the
   declared factual contract, forbids invented values/legal conclusions and is
   tied to an immutable task, prompt and schema version.
4. Normalisation and cross-field validation are deterministic and independently
   unit tested. Original extracted text/evidence remains available so a
   consuming application can show why a normalised value was produced.
5. A client can add a named profile, fields and validation/post-processing rules
   in its own repository without modifying package code; invalid or colliding
   extensions fail during registration.
6. The offline evaluation runner validates corpus/golden-data schemas and emits
   deterministic JSON containing field/document metrics, slice results,
   unsupported-assertion counts and baseline deltas with a non-zero exit on a
   failed approved threshold.
7. Live evaluations use only approved non-production provider configuration,
   record package/schema/prompt/model versions and cost, never log document or
   prompt content, and cannot run accidentally as part of ordinary unit tests.
8. No package runtime module imports FastAPI, SQLAlchemy, Dramatiq, boto3,
   app-template modules or an AI provider SDK; import-boundary tests enforce the
   dependency direction.
9. The built wheel/sdist contain every declared prompt/schema resource, exclude
   secrets and non-distributable documents, install into a clean environment
   and reproduce the reference-consumer result/eval contract.
10. Release notes define compatibility and upgrade behavior. No package is
    published until domain, security/data and package-owner review approve the
    release candidate and all required commands are green.

### Capability traceability

| Observable requirement | Acceptance | Checkpoint | Consumer surface | Required evidence |
| --- | --- | --- | --- | --- |
| Installable reusable capability | AC1, AC8–AC10 | P1, P4, P6 | Python distribution and explicit `LeaseCapabilityPack` registration | Clean-install, import-boundary and reference-consumer tests |
| Stable lease abstract | AC2, AC4 | P2 | `LeaseAbstract` and versioned value/evidence models | Schema snapshots, invalid-input and normalisation tests |
| Versioned extraction prompt | AC3 | P3 | `lease.extract.v1` capability resources | Resource checksum, registry, injection and structured-output tests |
| Client extension without fork | AC5 | P4 | named profiles/rules/post-processors | example client extension and collision/failure tests |
| Measurable extraction quality | AC6–AC7 | P5 | offline CLI/JSON report and opt-in live runner | golden corpus, metric unit tests, threshold and cost-bound tests |
| Controlled shared updates | AC9–AC10 | P6 | wheel/sdist, changelog and compatibility matrix | reproducible build, clean install and release checklist |

## Implementation checkpoints

### P1 — Package Contract and Repository Foundation

Dependencies: all activation decisions above; completed and tagged app template v0.8

- [ ] Create the dedicated `lease-intelligence` repository with `src/lease_intelligence/`, `tests/`, `evals/`, `docs/decisions/`, `examples/`, `pyproject.toml`, `uv.lock`, Makefile, CI, licence/access metadata, changelog and contributor/security guidance; document every dependency and keep runtime dependencies to the approved minimum.
- [ ] Record an ADR covering library versus service, package ownership, explicit host registration, dependency direction, semantic/version compatibility, release provenance and why lease-domain code remains outside the base template.
- [ ] Define and test the provider/storage/framework-neutral `LeaseCapabilityPack` manifest contract, package/schema/prompt version metadata and `importlib.resources` loading; malformed, duplicate or incomplete resources fail with bounded safe errors.
- [ ] Implement the commands in `Commands that must work`, a clean-wheel install smoke test and import-boundary tests proving runtime code imports no host application or provider SDK.

Human review required before application: major dependency additions, package/repository governance and externally distributed public Python API.

### P2 — Canonical Lease Abstract and Deterministic Validation

Dependencies: P1

- [ ] Define strict, documented Pydantic contracts for the approved v0.1 lease sections, typed extracted-value states, bounded evidence/page references, confidence/review flags and package/schema/prompt provenance; forbid unknown fields and distinguish absent, not-applicable and ambiguous values.
- [ ] Implement pure deterministic normalisers for approved date, duration, currency/amount, frequency, party/role and clause-value types while preserving raw extracted values and evidence.
- [ ] Implement cross-field validation and review warnings for contradictions such as term/date mismatch, unsupported normalisation, missing evidence, impossible ranges and mutually incompatible clause states; never turn a warning into an invented fact.
- [ ] Add schema snapshots, JSON round-trip tests, boundary/property-oriented test cases and a schema-version compatibility fixture approved by the lease-domain reviewer.

Human review required before application: lease-domain field/evidence contract and any legal-risk wording.

### P3 — Versioned Prompt and Extraction Pipeline

Dependencies: P2

- [ ] Add immutable `lease.extract` v1 task/prompt resources tied to the canonical schema; explicitly delimit the document as untrusted content, prohibit instruction-following from the lease, require evidence for material facts and require explicit absent/ambiguous output rather than guesses.
- [ ] Implement package APIs that expose the reviewed capability resources and validate raw structured results through the schema, normalisation and cross-field pipeline; the package accepts no provider/model name, credential, URL, storage object or unvalidated free-text success path.
- [ ] Add deterministic fake-executor fixtures covering success, missing sections, ambiguity, conflicting terms, malformed JSON/schema output, prompt-injection text, excessive evidence and validation warnings.
- [ ] Enforce append-only prompt/version checks using checked-in checksums or release manifests so modifying a released prompt/schema fixture fails CI and requires an explicit new version.

Human review required before application: AI prompt safety, structured extraction behavior, domain wording and output-retention guidance.

### P4 — Consumer Registration and Extension Contract

Dependencies: P3 and the final v0.8 host interface

- [ ] Implement explicit registration/composition APIs for named profiles, field sections, normalisers, validation rules and post-processors with deterministic ordering, namespace ownership and duplicate/collision rejection; avoid inheritance from internal concrete classes.
- [ ] Create a reference consumer fixture representing a v0.8-derived application: it registers the packaged task/prompt/schema with a fake `AIService`-compatible host, passes only a private document reference and receives a validated `LeaseAbstract`.
- [ ] Add a client-owned example profile in the fixture that introduces one namespaced field and rule without editing package resources; prove core package upgrades preserve the extension or fail with a clear compatibility error.
- [ ] Document the consuming-app responsibilities: package pin, AI task policy, document ownership/retention, persistence, migrations, routes, permissions, jobs, audit, UI and client-confidential regression tests. Provide no copy-and-edit installation path.

Human review required before application: externally consumed extension API and compatibility contract.

### P5 — Evaluation Corpus, Metrics and Regression Gates

Dependencies: P2–P4 and approved evaluation-data policy

- [ ] Define versioned corpus and golden-abstract manifests with licence/provenance, document hash, permitted use, complexity/slice labels and schema version; reject unapproved, unredacted or mismatched fixtures before evaluation.
- [ ] Implement deterministic offline metrics for schema validity, required-field coverage, exact/normalised values, evidence page/quote support, absent/ambiguous handling, unsupported assertions and per-section/document pass rates; unit test every scorer edge case.
- [ ] Add an offline CLI/report contract that emits stable machine-readable JSON, compares against an approved baseline and fails on configured overall, critical-field, slice or regression thresholds without exposing document/prompt content.
- [ ] Add an opt-in live-evaluation adapter contract using a dedicated non-production host, explicit enable flag and cost/request ceilings. Reports record package/schema/prompt/provider/model versions, latency/tokens/cost and safe case identifiers; ordinary CI skips it cleanly.
- [ ] Establish the initial reviewed baseline and publish a concise evaluation card describing corpus limits, known failure modes, thresholds and the human-review requirement for low-confidence or legally consequential fields.

Human review required before application: evaluation-data licensing/privacy, provider credential handling, spend limits and domain acceptance thresholds.

### P6 — Release, Upgrade and Reference Adoption

Dependencies: P1–P5

- [ ] Build wheel and sdist reproducibly, inspect package contents, scan dependencies/secrets, install into a clean Python 3.13 environment and run the reference consumer plus offline evaluation gate against the built artifact.
- [ ] Document semantic-version rules for schema, prompt, normalisation and extension changes; add deprecation/compatibility policy, migration notes, rollback instructions and a compatibility matrix beginning with app template v0.8.
- [ ] Prepare a v0.1.0 release candidate and demonstrate a consuming application upgrading between two test package versions through a pinned dependency change with its core and client regression suites green.
- [ ] Publish only after explicit package-owner, lease-domain and security/data approval; tag the exact reviewed commit, generate release notes/provenance and verify a fresh consumer can install the immutable version from the approved private registry.

Human review required before application: external package publication, registry credentials, release provenance and compatibility commitment.

## Reference map

| Checkpoint | Governing sources | What to extract |
| --- | --- | --- |
| P1 | `Internal_Custom_Application_Starter_Architecture_v2.md` BP §2–§5, §32–§34, §42–§43; `AGENTS.md`; `CONTRIBUTING.md`; `backend/pyproject.toml` | explicit boundaries, Python/tooling baseline, dependency/ADR/review rules, template validation and prohibition on lease code in the template |
| P2 | `TEMPLATE_V0_7_SCOPE.md` §2 structured-output contract, §3 domain exclusion; `backend/app/ai/tasks/schemas.py`; BP §30, §43 | strict Pydantic validation, safe factual outputs, file/content security and domain ownership |
| P3 | `backend/app/ai/registry.py`; `backend/app/ai/service.py`; `backend/app/ai/tasks/document_classify.yaml`; `backend/app/ai/prompts/document/classify_v1.yaml`; BP §23, §28 | immutable task/prompt contract, safe rendering, provider-neutral execution, structured validation and never-log rules |
| P4 | `backend/app/ai/registry.py`; `TEMPLATE_V0_8_SCOPE.md` §2.2–§2.5; BP §2–§5, §33, §43 | registry composition constraints, final attachment host boundary, extension direction and separation from client modules |
| P5 | `TEMPLATE_V0_7_SCOPE.md` §2 usage/evaluation metadata and §3 evaluation exclusion; BP §28, §31; `backend/tests/test_ai_fake_provider.py`; `backend/tests/test_ai_contracts.py` | privacy-safe evidence, provider-free default tests, opt-in live contracts, deterministic fixtures and observability limits |
| P6 | BP §32–§34, §42; `CONTRIBUTING.md`; approved P1 ADR and package registry documentation | build/release gates, dependencies, reviews, provenance, clean-consumer validation and controlled upgrades |

Before activation, re-verify all moving-code references against the tagged v0.8
template and replace them with the exact tag/commit used for the compatibility
baseline.

## API, data and security impact

- **Public package API:** `LeaseCapabilityPack`, `LeaseAbstract`, the extracted-
  value/evidence contracts, profile/rule protocols and evaluation report schema
  become versioned consumer APIs. Breaking changes require a major version.
- **Application API/frontend:** none in this plan. Every consuming application
  separately defines explicit API schemas, generated frontend types and UX.
- **Database/migrations:** none in this package. Consumers own persistence and
  Alembic migrations; package Pydantic models are never ORM models.
- **Tenancy/permissions:** the package has no organisation or user context and
  grants no access. The consuming service validates tenancy/permissions before
  invoking its `AIService` and before retaining/displaying results.
- **Documents:** the package receives neither storage credentials nor arbitrary
  URLs. The host supplies the approved document reference through its own v0.8
  attachment path. Evaluation fixtures follow the approved data policy.
- **Secrets/providers:** no runtime provider credentials or SDKs. Live evals use
  an injected host adapter and dedicated non-production secrets excluded from
  reports, logs and artifacts.
- **Legal risk:** output is labelled factual extraction requiring human review,
  not advice or an enforceability determination. Evidence and uncertainty are
  first-class and cannot be discarded by the package API.

## Validation plan

- Run `make check` on every checkpoint after P1 creates it.
- Run all schema, normalisation, manifest, prompt-safety, extension and scoring
  tests without network access in ordinary CI.
- Build artifacts and run clean-environment installation tests on every pull
  request; inspect resources so missing YAML/schema files fail before release.
- Run live evaluations manually or on a protected schedule only after explicit
  enablement, with fixed request/cost ceilings and dedicated non-production
  credentials.
- Require client repositories to run their own confidential regression corpus
  before accepting an upgraded package version.
- At release, run dependency/secret scans, offline evaluations, approved live
  model evaluations and the reference-consumer compatibility matrix against the
  exact artifact to be published.

## Review and delivery

- Keep this plan `Draft` while v0.8 or any activation decision is outstanding;
  it must not interrupt the v0.8 scope prompt cycle.
- After v0.8 is tagged, create the dedicated package repository, copy this plan
  into its `plans/` directory, update paths/line references to the pinned v0.8
  compatibility tag, resolve every activation decision and change exactly one
  status line to `Status: Active`.
- Execute one Pn checkpoint per implement → review → apply-and-commit loop.
- Do not combine client-product APIs/workflows with package checkpoints; create
  a separate active plan in each consuming application for its integration.
- Record every named human review before prompt 03 applies or commits the gated
  checkpoint. Package publication is never implied by implementation approval;
  P6 requires explicit release authority.
- When all checkboxes and acceptance criteria are reviewed and green, set
  `Status: Complete`, retain the plan with the release artifacts and tag the
  reviewed package release.
