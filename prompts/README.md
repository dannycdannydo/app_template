# Implementation Prompts

Standardised prompts for driving the stage-by-stage build of the application starter template. Each prompt is self-contained — copy and paste the relevant one into a fresh agent session.

## The daily loop

Work proceeds through a repeating cycle:

```
00-scope-next → 00b-scope-review → 01-implement-next → 02-review → 03-apply-and-commit → 01-implement-next ...
```

Prompt 00 runs **once per release** to draft the next scope file
(`TEMPLATE_V0_N_SCOPE.md`) from the implementation guide or a named release
design source; 00b reviews that draft before it is committed. The daily loop
(01–03) then executes either that release scope or the unique active standalone
plan.

| Prompt | When to use | Role | Outcome |
| --- | --- | --- | --- |
| `00-scope-next.md` | Once per release, before the daily loop | Planner | Next release's `TEMPLATE_V0_N_SCOPE.md` drafted and ready for review |
| `00b-scope-review.md` | After `00-scope-next` | Reviewer | Scope plan reviewed; approved plan committed, or fixes requested |
| `01-implement-next.md` | Starting a new chunk of work | Implementer | Next unchecked task is built, tested, and ready for review |
| `02-review.md` | After implementation | Reviewer | Structured review with approve / request-changes verdict |
| `03-apply-and-commit.md` | After review | Implementer | Review feedback applied, task checked off, committed |
| `05-discuss-and-plan.md` | Before implementation, for a smoke-test sweep or emerging idea | Planning partner | Checkpointed standalone execution contract written as Draft, Active or Complete |

## The periodic audit

| Prompt | When to use | Role | Outcome |
| --- | --- | --- | --- |
| `04-architecture-audit.md` | On demand, or at each release gate before tagging | Auditor | Drift report against the blueprint's cross-cutting rules; CRITICAL findings block release tags |

## Branching

CI runs the full quality gate on every push to `main` and on every pull request (see `.github/workflows/ci.yml`); pushes to other branches trigger nothing. Work each scope subsection on its own branch and merge through a reviewed PR so `main` only changes via CI-gated merges:

```
git checkout -b feature/<unit>     # start of prompt 01 (or prompt 00 for the scope file)
… implement → review → apply-and-commit …
git push -u origin feature/<unit>  # after commit; no CI run on the branch
open PR to main → CI runs on the PR → merge (single CI run on main)
```

Prompt 01 starts work on the current branch; prompt 03 commits there. Do not push to `main` directly.

The audit is **not** part of the daily loop. It reads the universal rule sections of the blueprint (§33 agent rules, §10 DB conventions, §12 API design, §13 API errors) and scans the codebase as it stands for drift and cross-cutting violations that a per-diff review cannot catch. A clean audit is a gating acceptance criterion for tagging each release (see scope §5).

`05-discuss-and-plan` is also outside the daily loop. It writes a reusable plan
only when the user asks. A plan ready for implementation contains the exact line
`Status: Active`, ordered `### Pn` checkbox checkpoints, commands, acceptance
criteria and a reference map. Prompt 01 prefers the unique active plan over the
release scope. Draft/Complete plans are ignored, and more than one Active plan
is a configuration error. `make validate-execution-contracts` enforces this
shape and is part of `make check`.

## How to use them

1. Open a fresh agent session (or continue an existing one).
2. Paste the prompt for the role you need.
3. The agent discovers the unique active plan, or falls back to the latest
   release scope, then reads the project documents and handoff files.
4. When the loop completes one task, start again with `01` for the next.

## How handoffs work between sessions

Each step writes its output to a file in `.handoff/` (gitignored), so the next step can pick it up in a **different session**. Chat output does not persist — only files do.

```
Prompt 00 writes  →  .handoff/scope.md
Prompt 00b reads      .handoff/scope.md
Prompt 00b writes →  .handoff/scope_review.md   (cleared on approval)
Prompt 01 writes  →  .handoff/implementation.md
Prompt 02 reads       .handoff/implementation.md
Prompt 02 writes  →  .handoff/review.md
Prompt 03 reads       .handoff/review.md
Prompt 03 clears  →  both files deleted after commit
```

If `.handoff/scope.md` exists, a scope-plan review is waiting to happen (00b). If `.handoff/scope_review.md` exists, the scope plan has been reviewed and the verdict decides whether to commit or re-run 00. If `.handoff/implementation.md` exists, a review is waiting to happen. If `.handoff/review.md` exists, an apply-and-commit is waiting to happen. Prompt 03 clears both after committing so the next cycle starts clean.

## Token economy — how context is managed

The architecture blueprint (`Internal_Custom_Application_Starter_Architecture_v2.md`) is large. Reading it in full on every step would waste context and dilute focus. The prompts are designed to avoid this:

- **Prompt 01 (implement):** Reads the active execution contract, whose
  reference map maps each checklist work unit to its governing sources. The
  implementer reads only those sections.
- **Prompt 02 (review):** Reads only the governing sections the implementer referenced in their handoff summary, plus the active contract/dependency chain needed for an independent interface-closure check. No blanket read.
- **Prompt 03 (apply-and-commit):** Does **not** read the blueprint or implementation guide at all. It is a mechanical step — apply fixes, validate, tick boxes, commit. It needs only the handoffs and active contract.
- **Prompt 04 (audit):** Reads four cross-cutting rule sections of the blueprint (§33, §10, §12, §13) regardless of task, because it checks the whole codebase against universal rules. These sections are compact — together they are under 150 lines.

The `IMPLEMENTATION_GUIDE.md` is referenced in prompt 01 as optional broader context and in prompt 00 as the authoritative source for the next release's capability list. Release-specific design sources named by the guide or scope (such as a workflow plan) are also authoritative for interface coverage during scope planning and scope review. They are not required for ordinary day-to-day code review unless the scope itself is incomplete or ambiguous.

## Project documents

Three documents exist in the repo root (plus one more per release):

- `Internal_Custom_Application_Starter_Architecture_v2.md` — the long-term architecture standard. Read selectively, via the reference map.
- `IMPLEMENTATION_GUIDE.md` — the build plan and incremental release sequence. Always read in prompt 00; optional elsewhere.
- `TEMPLATE_V0_N_SCOPE.md` — the current release contract, used when no active
  standalone plan exists.
- `plans/*.md` — standalone contracts. Only the unique exact `Status: Active`
  plan participates in the daily loop.

## Notes

- The unit of work is one scope subsection (e.g. §6.2) or one standalone-plan
  checkpoint (e.g. P2). Closely related line items may be batched only within
  that unit.
- If a review comes back clean (approved), skip the fix steps in `03` and go straight to commit.
- The audit (`04`) runs on demand or at release gates — not after every commit. CRITICAL findings block the release tag; MAJOR and MINOR findings are fed back into the daily loop as follow-up work.
- Prompt 00 runs once per release; prompts 01–03 loop within a release. After every subsection in §6 is checked and all acceptance criteria in §5 (including the clean audit) are met, the release is tagged and prompt 00 drafts the next scope file (`TEMPLATE_V0_N_SCOPE.md`).
