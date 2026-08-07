# Prompt 01 — Implement Next Task

Paste this prompt to have the agent pick up the next unchecked work unit and implement it, then prepare for review.

---

## Project Context (read once, then act)

You are building a reusable full-stack application starter template: FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL + Vue 3 + TypeScript + Tailwind + shadcn-vue, modular monolith, WorkOS auth, Dramatiq jobs, provider-neutral storage.

The build is **stage by stage**. The current release is the one whose scope file exists as `TEMPLATE_V0_N_SCOPE.md` (the highest-numbered such file, currently `TEMPLATE_V0_4_SCOPE.md`). Each subsection of the checklist is one work unit, worked through a three-step loop: implement → review → apply-and-commit.

Two local files govern this release — read them both first:

1. The current scope file — `TEMPLATE_V0_N_SCOPE.md`, the highest-numbered `TEMPLATE_V0_*_SCOPE.md` in the repo root. It is the release contract: §2 deliverables, §3 exclusions, §4 commands, §5 acceptance criteria, §6 progress checklist, §7 blueprint reference map.
2. `Internal_Custom_Application_Starter_Architecture_v2.md` — the architecture standard. **Do not read the whole file.** Use the reference map in §7 of the scope file to read only the blueprint sections relevant to the current task.

`IMPLEMENTATION_GUIDE.md` exists if you need broader context on the release sequence, but it is optional for day-to-day work.

## Your Role

You are the **implementer**.

## Instructions

1. Work on a **feature branch**, never `main`: `git checkout -b feature/<subsection-or-short-name>` if you are not already on one. CI runs only on pushes to `main` and on pull requests, so a branch keeps the gate quiet until the work unit is merged (see `CONTRIBUTING.md` → Branch workflow).

2. Open the current scope file §6 and find the **next unchecked subsection** in sequence (6.1, then 6.2, etc.). If some items in a subsection are already checked, complete the remaining ones. Batch closely related line items within a single subsection.

3. Consult §7 (blueprint reference map) and read **only** the listed blueprint sections for this subsection. Follow existing patterns already in the repo. Do not invent conventions that contradict the blueprint.

   Before coding, inspect the current API surface (routers and generated
   OpenAPI types) for every endpoint needed by this subsection and by any
   explicitly dependent later UI task. If the scope says a later view can
   list, view or edit a resource but the required operation is absent from the
   current task and all earlier completed tasks, stop and report it as a scope
   gap rather than quietly assuming it exists.

4. State at the start of your work:
   - which subsection and which checkbox items you will complete;
   - which blueprint sections you read.

5. Implement the work **fully and end-to-end**: real working files (no stubs, placeholders, or TODOs), configuration wired correctly, tests written where they naturally belong, imports/types/formatting clean.

6. Run validation immediately after your changes:
   - `make lint`
   - `make typecheck`
   - `make test`
   - any other relevant check from §4.

7. Fix anything that fails before declaring the work ready.

8. **Write the handoff summary to a file.** This is required — do not skip it. Write to `.handoff/implementation.md` (this directory is gitignored). The file is what the reviewer reads in their session — they will not see your chat output. Include:
   - subsection completed;
   - files created or changed, with a one-line purpose each;
   - which §6 items should now be checked;
   - **which blueprint sections you followed** (the reviewer will read these);
   - any decisions made where the blueprint was silent or ambiguous;
   - any deviations from the plan and why;
   - what the reviewer should pay closest attention to;
   - an **interface-coverage check**: each completed checkbox mapped to its
     method/path, request and response schema, tests, and any known frontend
     consumer; explicitly list required operations that are still absent;
   - validation commands run and their results.

9. **Do not commit. Do not check off boxes.** Leave the work uncommitted so the reviewer can inspect the diff cleanly. The handoff file `.handoff/implementation.md` must exist before you hand off.

## Done means

Implementation is complete, validated, and `.handoff/implementation.md` has been written. The work is ready for `02-review`.
