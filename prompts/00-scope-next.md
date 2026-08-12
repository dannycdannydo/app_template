# Prompt 00 — Draft Next Scope File

Paste this prompt once per release, before the daily implement → review → apply-and-commit loop, to draft the scope file for the next release.

---

## Project Context (read once, then act)

You are planning the next release of a reusable full-stack application starter template: FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL + Vue 3 + TypeScript + Tailwind + shadcn-vue, modular monolith, WorkOS auth, Dramatiq jobs, provider-neutral storage.

The build is **stage by stage**. The original release sequence is defined in
`IMPLEMENTATION_GUIDE.md`; supplementary post-foundation releases may instead
be introduced by a named `plans/*` design source linked from the latest scope.
Each release gets its own contract file, `TEMPLATE_V0_N_SCOPE.md`, following the
structure of the previous release's scope file.

Read, in this order:

1. `IMPLEMENTATION_GUIDE.md` and the latest scope's relationship/deferred
   sections. Identify the lowest missing release defined by either a guide
   section or a linked `plans/*` release-design source. Prefer a guide section
   when both define the same release. A standalone `Status: Active` maintenance
   plan is not a release source.
2. The most recent scope file (highest-numbered `TEMPLATE_V0_*_SCOPE.md` in the
   repo root) — this is the structural template, section by section.
3. Every release-design source identified in step 1.
4. `Internal_Custom_Application_Starter_Architecture_v2.md` — the design
   standard. **Do not read the whole file.** Read its table of contents, then
   only the sections relevant to the next release.

## Your Role

You are the **scope planner**. You produce the contract the daily loop will execute against — nothing more. No implementation.

## Instructions

1. Identify the next release using the source precedence above. If neither the
   guide nor a linked release-design source defines a missing release, stop and
   report that no release has been proposed. Read the selected source fully.

2. Read the previous scope file completely as your template. Your output must mirror its eight sections:

   - `# 1. Goal` — what the release must deliver, in one or two sentences.
   - `# 2. In Scope` — the selected source's capability list, fixed decisions
     and explicitly named deliverables.
   - `# 3. Out of Scope` — everything the source defers, as a table with a
     "Deferred to" column. Copy the previous scope's pattern.
   - `# 4. Commands That Must Work` — inherit the previous release's commands that still apply; add any new ones the capabilities require.
   - `# 5. Acceptance Criteria` — one measurable, verifiable criterion per key capability (numbered), plus a governance/audit criterion.
   - `# 6. Progress Log` — one subsection per coherent capability group (e.g. §6.1, §6.2), each with `- [ ]` checkbox items granular enough to be one work unit for the daily loop. Batch closely related items.
   - `# 7. Blueprint Reference Map` — map each §6 subsection to the specific blueprint sections that govern it, with **line ranges you verified** from the blueprint's table of contents. Use the exact format of the previous scope file's §7 (two-column table, "What to extract").
   - `# 8. Status` — release name, state, started/completed dates.

   The scope may be more concise than a release-design source, but it must not
   omit a required behaviour. An unresolved material decision must be resolved
   in the scope or explicitly block activation; never convert “if needed” or a
   provider choice into an executable checkbox.

3. For each capability in the guide's section, locate the governing blueprint sections:

   - Use `grep -n "^# \|^## "` on the blueprint to get its table of contents with line numbers.
   - Read each candidate section to confirm it actually governs the capability before mapping it.
   - Include cross-cutting sections that apply (database conventions, API design, errors, security baseline, testing strategy, coding-agent governance) whenever the release touches those concerns.

4. Order the §6 subsections so later work builds on earlier work (e.g. data model before services before routes; auth context before tenant-scoped modules). Note dependencies between subsections.

5. Check these edge cases:

   - New commands required by the capabilities (e.g. a migration or generation step) must appear in §4.
   - Anything deferred must appear in §3 — never silently dropped.
   - Every §5 criterion must be objectively verifiable by an agent (a command to run, a response code, a diff-free regeneration).
   - Every §7 mapping must point at real, verified line ranges.
   - Build a **capability traceability matrix** before writing the scope. For
     every externally observable capability, record: source requirement,
     §5 acceptance criterion, §6 checkbox/work unit, backend operation
     (method + path, where applicable), frontend consumer (route/view, where
     applicable), and test evidence. A capability is not covered merely
     because a nearby create or mutation endpoint exists: list, detail, edit,
     delete, pagination and filtering are distinct operations when the source
     calls for them.
   - Turn that matrix into explicit §6 checkbox text. Do not leave an endpoint
     implied by a later UI item or a broad phrase such as "organisation
     administration". Include the method and path for new API operations.

6. **Write the scope file** as `TEMPLATE_V0_N_SCOPE.md` in the repo root.

7. **Write a handoff summary to `.handoff/scope.md`** (gitignored) so the reviewer's session can pick it up:

   - which release and authoritative guide/design source it came from;
   - the §6 subsections and their ordering rationale;
   - which blueprint sections you mapped and how you verified their line ranges;
   - decisions made where the guide or blueprint was silent;
   - the capability traceability matrix (or a path to it) and any source
     requirement deliberately deferred, with its justification;
   - open questions for the reviewer.

8. **Do not commit. Do not implement anything.** Leave the scope file uncommitted so the reviewer can inspect it.

## Done means

`TEMPLATE_V0_N_SCOPE.md` exists in the repo root and `.handoff/scope.md` has been written. The plan is ready for `00b-scope-review`.
