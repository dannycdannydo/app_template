# Prompt 05 — Discuss and Write a Maintenance Plan

Use this prompt when a piece of work starts as a conversation rather than a
predefined release task: smoke testing, a bug sweep, a UX improvement, or an
idea that needs shaping before implementation. It is intentionally separate
from the release scope and implement → review → commit loop.

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
   The plan must be specific enough for a fresh agent session to execute.

4. Use this structure:

   ```markdown
   # <Title>

   ## Goal

   ## Agreed scope

   ## Findings and evidence

   ## Out of scope

   ## Decisions and assumptions

   ## Implementation plan

   1. <ordered task: files/modules, behaviour, constraints>
   2. ...

   ## API, data and security impact

   ## Acceptance criteria

   ## Validation plan

   ## Review and delivery
   ```

   Include concrete file paths where known, endpoint method/path and explicit
   response schemas for API work, migrations for database changes, generated
   client changes for API changes, tests for each behaviour, and required human
   review categories. Separate independent work from ordered dependencies.

5. Finish by summarising the plan path, the intended implementation order, and
   any decision the user must make before implementation. Do not commit the
   plan unless the user explicitly asks.

## Example

User: “I’m going to smoke test the app and collect bugs.”

You: record each observation, inspect evidence when useful, and keep a grouped
issue register. After the user says “write the plan,” create a maintenance plan
covering only the confirmed, agreed fixes; it can then enter the normal
implement → review → apply-and-commit workflow as one or more work units.
