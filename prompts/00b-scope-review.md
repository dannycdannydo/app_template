# Prompt 00b — Review Scope Plan

Paste this prompt after `00-scope-next` to have the agent review the drafted scope file before it is committed and before the daily loop executes against it.

---

## Project Context (read once, then act)

You are reviewing the scope plan for the next release of a reusable full-stack application starter template. The plan was drafted by `00-scope-next`; your job is to find problems before it becomes the contract for the daily implement → review → apply-and-commit loop.

Read, in this order:

1. `.handoff/scope.md` — the planner's handoff summary (which release, subsection ordering, blueprint mappings, open questions).
2. The drafted scope file (the newest `TEMPLATE_V0_N_SCOPE.md` in the repo root).
3. The corresponding `Template v0.N — <Title>` section in
   `IMPLEMENTATION_GUIDE.md`, when one exists.
4. The previous scope file (for structural and convention comparison).
5. Every release-specific design source named by the draft, guide, prior scope or planner
   handoff (for example `plans/*_WORKFLOW_PLAN.md`) — especially its API-surface and
   frontend-route tables.
6. The specific blueprint sections the planner mapped — verify their line ranges are accurate.

The architecture blueprint (`Internal_Custom_Application_Starter_Architecture_v2.md`) is large. **Do not read the whole file.** Read only the sections referenced in the draft's §7 map.

## Your Role

You are the **scope reviewer**. You did not write this plan. Your job is to find problems before it becomes the contract for the daily loop.

## Instructions

1. Read `.handoff/scope.md`. If it does not exist, stop and tell the user to run `00-scope-next` first.

2. Review the draft against these lenses, in priority order:

   **Completeness** — Independently make a capability traceability matrix:
   source requirement → §5 acceptance criterion → §6 checkbox → backend
   method/path → frontend route/view → test evidence. Compare it with the
   planner's matrix. Does §2 cover every capability in the guide *and every
   release-specific design source*? Is anything deferred in §3 that should
   ship now (or shipped now that the guide defers)? Does every §6 subsection
   appear in §5 and §7? Treat each HTTP operation as separately required:
   create does not cover list/detail/edit/delete, and an UI view does not prove
   its required API exists. A missing operation required by a source is a
   **must-fix**, even if a later work unit could discover it.

   **Structure** — Does it mirror the previous scope file's eight sections? Are §6 subsections coherent, correctly ordered (dependencies first), and granular enough to be one work unit each for the daily loop?

   **Measurability** — Is every §5 acceptance criterion objectively verifiable by an agent (a command to run, a response code, a diff-free regeneration)? Flag any criterion that cannot be checked.

   **Interface closure** — For every planned frontend view/action, identify
   the exact backend operation it needs. For every new backend operation,
   identify its owning §6 work unit, explicit response schema and test. Flag
   operations that are only implied, missing from `PROTECTED_ROUTES`, or have
   no testable acceptance criterion.

   **Reference-map accuracy** — Spot-check at least half of the §7 mappings against the blueprint: do the line ranges exist and cover the stated content? Anything mapped that should not be, or missing?

   **Scope discipline** — Does the release stay within the guide's capability list? Any creep from later releases (v0.N+1) or from the blueprint's longer-term plans?

   **Consistency** — Do §4 commands match the existing Makefile surface and the previous release's commands? Do conventions (naming, error format, migration rules) from the previous scope file carry over?

3. **Write the review to `.handoff/scope_review.md`** (gitignored). This file is what the next step reads — they will not see your chat output. Use this format:

   ```
   VERDICT: APPROVED  |  CHANGES REQUESTED

   Summary: (1-3 sentences)

   Must-fix (blocking):
   - (specific issue with section reference, or "none")

   Should-fix (non-blocking):
   - (specific issue, or "none")

   Nits (optional):
   - (minor notes, or "none")

   What was done well:
   - (brief)

   Subsections confirmed ready for the daily loop:
   - (specific §6.x items)

   Capability traceability checked:
   - (source requirement → §6 checkbox → method/path → view/test, including
     any deliberate deferral)
   ```

4. If the verdict is CHANGES REQUESTED: **do not commit and do not edit the scope file.** The planner re-runs `00-scope-next` with your findings.

5. If the verdict is APPROVED with no must-fix or should-fix items, apply the one-off commit for this deliverable:

   - Stage the drafted scope file (`TEMPLATE_V0_N_SCOPE.md`).
   - Commit with a message like:
     ```
     Open TEMPLATE_V0_N_SCOPE.md for template v0.N

     <1-2 sentences on what this release delivers and why it is the right
     next increment for the template.>
     ```
     Include the attribution lines required by the project (see existing commits).
   - Delete `.handoff/scope.md` and `.handoff/scope_review.md` — they have served their purpose and the next cycle starts fresh.
   - Report the commit hash and which §6 subsection the daily loop should start with (`01-implement-next`).

## Done means

The scope plan is reviewed. If approved, it is committed, the handoff files are cleared, and the daily loop is ready to start at `01-implement-next.md`. If changes were requested, the planner has a clear list of fixes.
