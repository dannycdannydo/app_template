# Prompt 03 — Apply Review, Commit and Merge

Paste this prompt to have the agent apply any review feedback, check off the completed task, commit, merge to `main`, and report what is next.

---

## Project Context

You are picking up after a review on the current release of the application starter template. This is the final step of the implement → review → apply-and-commit loop.

You need only two things, both already available:

1. The reviewer's structured review in `.handoff/review.md`.
2. The exact active execution-contract path recorded in
   `.handoff/implementation.md`: a `Status: Active` plan in `plans/`, if one was
   used, or the relevant `TEMPLATE_V0_N_SCOPE.md` scope file.

**You do not need to read the architecture blueprint or the implementation guide for this step.** This is a mechanical step: apply fixes, validate, check boxes, commit, merge.

## Your Role

You are the **implementer**, picking up after a review.

## Instructions

1. Read `.handoff/review.md`. This is the reviewer's structured review — it contains the verdict and any must-fix, should-fix, and nit items. If the file does not exist, stop and tell the user to run `02-review` first.

   Also read `.handoff/implementation.md` to resolve the exact active contract
   and work unit. Do not guess from the highest-numbered scope or another plan.

2. If the verdict is `APPROVED` with no must-fix or should-fix items, skip to
   step 5. Otherwise apply the review findings in steps 3–4; a `CHANGES
   REQUESTED` verdict is the normal input for that correction path.

3. Apply the must-fix items first. These are blocking — do not proceed until each is resolved.

4. Apply the should-fix items unless there is a good reason not to. Address nits at your discretion. If you choose not to address a should-fix item, state why.

5. **Update the documentation for every area this work unit touched.** A change
   that alters behaviour, configuration, a pattern or an invariant is not done
   until the documents that describe it are correct. This is the final
   documentation gate — prompt 01 should already have updated docs as part of
   implementation and prompt 02 should have checked them, but fix any gap here
   before validating. Inspect the diff and the touched areas, then:

   - **Area guides:** update the `AGENTS.md` at the root of each area touched
     (`backend/AGENTS.md`, `backend/app/db/AGENTS.md`, `backend/app/ai/AGENTS.md`,
     `backend/app/job_coordinator/AGENTS.md`, `frontend/AGENTS.md`) when the
     change alters that area's rules, invariants, layout, procedures or
     gotchas. A stale guide is worse than none.
   - **Centralised docs:** update `ARCHITECTURE.md` (system shape, request flow,
     layering, cross-cutting behaviour), `API_CONVENTIONS.md` (HTTP surface and
     conventions), `SECURITY.md` (controls, deferrals) or `README.md` (the
     front-page summary and command surface) whenever the change makes them
     inaccurate. A new `/api/v1` endpoint, permission code, background job type
     or security control must be reflected where it belongs.
   - **Configuration and runbooks:** document every new setting in
     `.env.example` (and `.env.production.example` when production-relevant);
     update `docs/operations.md`, `docs/backup-and-recovery.md`,
     `docs/rls-*.md` or add/supersede a `docs/decisions/` ADR when the change
     affects deployment, recovery, tenant isolation or a standing decision.
   - **Release bookkeeping:** update the release/version markers and upgrade
     guide if, and only if, this unit is release bookkeeping; the active
     contract checklist itself is updated in step 6.

   Do not rewrite unaffected docs and do not invent documentation. If a doc
   looks stale but you are unsure whether this work unit changed it, inspect it:
   either correct it in this change, or state in the report why it is unchanged.
   Documentation changes are staged and committed with the work unit, never as
   a separate later cleanup.

6. Run the **single complete local validation gate** for the work unit, whether
   the review was clean or changes were applied:
   - `make check`
   - every additional command required by the active checkpoint/contract that
     is not included in `make check`.

   Fix every failure and rerun the affected check, then rerun `make check` to
   establish one clean final result. This is the only stage that runs the full
   local gate by default; focused checks in prompts 01 and 02 provide earlier
   feedback without repeating the whole suite.

   Then, if the work unit names a required human-review gate, verify the
   review/handoff or contract contains explicit recorded approval for every
   named category. Stop before changing checkboxes, committing or merging if it
   is absent; never treat an automated/agent review as human approval.

7. **Update the active contract.** Change `[ ]` to `[x]` only in the selected
   subsection/checkpoint for every genuinely complete item approved by the
   review. For a plan in `plans/`, once every implementation checkbox in every
   checkpoint is checked, change the exact `Status: Active` line to
   `Status: Complete`; otherwise retain it. Leave unchecked any incomplete item.

8. **Commit.** Stage all relevant changes (implementation files, updated docs,
   and updated scope file or plan). Write a clear commit message:

   ```
   Implement <work-unit name> for <template v0.N or plan name>

   <1-2 sentences on what this adds and why it matters for the template foundation.>
   ```

   Include the attribution lines required by the project (see existing commits or the project's commit conventions).

9. **Push, open a PR, and merge it to `main`.** The work unit lives on a `feature/*` branch — never push directly to `main`. Push the branch and open a pull request to `main`: the PR is where CI runs and is the single merge gate (see `CONTRIBUTING.md` → Branch workflow). Once CI on the PR is green, **merge the PR into `main`** (and close it if the merge does not close it automatically) as part of this step. The review has already happened earlier in the loop, so do not leave the PR open for further review — PRs should not pile up. Delete the merged `feature/*` branch after merging.

10. **Clear the handoff files.** Delete `.handoff/implementation.md` and `.handoff/review.md`. They have served their purpose and should not linger — the next cycle starts fresh.

11. **Report status.** After committing and merging, state:
    - which subsection/checkpoint was completed, committed and merged;
    - whether the review was clean or changes were applied (summarise);
    - which documentation was updated (area guides, centralised docs,
      configuration, runbooks) and why any touched-area doc needed no change;
    - validation results;
    - the commit hash and the PR number;
    - which subsection/checkpoint is next in sequence;
    - if this completed a standalone plan, report `Status: Complete`;
    - if this was the last release-scope subsection, note that its acceptance
      criteria must be verified before tagging.

## Done means

Review feedback is applied, documentation for every touched area (and any
affected centralised doc) is accurate, validation passes, the active contract
reflects the new state, and the work is committed and merged to `main`. The loop
is ready to restart at `01-implement-next.md` for the next work unit, or to
close the active plan/release when none remains.
