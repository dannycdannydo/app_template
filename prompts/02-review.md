# Prompt 02 — Review

Paste this prompt to have the agent review the most recent uncommitted implementation against the project requirements and conventions.

---

## Project Context (read once, then act)

You are reviewing work on a reusable full-stack application starter template: FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL + Vue 3 + TypeScript + Tailwind + shadcn-vue.

The build is **stage by stage**. The current release is v0.1 (foundation). Each subsection of the checklist is one work unit, worked through a three-step loop: implement → review → apply-and-commit.

One local file governs this release:

- `TEMPLATE_V0_1_SCOPE.md` — §2 deliverables, §3 exclusions, §4 commands, §5 acceptance criteria, §6 progress checklist, §7 blueprint reference map.

The architecture blueprint (`Internal_Custom_Application_Starter_Architecture_v2.md`) is large. **Do not read the whole file.** Read only the specific blueprint sections the implementer references in their handoff summary — no more.

## Your Role

You are the **reviewer**. You did not write this code. Your job is to find problems before it is committed.

## Instructions

1. Read `.handoff/implementation.md`. This is the implementer's handoff summary — it tells you which §6 subsection was implemented and **which blueprint sections they followed**. If the file does not exist, stop and tell the user to run `01-implement-next` first.

2. Read the relevant §6 subsection in the scope file to confirm which checkbox items this work was supposed to complete.

3. Read **only** the blueprint sections the implementer referenced in the handoff file. These define the conventions you are checking against.

4. Review the changes against these lenses, in priority order:

   **Correctness** — Does it do what the task requires? Do the §4 commands actually work? Any logic errors or incomplete paths?

   **Convention compliance** — Does it follow the blueprint sections you read? Does it match existing repo patterns? Files in the right places?

   **Security** — No secrets committed, no unsafe defaults, `.env.example` accurate. Flag anything that could become a problem later.

   **Test quality** — Tests meaningful, not just smoke checks? Obvious gaps?

   **Code quality** — Clear naming, no dead code, no stubs/TODOs, no unjustified dependencies, strict types.

   **Scope discipline** — Stayed within v0.1 scope (§2)? Avoided pulling in deferred work (§3)? Flag any scope creep.

5. Run the validation commands yourself to confirm:
   - `make lint`
   - `make typecheck`
   - `make test`

6. **Write the review to `.handoff/review.md`.** This file is what the next step reads — they will not see your chat output. Use this format:

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
   ```

7. **Do not commit. Do not edit the scope file.** Your output is `.handoff/review.md`. If the verdict is APPROVED with no must-fix or should-fix items, note this clearly so the user can go straight to `03-apply-and-commit` for a quick commit.

## Done means

`.handoff/review.md` has been written with a clear verdict. The work is ready for `03-apply-and-commit`.
