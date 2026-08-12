# Prompt 02 — Review

Paste this prompt to have the agent review the most recent uncommitted implementation against the project requirements and conventions.

---

## Project Context (read once, then act)

You are reviewing work on a reusable full-stack application starter template: FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL + Vue 3 + TypeScript + Tailwind + shadcn-vue.

The build is **stage by stage**. The implementer's handoff names the active
execution contract: either a `Status: Active` standalone plan in `plans/` or a
root `TEMPLATE_V0_*_SCOPE.md`. Each subsection/checkpoint is one work unit,
worked through implement → review → apply-and-commit.

One active contract governs the work. Read the exact path from
`.handoff/implementation.md`; do not independently switch to a newer scope or
another plan mid-review. Verify that a plan still says `Status: Active`.

The architecture blueprint (`Internal_Custom_Application_Starter_Architecture_v2.md`) is large. **Do not read the whole file.** Read only the specific blueprint sections the implementer references in their handoff summary — no more.

## Your Role

You are the **reviewer**. You did not write this code. Your job is to find problems before it is committed.

## Instructions

1. Read `.handoff/implementation.md`. This is the implementer's handoff summary
   — it tells you which contract/work unit was implemented and **which governing
   sections they followed**. If the file does not exist, stop and tell the user
   to run `01-implement-next` first.

2. Read the named contract's relevant §6 subsection or `Pn` checkpoint to
   confirm which checkbox items this work was supposed to complete. If the
   handoff omits the contract path, stop and request a corrected handoff.

3. Read **only** the governing sections the implementer referenced in the
   handoff file. Also read the active contract's scope, acceptance criteria and
   full checkpoint dependency chain needed for interface closure. If it names a
   design source and is ambiguous, read only the relevant deliverable/API/data/
   frontend tables. This is contract verification, not a blanket blueprint read.

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

   **Correctness** — Does it do what the task requires? Do the contract's
   commands actually work? Any logic errors or incomplete paths?

   **Convention compliance** — Does it follow the blueprint sections you read? Does it match existing repo patterns? Files in the right places?

   **Security** — No secrets committed, no unsafe defaults, `.env.example` accurate. Flag anything that could become a problem later.

   **Test quality** — Tests meaningful, not just smoke checks? Obvious gaps?

   **Code quality** — Clear naming, no dead code, no stubs/TODOs, no unjustified dependencies, strict types.

   **Scope discipline** — Stayed within the current release's scope (§2)? Avoided pulling in deferred work (§3)? Flag any scope creep.

6. Run the validation commands yourself to confirm:
   - `make lint`
   - `make typecheck`
   - `make test`
   - every additional command required by the active checkpoint/contract.

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

   Human-review gate:
   - (required categories and whether recorded approval exists, or "none")

   Interface-coverage evidence:
   - (requirement → method/path → schema/test; list every missing operation,
     or "none")
   ```

8. **Do not commit. Do not edit the active contract.** Your output is
   `.handoff/review.md`. Code may receive an `APPROVED` verdict while a separate
   required human-review gate remains pending, but record that gate explicitly;
   prompt 03 must stop until approval is recorded.

## Done means

`.handoff/review.md` has been written with a clear verdict. The work is ready for `03-apply-and-commit`.
