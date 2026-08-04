# Implementation Prompts

Standardised prompts for driving the stage-by-stage build of the application starter template. Each prompt is self-contained — copy and paste the relevant one into a fresh agent session.

## The daily loop

Work proceeds through a repeating cycle:

```
01-implement-next  →  02-review  →  03-apply-and-commit  →  01-implement-next ...
```

| Prompt | When to use | Role | Outcome |
| --- | --- | --- | --- |
| `01-implement-next.md` | Starting a new chunk of work | Implementer | Next unchecked task is built, tested, and ready for review |
| `02-review.md` | After implementation | Reviewer | Structured review with approve / request-changes verdict |
| `03-apply-and-commit.md` | After review | Implementer | Review feedback applied, task checked off, committed |

## The periodic audit

| Prompt | When to use | Role | Outcome |
| --- | --- | --- | --- |
| `04-architecture-audit.md` | On demand, or at each release gate before tagging | Auditor | Drift report against the blueprint's cross-cutting rules; CRITICAL findings block release tags |

The audit is **not** part of the daily loop. It reads the universal rule sections of the blueprint (§33 agent rules, §10 DB conventions, §12 API design, §13 API errors) and scans the codebase as it stands for drift and cross-cutting violations that a per-diff review cannot catch. A clean audit is a gating acceptance criterion for tagging each release (see scope §5).

## How to use them

1. Open a fresh agent session (or continue an existing one).
2. Paste the prompt for the role you need.
3. The agent reads the project documents, finds its place, and acts.
4. When the loop completes one task, start again with `01` for the next.

## Token economy — how context is managed

The architecture blueprint (`Internal_Custom_Application_Starter_Architecture_v2.md`) is ~2150 lines. Reading it in full on every step would waste context and dilute focus. The prompts are designed to avoid this:

- **Prompt 01 (implement):** Reads the scope file, which contains a **blueprint reference map** (§7) mapping each checklist subsection to the specific blueprint sections that govern it. The implementer reads only those sections — typically 2–4 short sections, not the whole document.
- **Prompt 02 (review):** Reads only the blueprint sections the implementer referenced in their handoff summary. No blanket read.
- **Prompt 03 (apply-and-commit):** Does **not** read the blueprint or implementation guide at all. It is a mechanical step — apply fixes, validate, tick boxes, commit. It needs only the review feedback and the scope checklist.
- **Prompt 04 (audit):** Reads four cross-cutting rule sections of the blueprint (§33, §10, §12, §13) regardless of task, because it checks the whole codebase against universal rules. These sections are compact — together they are under 150 lines.

The `IMPLEMENTATION_GUIDE.md` is referenced in prompt 01 as optional broader context. It is not required for day-to-day work.

## Project documents

Three documents exist in the repo root:

- `Internal_Custom_Application_Starter_Architecture_v2.md` — the long-term architecture standard. Read selectively, via the reference map.
- `IMPLEMENTATION_GUIDE.md` — the build plan and incremental release sequence. Optional reference.
- `TEMPLATE_V0_1_SCOPE.md` — the current release contract with the progress checklist and blueprint reference map. Always read.

## Notes

- The unit of work is one subsection of the scope checklist (e.g. §6.2, §6.3). The implementer may batch closely related line items within a subsection.
- If a review comes back clean (approved), skip the fix steps in `03` and go straight to commit.
- The audit (`04`) runs on demand or at release gates — not after every commit. CRITICAL findings block the release tag; MAJOR and MINOR findings are fed back into the daily loop as follow-up work.
- After every subsection in §6 is checked and all acceptance criteria in §5 (including the clean audit) are met, the release is tagged and the next scope file (`TEMPLATE_V0_2_SCOPE.md`) is opened.
