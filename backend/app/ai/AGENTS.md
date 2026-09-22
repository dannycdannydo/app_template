# AI Subsystem — Agent Guide

Read this before changing anything under `app/ai/` or the AI demo module. It is a
short orientation and invariant list; **`app/ai/README.md` is the detailed
developer guide** and the design authorities are ADR-0017
(`docs/decisions/0017-provider-neutral-ai-service.md`) and ADR-0018
(`docs/decisions/0018-vertex-ai-only.md`), plus `app/db/AGENTS.md` for the RLS
side. Keep this guide current when an AI rule or procedure changes.

## Layout

- `service.py` — `AIService`, the only application-facing entry point
  (`execute`, `authorize_source`).
- `schemas.py` / `errors.py` — `AIRequest`/`AIResult`/`Attachment` and the
  normalised `AIError` taxonomy.
- `registry.py` + `tasks/*.yaml` + `prompts/*` + `models/registry.yaml` — the
  checked-in task/prompt/model registries and validator.
- `providers/` — `LLMProvider` ABC and concrete adapters (`openai`, `anthropic`,
  `deepseek`, `azure_openai`, `vertex`, `local`, deterministic `fake`); selected
  by `providers/factory.py`.
- `execution.py` — the durable `ai.execute` job and `run_claimed` wrapper.
- `persistence/` — ORM models, typed SQL, the request-time port and impl, the
  transfer-reference store, reconciliation and platform AI-settings router.
- `transfer*.py`, `staging.py`, `scratch.py`, `source_authority.py`,
  `storage_resolver.py`, `runtime.py` — provider-neutral transfer/staging/source
  plumbing.
- Public demo routes: `app/modules/ai_demo/`; platform settings:
  `app/ai/persistence/router.py`.

## Invariants

- **Provider SDKs stay behind adapters.** They may be imported only under
  `app/ai/providers/`; enforced by `tests/test_ai_import_boundary.py`. Transfer,
  staging and reference internals may not be imported outside `app/ai/`, and
  feature modules may not name transfer modes or `gs://` references.
- **Feature modules use only** `AIService`, `app.ai.schemas`, `app.ai.errors` and
  `app.ai.execution`. No prompts, document bytes, credentials, signed URLs or raw
  provider responses in logs, audit, the broker or Sentry.
- **AI cleanup deletes only AI-owned copies**, never feature sources. Cleanup is
  always an explicit persistence/transfer operation.
- **No caller-supplied URLs or browser-to-provider uploads.** Non-inline transfer
  modes are default-deny at deployment (`AI_ENABLED_TRANSFER_MODES`) and per
  organisation (`allowed_transfer_modes=["inline"]`).
- **`AIService.execute` fails closed without a persistence port**; a task declares
  exactly one of `output_schema` or `declares_text_result`.
- AI data tables (`ai_requests`, `ai_outputs`, `ai_attachment_references`,
  `ai_scratch_uploads`) are organisation-owned under RLS; the worker binds the
  durable job's organisation, and because context is transaction-local it rebinds
  after every internal commit (`persistence/service.py`, `execution.py`).

## Adding a provider / model / task / prompt

- **Provider:** add the adapter under `providers/`, add the id to
  `AI_KNOWN_PROVIDER_IDS` (`app/core/config.py`) and the `create()` branch +
  ordering in `providers/factory.py`, export it in `providers/__init__.py`, add a
  `contracts/providers.yaml` entry if it supports non-inline modes, and update
  `PROVIDER_INLINE_ATTACHMENT_MIME_TYPES` if it carries documents.
- **Model:** add an entry to `models/registry.yaml` (provider, capabilities,
  context window, tiers, pricing, transfer limits) consistent with the adapter
  and provider contract.
- **Task:** add `tasks/<name>.yaml`, a matching prompt, a Pydantic output schema
  in `tasks/schemas.py` for structured tasks, register a durable task in
  `DURABLE_TASKS` (`execution.py`) if background, and add the route/schema.
- **Prompt:** add `prompts/<dir>/<name>_v<version>.yaml`; the filename must match
  name and version, and variables/`output_contract` must match the task.
- Run `make validate-ai-registries` (`scripts/validate_ai_registries.py` →
  `validate_registry_bundle`). It checks task↔prompt existence, variable and
  output-contract match, model routability, MIME subsets and transfer-mode
  realisability.

## Execution

- **Sync:** `execute_managed_ai(session, AIRequest(...))` → `AIService.execute`.
- **Queued:** `enqueue_document_execution` writes a `QUEUED` `ai_requests` row and
  a durable `ai.execute` job in one transaction; the broker message carries only
  the job id. `request_id = job_id.hex`.
- The `ai` queue (`execute_ai_task`) runs under `run_claimed`; `AIRequestReplayError`
  reconciles to the winning attempt and `AIError` is terminal
  (`JobPermanentError`).

## Testing

- Default run excludes the `ai_contracts` marker; run live provider tests with
  `make test-ai-contracts` (skipped without credentials).
- Representative suites: `tests/test_ai_service.py`, `test_ai_registry.py`,
  `test_ai_provider_factory.py`, `test_ai_adapters.py`, `test_ai_output_safety.py`,
  `test_ai_execution.py`, `test_ai_persistence_db.py`, `test_ai_reference_db.py`,
  `test_ai_reconcile_db.py`, `test_ai_import_boundary.py`,
  `test_rls_ai_data_enablement_db.py`.
- New protected `/api/v1/ai/*` routes must be added to `PROTECTED_ROUTES` in
  `tests/test_security_suite.py` (the demo routes already are).

## Gotchas

- Retry-only reference reuse relies on the partial unique index
  `(organisation_id, idempotency_key) WHERE status='live'`; `ai_outputs` keeps a
  composite `(ai_request_id, organisation_id)` FK.
- Retention/reconciliation sweeps iterate the global `organisations` table and
  bind each tenant, with a fair per-organisation batch budget — never a bypass.
- `ai_requests.execution_metadata` is bounded and independently expiring; do not
  put unbounded or sensitive data there.

## Key files

- `app/ai/README.md` (detailed), `app/ai/service.py`, `app/ai/registry.py`
- `app/ai/providers/base.py`, `app/ai/providers/factory.py`
- `app/ai/execution.py`, `app/ai/persistence/`
- `app/modules/ai_demo/` (routes), `app/ai/persistence/router.py` (platform settings)
- `scripts/validate_ai_registries.py`, `tests/test_ai_import_boundary.py`
