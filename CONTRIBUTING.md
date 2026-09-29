# Contributing

This repository is a maintained template product. Changes go through a three-step loop per work unit: **implement → review → apply-and-commit**.

## Workflow

1. Discover the active execution contract: use the unique `plans/*.md` file
   containing the exact line `Status: Active`, or otherwise the
   highest-numbered `TEMPLATE_V0_*_SCOPE.md` in the repository root. More than
   one active plan is an error.
2. Pick its next unchecked work unit in sequence: one scope §6 subsection or
   one plan `### Pn` checkpoint. Read the governing sources listed for that unit
   in the contract's reference map, plus existing repo patterns. Do not invent
   conventions that contradict the blueprint.
3. **Implement** fully and end-to-end: real working files, configuration wired, tests where they naturally belong.
4. Run focused tests and static checks immediately after changes. Use the
   smallest commands that cover the affected behaviour; do not defer known
   focused-check failures. Implementation never runs complete backend/frontend
   suites or `make check`, regardless of scope, sensitivity, risk or contract
   wording. Complete commands listed by a contract are deferred to the final
   post-review gate.
5. Write a handoff summary for the reviewer (active contract path, work unit,
   files changed with one-line purposes, checklist items to check, governing
   sections followed, decisions/deviations, human-review gates, review focus and
   validation results).
6. **Do not commit and do not check off boxes** until the reviewer has inspected the diff.
7. The reviewer inspects the diff and validation evidence, runs focused checks
   where they add value, and flags issues. Review never runs a complete suite
   or repository gate.
8. The implementer addresses the findings and runs `make check` once on the
   final reviewed state, plus any additional contract commands. Only after that
   gate is green is the work applied and committed and the §6 boxes checked.

## Branch workflow

CI runs the full quality gate on every push to `main` and on every pull request (see `.github/workflows/ci.yml`). Pushes to any other branch trigger nothing. Work therefore happens on branches, and `main` only ever changes through a reviewed, merged pull request:

1. Start each work unit on its own branch: `git checkout -b feature/<unit>`
   (the subsection/checkpoint name from the active contract).
2. Run the implement → review → apply-and-commit loop on that branch: implement uncommitted, have the reviewer inspect the diff (steps 1–6 above), then commit.
3. Push the branch after the reviewed commit: `git push -u origin feature/<unit>`. This does not start a build.
4. Open a pull request to `main` when the work unit is complete. CI runs on the PR; it must be green.
5. Merge the PR. This single merge to `main` is the one CI run per work unit.

Do not push directly to `main` and do not merge your own PR without review. Keep the branch history clean with the commit style below, and delete the branch after merge.

## Commit style

Commit messages are for future readers scanning history. Rules:

- One sentence that explains **why** the change exists and what outcome it enables, not a list of files.
- First line under 72 characters.
- Use clear, accurate verbs: `add` (new capability), `update` (enhancement), `fix` (bug fix).
- Add a body only when it explains reasoning, tradeoffs, or context.
- Never commit secrets, `.env` files, or unrelated changes.

## Review requirements

The following changes require human review before they are applied (also in `AGENTS.md`):

- authentication changes;
- permission-model changes;
- tenant-isolation changes;
- destructive migrations;
- secret handling;
- public API breaks;
- infrastructure changes;
- backup and recovery changes;
- major dependency additions.

## Quality gate

`make check` must pass with zero lint errors, zero type errors, and green tests before any release. CI runs the same gate on push. Do not weaken linting, typing, or tests to make things pass.

## Versioning and releases

The template is released as immutable git tags named `vMAJOR.MINOR.PATCH`
(blueprint §41). The repository tracks each post-foundation release in the
highest-numbered `TEMPLATE_V0_N_SCOPE.md`; its `# 8. Status` block records the
release, state, start and completion dates.

- The released version is recorded in three places, which must always agree:
  `backend/pyproject.toml` `[project].version`, `frontend/package.json`
  `version`, and `[tool.project-template].version` in `backend/pyproject.toml`
  (the blueprint §41 `[tool.project-template]` convention that lets clone
  consumers infer the implemented template release).
- A release is only `State: complete` once every scope checkpoint is checked
  after review and package versions match the tag's `MAJOR.MINOR.PATCH`.
- Tags are immutable. A bookkeeping mismatch (for example a scope still marked
  planned) is corrected in a new reviewed commit; never move or rewrite a tag.
