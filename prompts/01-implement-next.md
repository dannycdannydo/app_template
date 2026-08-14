# Prompt 01 — Implement Next Task

Paste this prompt to have the agent pick up the next unchecked work unit and implement it, then prepare for review.

---

## Project Context (read once, then act)

You are building a reusable full-stack application starter template: FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL + Vue 3 + TypeScript + Tailwind + shadcn-vue, modular monolith, WorkOS auth, Dramatiq jobs, provider-neutral storage.

The build is **stage by stage**. Work is governed by one active execution
contract: either a standalone plan in `plans/` whose exact status line is
`Status: Active`, or otherwise the highest-numbered root
`TEMPLATE_V0_*_SCOPE.md`. Each checklist subsection/checkpoint is one work unit,
worked through a three-step loop: implement → review → apply-and-commit.

Discover and read the contract first:

1. Search `plans/*.md` for the exact line `Status: Active`. If exactly one
   exists, it is the active contract. If more than one exists, stop and report
   the conflicting paths. Ignore `Draft`, `Complete`, `Proposed` and descriptive
   status text. If none exists, use the highest-numbered root
   `TEMPLATE_V0_*_SCOPE.md`.
2. The active contract supplies scope/exclusions, acceptance criteria, commands,
   an ordered progress checklist (`§6` for a release scope or
   `Implementation checkpoints` for a plan), and a reference map.
3. `Internal_Custom_Application_Starter_Architecture_v2.md` is the architecture
   standard. **Do not read the whole file.** Use the active contract's reference
   map to read only the sections relevant to the selected work unit.

`IMPLEMENTATION_GUIDE.md` exists if you need broader context on the release sequence, but it is optional for day-to-day work.

## Your Role

You are the **implementer**.

## Instructions

1. Work on a **feature branch**, never `main`: `git checkout -b feature/<subsection-or-short-name>` if you are not already on one. CI runs only on pushes to `main` and on pull requests, so a branch keeps the gate quiet until the work unit is merged (see `CONTRIBUTING.md` → Branch workflow).

2. Find the **next unchecked work unit** in sequence: a §6 subsection in a
   release scope or a `### Pn` checkpoint under `Implementation checkpoints` in
   an active plan. If some items are already checked, complete the remaining
   ones. Do not combine separate subsections/checkpoints.

3. Consult the contract's reference map and read **only** the listed governing
   sections for this work unit. Follow existing patterns already in the repo.
   Do not invent conventions that contradict the blueprint.

   Before coding, inspect the current API surface (routers and generated
   OpenAPI types) for every endpoint needed by this subsection and by any
   explicitly dependent later UI task. If the scope says a later view can
   list, view or edit a resource but the required operation is absent from the
   current task and all earlier completed tasks, stop and report it as a scope
   gap rather than quietly assuming it exists.

4. State at the start of your work:
   - the active contract path;
   - which subsection/checkpoint and checkbox items you will complete;
   - which blueprint sections you read.

5. Implement the work **fully and end-to-end**: real working files (no stubs, placeholders, or TODOs), configuration wired correctly, tests written where they naturally belong, imports/types/formatting clean.

6. Run **focused validation** immediately after your changes. Choose the
   smallest commands that exercise the changed behaviour and provide fast
   feedback:
   - the backend and/or frontend tests directly covering the changed code;
   - relevant formatter, lint or type checks for the affected package/files;
   - every checkpoint-specific command required by the contract; and
   - generated-client or migration checks when those surfaces changed.

   Do not run the complete repository gate by default in this step; prompt 03
   runs it once after review. Run `make check` here as well when the work is
   unusually broad or high-risk (for example authentication, permissions,
   tenant isolation, migrations, shared infrastructure, dependencies or
   cross-cutting generated API changes), or when the contract explicitly
   requires it before review.

7. Fix anything that fails before declaring the work ready. Do not defer a
   known focused-test, lint or type error to prompt 03.

8. **Write the handoff summary to a file.** This is required — do not skip it. Write to `.handoff/implementation.md` (this directory is gitignored). The file is what the reviewer reads in their session — they will not see your chat output. Include:
   - active contract path and status;
   - subsection/checkpoint completed;
   - files created or changed, with a one-line purpose each;
   - which checklist items should now be checked;
   - **which blueprint sections you followed** (the reviewer will read these);
   - any decisions made where the blueprint was silent or ambiguous;
   - any deviations from the plan and why;
   - what the reviewer should pay closest attention to;
   - an **interface-coverage check**: each completed checkbox mapped to its
     method/path, request and response schema, tests, and any known frontend
     consumer; explicitly list required operations that are still absent;
   - focused validation commands run and their results, plus whether the full
     `make check` gate was run and why.

9. **Do not commit. Do not check off boxes.** Leave the work uncommitted so the reviewer can inspect the diff cleanly. If the work unit names a human-review gate, call it out prominently; implementation may be reviewed, but prompt 03 cannot apply/commit it until the required approval is recorded. The handoff file `.handoff/implementation.md` must exist before you hand off.

## Done means

Implementation is complete, focused validation is green, and
`.handoff/implementation.md` has been written. The work is ready for
`02-review`; the complete local gate runs in prompt 03.
