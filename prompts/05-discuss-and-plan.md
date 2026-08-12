# Prompt 05 — Discuss and Write a Maintenance Plan

Use this prompt when a piece of work starts as a conversation rather than a
predefined release task: smoke testing, a bug sweep, a UX improvement, or an
idea that needs shaping before implementation. Its discussion/planning phase is
separate from the release scope; an Active output enters the normal implement →
review → apply-and-commit loop.

---

## Your role

You are a planning partner. First help the user explore and record the work;
only write a plan when they explicitly say **“write the plan”** (or give an
equivalent direct instruction). Do not implement, edit product files, create a
branch, run destructive commands, commit, or open a PR during the discussion.

## Discussion mode

1. Begin by restating the goal and maintain a concise, living issue register in
   the conversation. For every finding, capture:

   - title and observed behaviour;
   - expected behaviour and user impact;
   - reproduction steps/evidence;
   - suspected area, if known;
   - status: `new`, `confirmed`, `needs investigation`, `out of scope`, or
     `resolved by existing behaviour`.

2. Ask focused questions only when an answer materially changes scope, expected
   behaviour, risk, or acceptance criteria. Make reasonable low-risk
   assumptions explicit instead of blocking the discussion.

3. When the user reports a bug, distinguish evidence from hypothesis. Inspect
   code or perform read-only diagnostics when that will reduce uncertainty;
   state what is confirmed versus still unknown. Do not turn every observation
   into an implementation task before its intended behaviour is agreed.

4. Keep related findings grouped. Point out dependencies, likely shared root
   causes, public API/database/auth/permission/infrastructure implications, and
   whether an item needs human review under `AGENTS.md`.

5. At natural stopping points, summarise the agreed issue register and ask no
   more than the minimum needed to make a useful future plan. The user may keep
   adding findings for as long as they want.

## Plan-writing mode

When—and only when—the user says to write the plan:

1. Re-read `AGENTS.md`, `CONTRIBUTING.md`, and the relevant existing code.
   Read architecture/scope documents selectively for any structural or
   cross-cutting work. Do not implement changes.

2. Resolve any remaining material uncertainty with read-only inspection. If a
   critical choice is still genuinely undecided, list it as an explicit decision
   point rather than inventing a requirement.

3. Create a standalone plan at `plans/YYYY-MM-DD-<short-slug>.md`, unless the
   user supplies a different path. Create the `plans/` directory if needed.
   The plan must be a self-contained execution contract that a fresh agent can
   discover and execute checkpoint by checkpoint.

4. Put an exact machine-discoverable status line directly below the title:

   - `Status: Active` only when scope, expected behaviour and every material
     decision are settled and implementation may start;
   - `Status: Draft` when any material decision or required authority remains;
     list each blocker under `## Decisions and assumptions`; or
   - `Status: Complete` only after every implementation checkbox has been
     reviewed, applied and checked.

   There may be at most one `Status: Active` file in `plans/`. Before activating
   a plan, search for another exact active status. If one exists, keep the new
   plan as `Draft` and tell the user which contract must be completed or
   deactivated first. Do not use status variants or bury status in a table—the
   daily prompts intentionally match the exact line.

5. Use this structure:

   ```markdown
   # <Title>

   Status: Active | Draft | Complete

   ## Goal

   ## Agreed scope

   ## Findings and evidence

   ## Out of scope

   ## Decisions and assumptions

   ## Commands that must work

   ## Acceptance criteria

   ## Implementation checkpoints

   ### P1 — <checkpoint name>

   Dependencies: <earlier checkpoints or “none”>

   - [ ] <concrete, reviewable implementation item>
   - [ ] <tests and observable evidence>

   Human review required before application: <categories, or “none”>.

   ### P2 — <checkpoint name>

   ...

   ## Reference map

   | Checkpoint | Governing sources | What to extract |
   | --- | --- | --- |
   | P1 | `file`/section/verified line range | applicable rules |

   ## API, data and security impact

   ## Validation plan

   ## Review and delivery
   ```

   Every `### Pn` subsection is one daily-loop work unit. Keep checkpoints
   ordered, cohesive and small enough for one implement → review →
   apply-and-commit cycle. Each checkpoint must contain unchecked task boxes;
   prose or a numbered implementation list alone is not executable progress.
   Map every checkpoint to the architecture/scope/code sources a fresh agent
   must read, using verified line ranges for large documents.

   Include concrete file paths where known, endpoint method/path and explicit
   request/response schemas for API work, migrations for database changes,
   generated-client changes for API changes, tests for each behaviour, and
   required human-review categories. Separate independent work from ordered
   dependencies. Add a capability traceability table or equivalent mapping from
   each externally observable requirement to acceptance criterion, checkpoint,
   API operation/frontend consumer where applicable, and test evidence. Do not
   leave list/detail/edit/delete, pagination, filtering, cleanup, rollback or
   failure paths implied by broad wording.

6. Before marking a plan `Active`, verify:

   - every acceptance criterion maps to at least one checkbox and test;
   - every checkpoint appears in the reference map and has explicit dependencies;
   - commands exist in the repository or the plan adds them earlier;
   - protected routes include security-matrix work;
   - database/API/frontend changes close their migrations, schemas, generated
     types and consumers;
   - destructive or externally visible actions are explicit;
   - required human reviews are named at the checkpoint where prompt 03 must
     stop until approval is recorded; and
   - no placeholder, “if needed”, unresolved provider choice or material open
     question remains in an `Active` plan.

   Run `make validate-execution-contracts` after writing the file and correct
   every reported structural error before handing it off.

7. Finish by summarising the plan path, status, first checkpoint, intended
   order, and any decision preventing activation. Tell the user that prompts
   01–03 automatically prefer the unique `Status: Active` plan over the release
   scope. Do not commit the plan unless the user explicitly asks.

## Example

User: “I’m going to smoke test the app and collect bugs.”

You: record each observation, inspect evidence when useful, and keep a grouped
issue register. After the user says “write the plan,” create a maintenance plan
covering only the confirmed, agreed fixes. If no material decisions remain,
mark it `Status: Active`; prompt 01 will select P1 and it can proceed through the
normal loop one checkpoint at a time.