- Each release adds an upgrade guide under `docs/upgrades/` (for example
  `docs/upgrades/0.7-to-0.8.md`) covering changed files, new dependencies,
  configuration, migrations, security implications and manual adoption steps.
- `backend/tests/test_release_versions.py` enforces the version/scope
  consistency so a release cannot drift out of step with its scope document.

## Porting template changes to derived apps

Apps built from this template can take later template fixes and features by
cherry-picking the template's reviewed commits, rather than re-running the
template's plans. Git cherry-pick replays a commit's diff with a three-way
merge, so this works even when the app was created with GitHub's "Use this
template" and shares no history with the template.

### Keeping the template portable

- Keep each template change in focused commits: one work unit per commit, no
  unrelated edits mixed in, so an app can take exactly the change it needs.
- Tag every release (see *Versioning and releases*) and keep the
  `docs/upgrades/` guide current. The guide is the checklist a derived app
  follows after porting.

### Keeping an app portable

- Record the template release the app is synced to in
  `[tool.project-template].version` in the app's `backend/pyproject.toml`.
  Update it only after a full release has been ported.
- Put app features in new modules, routes, views and migrations. Keep edits to
  template-owned core files (`backend/app/core/config.py`, CI workflows,
  Compose files, `AGENTS.md` guides, shared docs) small and together in one
  place, because conflicts only occur where both sides edit the same lines.

### Porting procedure

1. In the app repository, add the template as a remote once:
   `git remote add template <template-repo-url>`, then `git fetch template --tags`.
2. List what is new since the app's recorded version:
   `git log --oneline --no-merges <synced-tag>..template/main`.
3. Check how much the change overlaps the app's customisations. Compare
   `git diff --name-only <first-commit>^..<last-commit>` (the template range)
   with `git diff --name-only <app-base>..HEAD` (the app's changes). Little
   overlap means cherry-pick; heavy overlap means port with a prompt (below).
4. On a dedicated branch, cherry-pick the range in order:
   `git cherry-pick <first-commit>^..<last-commit>`. Skip commits that are not
   part of the change being ported.
5. Resolve conflicts by keeping the template's intent and the app's
   customisation together. Then work through the gotchas below.
6. Run the full `make check`, plus `make e2e` and any service-backed CI jobs
   the change touches, in the app. A clean cherry-pick shows that the diff
   applied, not that it works with the app's customisations.
7. The same human-review categories apply as in the template (see *Review
   requirements*). Approval given in the template does not cover the app's
   conflict resolutions.

### Gotchas

- **Alembic migrations.** A template migration's `down_revision` points to the
  template's previous head, not the app's. After cherry-picking, re-parent it
  onto the app's current head and run `alembic heads` to confirm a single head.
  If both sides changed the same table, review the combined schema by hand.
- **Generated API client.** Never hand-merge `frontend/src/api/generated/`.
  Port the backend changes, then run `make generate-client`.
- **Lockfiles.** Resolve `backend/uv.lock` and `frontend/pnpm-lock.yaml`
  conflicts by taking the app's version and re-running `uv lock` /
  `pnpm install`. Do not merge them line by line.
- **Plans and handoff files.** Port a template plan file only as a reference.
  Do not make it `Status: Active` in the app, because its checkboxes are
  already done.

### Porting with a prompt

When an app has drifted too far for a clean cherry-pick, export the reviewed
commits as patches and give them to an agent as a porting brief:

```bash
git format-patch <first-commit>^..<last-commit> -o <app>/.handoff/template-patches
```

The prompt should say that the patches are already reviewed and approved in
the template, so the agent ports their intent and does not redesign them. The
agent keeps pinned versions, digests, security decisions and test assertions
exactly as they are, adapts only what the app's customisations force, and
lists every adaptation in its handoff. Include the relevant plan, ADRs and
findings docs so the agent has the reasoning behind each decision. The ported
work then goes through the normal implement → review → apply-and-commit loop.

## Dependency policy

Do not add dependencies without documenting why. Substantial additions should be recorded in an ADR (see `docs/decisions/`).

## Tests

- Tests accompany behavioural changes.
- Backend: pytest against real PostgreSQL for integration tests (unit tests for pure logic).
- Frontend: Vitest for unit tests; Playwright for critical end-to-end journeys.
- Integration tests are the most important layer.

## Docs

- Foundational changes must update or supersede the relevant ADR.
- Architecture changes update `ARCHITECTURE.md` and `API_CONVENTIONS.md` as appropriate.
- Every variable the app reads is documented in `.env.example`.
