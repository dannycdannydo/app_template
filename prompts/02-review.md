# Prompt 02 — Review

Paste this prompt to have the agent review the most recent uncommitted implementation against the project requirements and conventions.

---

## Project Context (read once, then act)

You are reviewing work on a reusable full-stack application starter template: FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL + Vue 3 + TypeScript + Tailwind + shadcn-vue.

The build is **stage by stage**. The current release is the one whose scope file exists as `TEMPLATE_V0_N_SCOPE.md` (the highest-numbered such file, currently `TEMPLATE_V0_4_SCOPE.md`). Each subsection of the checklist is one work unit, worked through a three-step loop: implement → review → apply-and-commit.

One local file governs this release:

- The current scope file — `TEMPLATE_V0_N_SCOPE.md`, the highest-numbered `TEMPLATE_V0_*_SCOPE.md` in the repo root — §2 deliverables, §3 exclusions, §4 commands, §5 acceptance criteria, §6 progress checklist, §7 blueprint reference map.

The architecture blueprint (`Internal_Custom_Application_Starter_Architecture_v2.md`) is large. **Do not read the whole file.** Read only the specific blueprint sections the implementer references in their handoff summary — no more.

## Your Role

You are the **reviewer**. You did not write this code. Your job is to find problems before it is committed.

## Instructions

1. Read `.handoff/implementation.md`. This is the implementer's handoff summary — it tells you which §6 subsection was implemented and **which blueprint sections they followed**. If the file does not exist, stop and tell the user to run `01-implement-next` first.

2. Read the relevant §6 subsection in the scope file to confirm which checkbox items this work was supposed to complete.

3. Read **only** the blueprint sections the implementer referenced in the handoff file. These define the conventions you are checking against. Also read the current scope's §2, §5 and the full §6 subsection dependency chain needed to determine whether an interface required by this work already exists. If the scope names a release-specific design source and the contract is ambiguous, read only that source's deliverables/API-surface/frontend-route tables. This is contract verification, not a blanket blueprint read.

4. Independently verify interface closure before reviewing implementation
   details:
   - translate each checkbox being claimed into concrete observable behaviour;
   - inspect routers and generated OpenAPI types, not just the diff, to verify
     the required method/path exists with an explicit response schema;
   - for a frontend view/action, identify its exact API operation and verify
     that it exists now or is explicitly scheduled in a later unchecked task;
   - for API work, verify create/list/detail/edit/delete, pagination and
     filtering individually when required by §2, §5, a release design source,
     or a dependent UI task; and
   - verify every new protected route is in `PROTECTED_ROUTES`.

   Do not accept "the current checkbox was implemented" as sufficient when
   the release contract or a required dependent UI flow exposes a missing API
   operation. Record that as a must-fix scope/implementation gap with the
   source requirement and the missing method/path.

5. Review the changes against these lenses, in priority order:

   **Correctness** — Does it do what the task requires? Do the §4 commands actually work? Any logic errors or incomplete paths?

   **Convention compliance** — Does it follow the blueprint sections you read? Does it match existing repo patterns? Files in the right places?

   **Security** — No secrets committed, no unsafe defaults, `.env.example` accurate. Flag anything that could become a problem later.

   **Test quality** — Tests meaningful, not just smoke checks? Obvious gaps?

   **Code quality** — Clear naming, no dead code, no stubs/TODOs, no unjustified dependencies, strict types.

   **Scope discipline** — Stayed within the current release's scope (§2)? Avoided pulling in deferred work (§3)? Flag any scope creep.

6. Run the validation commands yourself to confirm:
   - `make lint`
   - `make typecheck`
   - `make test`

7. **Write the review to `.handoff/review.md`.** This file is what the next step reads — they will not see your chat output. Use this format:

   ```
   VERDICT: APPROVED  |  CHANGES REQUESTED

   Summary: (1-3 sentences)

   Must-fix (blocking):
   - (specific issue with file:line, or "none")

   Should-fix (non-blocking):
   - (specific issue with file:line, or "none")

   Nits (optional):
   - (minor notes, or "none")

   What was done well:
   - (brief)

   Checklist items that may now be checked off:
   - (specific §6 items)

   Checklist items that should NOT yet be checked off:
   - (any incomplete, with reason)

   Interface-coverage evidence:
   - (requirement → method/path → schema/test; list every missing operation,
     or "none")
   ```

8. **Do not commit. Do not edit the scope file.** Your output is `.handoff/review.md`. If the verdict is APPROVED with no must-fix or should-fix items, note this clearly so the user can go straight to `03-apply-and-commit` for a quick commit.

## Done means

`.handoff/review.md` has been written with a clear verdict. The work is ready for `03-apply-and-commit`.
