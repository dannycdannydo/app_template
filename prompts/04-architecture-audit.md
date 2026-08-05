# Prompt 04 — Architecture Audit

Paste this prompt periodically to check the codebase as it stands against the cross-cutting rules and conventions of the blueprint. This is **not** part of the daily implement-review-commit loop — it is a health check that runs on demand or at release gates.

---

## Why this prompt exists

Prompt 02 reviews one diff against the blueprint sections mapped to that specific task. That catches local problems. But it cannot catch:

- **Drift across commits** — naming or structure that subtly diverges as the codebase grows.
- **Cross-cutting rule violations** — the blueprint's universal rules (routers stay thin, ORM models never serve as API request models, every endpoint declares a response schema, provider SDKs stay behind adapters) apply everywhere, not just to the task in hand. A per-task review only reads the mapped sections, so it can miss a universal rule being broken.
- **Accumulated inconsistency** — patterns that looked fine in isolation but are starting to diverge.

This audit reads the universal rule sections of the blueprint and scans the **whole codebase as it currently stands**, not a diff.

## Project Context (read once, then act)

You are auditing work on a reusable full-stack application starter template: FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL + Vue 3 + TypeScript + Tailwind + shadcn-vue.

The build is **stage by stage**. The current release is v0.3 (frontend application shell). One local file tracks progress:

- `TEMPLATE_V0_3_SCOPE.md` — §2 deliverables, §3 exclusions, §6 progress checklist.

The architecture blueprint (`Internal_Custom_Application_Starter_Architecture_v2.md`) is large. **You do not read the whole file.** You read only the cross-cutting, universal rule sections listed below. These are the conventions that apply to every task, not just one.

## Your Role

You are the **auditor**. You did not write this code. You are looking for drift, not for one-off bugs.

## Blueprint sections to read

Read these four sections only. They define the universal cross-cutting rules:

1. **§33 — Coding-Agent Governance.** The mandatory agent rules. These are universal and apply to all code, all tasks. (~20 rules.)
2. **§10 — Database Conventions.** Naming, timestamps, UUIDv7, money/decimal, soft deletion, optimistic concurrency, JSONB, constraints.
3. **§12 — API Design.** REST, versioning, pagination format, filtering and sorting rules.
4. **§13 — API Errors.** The standard error schema, exception-to-HTTP mappings, "services raise domain exceptions, central handlers translate."

Do not read other blueprint sections unless a specific finding sends you there.

## Instructions

1. Read the four blueprint sections listed above. These are the rules you are checking against.

2. Survey the codebase as it currently stands. Use `ls`, file reads (scoped, not whole-file where possible), `grep`, and `git log` to understand what has been built so far and how it is structured.

3. Be **applicability-aware**. The codebase is growing incrementally. Only check a rule where the relevant code exists. For example:
   - The foundation (v0.1) and identity/tenancy core (v0.2) are shipped, so "routers remain thin" and "business logic belongs in services" are **live** wherever routers and services exist — check them.
   - "ORM models are never API request models" applies wherever both ORM models and API request schemas coexist — check it in every module that has both.
   - Rules for capabilities that have not landed yet — e.g. "provider SDKs stay behind adapters" and "long-running work uses Dramatiq" (v0.4) — are **not applicable yet** — note this and move on.
   - State explicitly which rules are not yet applicable and why, so the user knows you did not skip them by accident.

4. For each applicable rule, scan the codebase for violations. Common things to look for:
   - Files in the wrong location per the §5 backend structure or §14 frontend structure.
   - Naming that breaks §10 conventions (singular model names, plural table names, `<entity>_id` foreign keys, snake_case DB names).
   - API routes missing explicit response schemas, or accepting ORM objects as request bodies.
   - Hardcoded `os.getenv()` calls instead of the typed settings model.
   - Provider SDKs imported directly into application modules instead of behind adapters.
   - Long-running work in HTTP handlers instead of background jobs.
   - Hand-written frontend API types instead of generated ones.
   - Missing audit events where the blueprint requires them.
   - Cross-cutting drift: a convention established in one module not followed in another.
   - Linting, typing, or tests weakened or disabled.

5. Run the validation gate to confirm the current state:
   - `make lint`
   - `make typecheck`
   - `make test`

6. Write a structured audit report:

   ```
   AUDIT VERDICT: CLEAN  |  DRIFT FOUND

   Scope reviewed:
   - (which parts of the codebase were surveyed)
   - (which release/version this audit covers)

   Rules checked and applicable:
   - (list each applicable rule from §33/§10/§12/§13 and pass/fail)

   Rules not yet applicable:
   - (list with reason, e.g. "routers do not exist yet")

   Drift found (severity-tagged):

     CRITICAL (blocks release tag):
     - (rule violated, file:line, what is wrong, what it should be)

     MAJOR (should fix before next release):
     - (...)

     MINOR (worth noting, non-blocking):
     - (...)

   Consistency check:
   - (are conventions applied uniformly across the codebase? note any divergence between modules)

   What is being done well:
   - (brief, so the patterns to keep are visible)

   Validation gate:
   - (lint/typecheck/test results)
   ```

7. **Do not commit. Do not edit any files.** Your output is the report. The user decides what to do with the findings — typically the must-fix items go back into the implement-review-commit loop as new work units, or block a release tag until resolved.

## When to use this prompt

- **On demand**, when you suspect drift or want a health check.
- **At the end of each release**, before tagging. For the current release (v0.3), this means after the final §6 subsection is complete and before tagging `v0.3.0` — a clean audit is a gating acceptance criterion (see scope §5).
- **After a cluster of related subsections**, if you want an early sweep before the release gate.

## How findings are handled

- CRITICAL and MAJOR items become work items — feed them back through prompts 01→02→03 as follow-up tasks.
- MINOR items can be batched into a cleanup pass or deferred.
- A release cannot be tagged while CRITICAL items are open.
