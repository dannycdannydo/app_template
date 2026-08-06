# ADR 0008: Local Development Model — Native App Code, Containerised Infrastructure

Status: Accepted

## Context

Blueprint §36 mandates that the template ships Dockerfiles and a set of Compose files, but it is intentionally silent on *how* application code runs during day-to-day local development. As the v0.1 §6.7 work unit was scoped, the scope file drifted into an internal contradiction: §2 lists "Docker Compose for local development (PostgreSQL, Redis)" (infrastructure only), while §6.7 required `compose.local.yml` to run PostgreSQL, Redis, the API, and the frontend with "volume mounts for live reload."

Bind-mounting application source into containers for live reload is a known source of friction: slower reloads, file-watcher edge cases across host/container boundaries, harder debugger attachment, and a worse experience for coding agents, which must wrap every command in `docker compose exec` instead of invoking `uv run` or `pnpm` directly. This decision resolves the contradiction and records the canonical local development model so that v0.1 §6.7 implements a single, consistent approach.

## Options considered

- **Everything in Docker (full container stack, source bind-mounted for reload)**: maximum environment parity and a one-command onboarding story, but slower iteration, debugger/bind-mount pain, and the worst experience for coding-agent workflows.
- **Native app code + containerised infrastructure only (hybrid)**: Vue, FastAPI, and the worker run on the host (`pnpm dev`, `uvicorn --reload`, `dramatiq`); PostgreSQL and Redis run in Docker. Fastest reload, trivial debugging, and agents invoke tooling directly. Shifts "works on my machine" risk onto the host toolchain.
- **Native-only (no Docker at all)**: rejected — infrastructure versions must be pinned and reproducible (blueprint principle #14 and §44 step 2 require containerised local infrastructure to avoid version drift).

## Decision

Adopt the **hybrid** model as the default for day-to-day development:

```text
Host (native)                  Docker Compose
─────────────────              ──────────────────────
Vue (pnpm dev)                 PostgreSQL
FastAPI (uvicorn --reload)     Redis
Dramatiq (uv run dramatiq)     (MinIO, Mailpit in later releases)
```

- `make dev` is the canonical development command. It starts the infrastructure services from `deploy/compose/compose.local.yml` and launches the API and frontend natively with live reload.
- A second command, `make dev-docker`, runs the **entire** stack (API, frontend, worker, Postgres, Redis) in containers. It exists for CI parity, fresh-clone onboarding verification, Dockerfile validation, and deployment debugging — not for daily use.
- `compose.local.yml` carries the local services. Docker Compose profiles (or an equivalent mechanism within the single file) separate the infra-only set from the full-stack set, so both commands are served from the blueprint's existing three-file Compose layout (BP §36) without adding a fourth file.
- `backend/Dockerfile` and `frontend/Dockerfile` remain required: they serve CI, both production profiles (BP §35), and the `make dev-docker` path.

## Consequences

- The host must provide the application toolchains (Python 3.13 + `uv`, Node + `pnpm`). This is documented in the README clean-clone procedure and `.env.example`.
- Because `make dev-docker` is the 5%-path, **CI must exercise the full-Docker path** (blueprint §42 already requires a fresh-clone "start local services" validation); otherwise it rots into a broken onboarding trap.
- Future infrastructure services (MinIO, Mailpit, LocalStack) are added to `compose.local.yml` as their releases arrive, not retrofitted into v0.1.
- This ADR supersedes the v0.1 §6.7 wording that required volume-mount live reload; the v0.1 scope file §6.7 is amended accordingly to match this decision and §2.

---
