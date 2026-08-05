# Prompt 00 — Draft Next Scope File

Paste this prompt once per release, before the daily implement → review → apply-and-commit loop, to draft the scope file for the next release.

---

## Project Context (read once, then act)

You are planning the next release of a reusable full-stack application starter template: FastAPI + SQLAlchemy 2 + Pydantic 2 + PostgreSQL + Vue 3 + TypeScript + Tailwind + shadcn-vue, modular monolith, WorkOS auth, Dramatiq jobs, provider-neutral storage.

The build is **stage by stage** (v0.1 and v0.2 shipped: foundation, then identity and tenancy; v0.3 is next). The release sequence and each release's capability list are defined in `IMPLEMENTATION_GUIDE.md`. Each release gets its own contract file, `TEMPLATE_V0_N_SCOPE.md`, following the structure of the previous release's scope file.

Read, in this order:

1. `IMPLEMENTATION_GUIDE.md` — find the section titled `Template v0.N — <Title>` for the next unshipped release (the lowest-numbered release without a `TEMPLATE_V0_N_SCOPE.md`).
2. The most recent scope file (highest-numbered `TEMPLATE_V0_N_SCOPE.md` in the repo root, currently `TEMPLATE_V0_2_SCOPE.md`) — this is your structural template, section by section.
3. `Internal_Custom_Application_Starter_Architecture_v2.md` — the design standard. **Do not read the whole file.** Read its table of contents to locate sections relevant to the next release's capabilities, then read only those sections.

## Your Role

You are the **scope planner**. You produce the contract the daily loop will execute against — nothing more. No implementation.

## Instructions

1. Identify the next release: the lowest-numbered release in `IMPLEMENTATION_GUIDE.md` that has no `TEMPLATE_V0_N_SCOPE.md` yet. Read its full section.

2. Read the previous scope file completely as your template. Your output must mirror its eight sections:

   - `# 1. Goal` — what the release must deliver, in one or two sentences.
   - `# 2. In Scope` — the capability list from the guide's "Adds:" block, plus any explicitly named deliverables.
   - `# 3. Out of Scope` — everything the guide defers to later releases, as a table with a "Deferred to" column. Copy the pattern from the previous scope file's §3.
   - `# 4. Commands That Must Work` — inherit the previous release's commands that still apply; add any new ones the capabilities require.
   - `# 5. Acceptance Criteria` — one measurable, verifiable criterion per key capability (numbered), plus a governance/audit criterion.
   - `# 6. Progress Log` — one subsection per coherent capability group (e.g. §6.1, §6.2), each with `- [ ]` checkbox items granular enough to be one work unit for the daily loop. Batch closely related items.
   - `# 7. Blueprint Reference Map` — map each §6 subsection to the specific blueprint sections that govern it, with **line ranges you verified** from the blueprint's table of contents. Use the exact format of the previous scope file's §7 (two-column table, "What to extract").
   - `# 8. Status` — release name, state, started/completed dates.

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

6. **Write the scope file** as `TEMPLATE_V0_N_SCOPE.md` in the repo root.

7. **Write a handoff summary to `.handoff/scope.md`** (gitignored) so the reviewer's session can pick it up:

   - which release, and the guide section it came from;
   - the §6 subsections and their ordering rationale;
   - which blueprint sections you mapped and how you verified their line ranges;
   - decisions made where the guide or blueprint was silent;
   - open questions for the reviewer.

8. **Do not commit. Do not implement anything.** Leave the scope file uncommitted so the reviewer can inspect it.

## Done means

`TEMPLATE_V0_N_SCOPE.md` exists in the repo root and `.handoff/scope.md` has been written. The plan is ready for `00b-scope-review`.
