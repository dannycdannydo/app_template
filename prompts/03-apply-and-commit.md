# Prompt 03 — Apply Review, Commit and Merge

Paste this prompt to have the agent apply any review feedback, check off the completed task, commit, merge to `main`, and report what is next.

---

## Project Context

You are picking up after a review on the current release of the application starter template. This is the final step of the implement → review → apply-and-commit loop.

You need only two things, both already available:

1. The reviewer's structured review (in the recent conversation or session output).
2. The current scope file — `TEMPLATE_V0_N_SCOPE.md`, the highest-numbered `TEMPLATE_V0_*_SCOPE.md` in the repo root — §6 the progress checklist you will update.

**You do not need to read the architecture blueprint or the implementation guide for this step.** This is a mechanical step: apply fixes, validate, check boxes, commit, merge.

## Your Role

You are the **implementer**, picking up after a review.

## Instructions

1. Read `.handoff/review.md`. This is the reviewer's structured review — it contains the verdict and any must-fix, should-fix, and nit items. If the file does not exist, stop and tell the user to run `02-review` first.

2. If the verdict was APPROVED with no must-fix or should-fix items, skip to step 5.

3. Apply the must-fix items first. These are blocking — do not proceed until each is resolved.

4. Apply the should-fix items unless there is a good reason not to. Address nits at your discretion. If you choose not to address a should-fix item, state why.

5. Re-run the full validation gate to confirm everything is green:
   - `make lint`
   - `make typecheck`
   - `make test`

6. **Update the scope file.** In the current scope file §6, change `[ ]` to `[x]` for every checkbox item now genuinely complete. Leave unchecked any item where a should-fix was skipped — note it in your report.

7. **Commit.** Stage all relevant changes (implementation files + updated scope file). Write a clear commit message:

   ```
   Implement <subsection name> for template v0.N

   <1-2 sentences on what this adds and why it matters for the template foundation.>
   ```

   Include the attribution lines required by the project (see existing commits or the project's commit conventions).

8. **Push, open a PR, and merge it to `main`.** The work unit lives on a `feature/*` branch — never push directly to `main`. Push the branch and open a pull request to `main`: the PR is where CI runs and is the single merge gate (see `CONTRIBUTING.md` → Branch workflow). Once CI on the PR is green, **merge the PR into `main`** (and close it if the merge does not close it automatically) as part of this step. The review has already happened earlier in the loop, so do not leave the PR open for further review — PRs should not pile up. Delete the merged `feature/*` branch after merging.

9. **Clear the handoff files.** Delete `.handoff/implementation.md` and `.handoff/review.md`. They have served their purpose and should not linger — the next cycle starts fresh.

10. **Report status.** After committing and merging, state:
   - which subsection was completed, committed and merged;
   - whether the review was clean or changes were applied (summarise);
   - validation results;
   - the commit hash and the PR number;
   - which subsection is next in the sequence;
   - if this was the last subsection in §6, note that §5 acceptance criteria should be verified before tagging v0.N.0.

## Done means

Review feedback is applied, validation passes, the scope file reflects the new state, and the work is committed and merged to `main`. The loop is ready to restart at `01-implement-next.md` for the next subsection — or, if the current release is complete, to verify acceptance criteria and tag the release.
