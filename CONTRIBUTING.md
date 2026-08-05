# Contributing

This repository is a maintained template product. Changes go through a three-step loop per work unit: **implement → review → apply-and-commit**.

## Workflow

1. Pick the next unchecked work unit in `TEMPLATE_V0_1_SCOPE.md` §6 (in sequence).
2. Read the blueprint sections listed for that unit in §7 of the scope file, plus existing repo patterns. Do not invent conventions that contradict the blueprint.
3. **Implement** fully and end-to-end: real working files, configuration wired, tests where they naturally belong.
4. Run the quality gate immediately after changes: `make lint`, `make typecheck`, `make test`, plus any other relevant check from the scope file §4. Fix anything that fails.
5. Write a handoff summary for the reviewer (subsection completed, files changed with one-line purposes, §6 items to check, blueprint sections followed, decisions made where the blueprint was silent, deviations, what the reviewer should focus on, validation results).
6. **Do not commit and do not check off boxes** until the reviewer has inspected the diff.
7. The reviewer inspects the diff and the handoff, flags issues, and the implementer addresses them. Only then the work is applied and committed, and the §6 boxes are checked.

## Branch workflow

CI runs the full quality gate on every push to `main` and on every pull request (see `.github/workflows/ci.yml`). Pushes to any other branch trigger nothing. Work therefore happens on branches, and `main` only ever changes through a reviewed, merged pull request:

1. Start each work unit on its own branch: `git checkout -b feature/<unit>` (the unit name from `TEMPLATE_V0_1_SCOPE.md` §6).
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
