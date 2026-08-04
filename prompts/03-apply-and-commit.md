# Prompt 03 — Apply Review and Commit

Paste this prompt to have the agent apply any review feedback, check off the completed task, commit, and report what is next.

---

## Project Context

You are picking up after a review on the v0.1 foundation release of the application starter template. This is the final step of the implement → review → apply-and-commit loop.

You need only two things, both already available:

1. The reviewer's structured review (in the recent conversation or session output).
2. `TEMPLATE_V0_1_SCOPE.md` §6 — the progress checklist you will update.

**You do not need to read the architecture blueprint or the implementation guide for this step.** This is a mechanical step: apply fixes, validate, check boxes, commit.

## Your Role

You are the **implementer**, picking up after a review.

## Instructions

1. Read the reviewer's structured review. Note the verdict and any must-fix, should-fix, and nit items.

2. If the verdict was APPROVED with no must-fix or should-fix items, skip to step 5.

3. Apply the must-fix items first. These are blocking — do not proceed until each is resolved.

4. Apply the should-fix items unless there is a good reason not to. Address nits at your discretion. If you choose not to address a should-fix item, state why.

5. Re-run the full validation gate to confirm everything is green:
   - `make lint`
   - `make typecheck`
   - `make test`

6. **Update the scope file.** In `TEMPLATE_V0_1_SCOPE.md` §6, change `[ ]` to `[x]` for every checkbox item now genuinely complete. Leave unchecked any item where a should-fix was skipped — note it in your report.

7. **Commit.** Stage all relevant changes (implementation files + updated scope file). Write a clear commit message:

   ```
   Implement <subsection name> for template v0.1

   <1-2 sentences on what this adds and why it matters for the template foundation.>
   ```

   Include the attribution lines required by the project (see existing commits or the project's commit conventions).

8. **Report status.** After committing, state:
   - which subsection was completed and committed;
   - whether the review was clean or changes were applied (summarise);
   - validation results;
   - the commit hash;
   - which subsection is next in the sequence;
   - if this was the last subsection in §6, note that §5 acceptance criteria should be verified before tagging v0.1.0.

## Done means

Review feedback is applied, validation passes, the scope file reflects the new state, and the work is committed. The loop is ready to restart at `01-implement-next.md` for the next subsection — or, if v0.1 is complete, to verify acceptance criteria and tag the release.
