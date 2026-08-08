# Template v0.5 — Scope & Progress Log

## Relationship to other documents

- `Internal_Custom_Application_Starter_Architecture_v2.md` is the long-term **design standard**; for this release the governing sections are **BP §17** (Storage Architecture, lines 851–947) and **BP §18** (Background Jobs, lines 949–1025).
- `IMPLEMENTATION_GUIDE.md` is the **build plan** and the incremental release sequence. v0.5 in this guide is *Files and Jobs* (Operations moved to v0.6). The guide names no separate design source for v0.5; ADR-0004 (use Dramatiq) and ADR-0006 (provider-neutral storage) carry the design decisions the release implements.
- This file is the **scoped contract for the v0.5 release**. It defines exact deliverables, exclusions, acceptance tests, and the commands that must work. It also serves as a progress log: check items off as they are completed.

---

# 1. Goal of v0.5

A **provider-neutral object-storage foundation with signed uploads and a durable Dramatiq job pipeline** on top of the v0.2–v0.4 identity and platform core. After v0.5, an organisation member can upload a file directly to storage through a signed URL, the application records its metadata and audits its lifecycle, and a background worker processes it with a durable, pollable job record — the core foundation for document-heavy applications. Storage SDKs stay behind a provider interface (ADR-0006), long-running work runs in Dramatiq workers never in HTTP handlers (ADR-0004), and every file and job is tenant-scoped through the existing organisation plane.

Per `IMPLEMENTATION_GUIDE.md` §8 (Definition of the First Usable Template): a fresh clone can "upload a file using a signed URL" and "enqueue and complete a Dramatiq job".

---

# 2. In Scope

```text
storage provider interface
one S3-compatible adapter
MinIO for local development
signed uploads
file metadata records
Dramatiq
Redis
durable job records
job progress polling
```

The v0.1–v0.4 foundation already ships Redis (rate limiting), the organisation permission plane with `documents.read` / `documents.upload` / `documents.delete` codes (BP §9 example set), the append-only audit service, the generated-client pipeline, TanStack Query, and the Vue shell. v0.5 builds files and jobs on that foundation; it is not a greenfield build.

Explicit deliverables:

- **Storage provider interface** (`app/storage/`): a provider-neutral `ObjectStorage` interface (create signed PUT/GET URLs, head/verify, delete, ensure bucket) plus a `FakeObjectStorage` in-memory adapter used by the pytest suite. No module outside `app/storage/` imports a provider SDK; application code depends on the interface only (ADR-0006).
- **One S3-compatible adapter**: `S3Storage` over boto3, wired from settings (`storage_provider=s3`); it works against MinIO locally and against AWS S3 / any S3-compatible service. Azure Blob and GCS adapters are explicitly deferred (guide §5: "ship S3-compatible + MinIO only").
- **MinIO for local development**: a `minio` service joins `deploy/compose/compose.local.yml` (infra set, so `make dev` starts it) and the fullstack profile; private bucket by default; the S3 adapter lazily ensures the bucket exists.
- **Signed uploads** (BP §17 direct upload flow): upload *intent* → signed PUT URL → browser uploads directly to storage → upload *complete* verifies the object → worker processes → `ready`. Object keys are server-generated (`organisations/{organisation_id}/documents/{file_id}/...`); the client never supplies an object path.
- **File metadata records**: `files` table (BP §17 shape: id, organisation_id, storage_provider, storage_bucket, object_key, original_filename, content_type, size_bytes, checksum, status pending/uploaded/processing/ready/failed/quarantined/deleted, created_by_user_id, created_at, deleted_at) via Alembic migration, with an org-scoped files module (`modules/files/`) following the existing module pattern (models / queries / service / schemas / router).
- **Dramatiq + Redis**: `dramatiq` dependency, `app/workers.py` entrypoint (`dramatiq app.workers`), worker wired into `make dev` (native, per ADR-0008) and the `dev-docker` fullstack profile (same backend image, worker command per BP §36).
- **Durable job records**: `jobs` table (BP §18 shape: id, organisation_id, job_type, status queued/running/succeeded/failed/cancelled, progress, input_reference, result_reference, error_code, error_message, attempt_count, created_by_user_id, created_at, started_at, completed_at) via Alembic migration; a job service that writes the durable row and enqueues the Dramatiq task; the worker updates status/progress; bounded retries (transient errors retried, permanent validation errors not).
- **Job progress polling**: org-scoped `GET /api/v1/jobs` (list) and `GET /api/v1/jobs/{job_id}` (status + progress) endpoints; the example file-processing job drives progress 0→100.
- **Example job**: `process_file` — after upload completion, the worker verifies the stored object, transitions the file uploaded→processing→ready (or failed/quarantined) and writes progress, closing the files↔jobs loop that both capabilities exist to demonstrate.
- **Files & jobs UI**: a `/files` documents page (upload with direct-upload progress then job-progress polling, file table with status, delete, download) built on the existing `DataTable`/form/toast building blocks, with query composables in `src/queries/files.ts` and `src/queries/jobs.ts`; generated-client refresh.
- New settings (backend-only, no new `VITE_*` variables) documented in `.env.example`: `STORAGE_PROVIDER`, `STORAGE_BUCKET`, `STORAGE_ENDPOINT_URL`, `STORAGE_PUBLIC_ENDPOINT_URL`, `STORAGE_REGION`, `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY`, `STORAGE_MAX_UPLOAD_SIZE`, `STORAGE_ALLOWED_CONTENT_TYPES`.

Permission gates: files and jobs are org-scoped through the existing active-membership check and the existing permission codes — `documents.upload` gates the upload intent and completion, `documents.read` gates list/detail/download-url and, in v0.5, the job endpoints (the files module is the only job producer; a generic `jobs.*` permission is deferred until a second producer appears, rule of three). No platform-plane involvement: files and jobs live in the organisation plane like `records`.

---

# 3. Out of Scope (Explicitly Deferred)

These are **not** part of v0.5. They appear in later releases per `IMPLEMENTATION_GUIDE.md` or are deferred by the rule of three.

| Capability | Deferred to |
| --- | --- |
| Azure Blob and GCS storage adapters (ship S3-compatible + MinIO only) | post-v1 (guide §5; ADR-0006 keeps the interface contract for when they are added) |
| Structured JSON logging, Sentry, email provider interface + one provider, basic notifications | v0.6 |
| Hybrid VPS deployment, backup and recovery documentation | v0.6 |
| Transactional outbox | post-v1 (guide §5) |
| Generic import-mapping UI, generic export framework | post-v1 (guide §5) |
| Server-Sent Events, pgvector/PostGIS setup | post-v1 (guide §5) |
| General (application-level) database-backed feature-flag framework | post-v1 (guide §5; only platform-controlled organisation flags ship in v0.4) |
| Integrated malware-scanner provider (the quarantine/failed states and the scanning hook seam ship in v0.5) | post-v1 (BP §17 security, BP §30 file security) |
| Server-side document processing beyond verify-and-mark-ready (thumbnails, OCR, extraction, transcoding) | post-v1 |
| Multipart / resumable chunked uploads (signed single-PUT covers typical sizes) | post-v1 |
| Generic job orchestration (DAGs, scheduled/cron jobs, worker dashboard/UI) | post-v1 (BP §18 example queues and heavy-workload separation) |
| Teams (`teams`, `team_memberships`) and team-specific file permissions | post-v1 (BP §9 adds teams only when required) |
| Org-level invitations / gating the unprivileged `POST /api/v1/organisations` | post-v1 (carried from v0.4; breaking change, human review required) |
| Self-service registration / public signup flows | post-v1 |
| Advanced data grids (AG Grid, Handsontable) | post-v1 (BP §16) |
| Server-side rendering, multi-language UI / i18n | post-v1 |
| Managed Azure reference deployment | post-v1 |

---

# 4. Commands That Must Work

All v0.1–v0.4 commands remain part of the quality gate. `make dev` now also starts MinIO (infra) and the Dramatiq worker (native, ADR-0008); the `dev-docker` fullstack profile gains a worker service. A new `make worker` target runs the worker natively on its own. `make generate-client` now also produces types for the files/jobs endpoints and the drift check stays in `make check`. The Alembic migration pipeline (`make migrate`) covers the new `files` and `jobs` tables.

```bash
make dev              # Postgres + Redis + MinIO in Docker; API + frontend + Dramatiq worker native with live reload (ADR-0008)
make dev-docker       # entire stack in containers (CI parity, onboarding, Dockerfile validation) incl. worker + MinIO
make worker           # run the Dramatiq worker natively (uv run dramatiq app.workers)
make migrate          # run Alembic migrations
make lint             # Ruff (backend) + ESLint/oxlint (frontend)
make typecheck        # Pyright (backend) + vue-tsc (frontend)
make test             # pytest (backend) + Vitest (frontend)
make format           # Ruff format + Prettier
make generate-client  # export OpenAPI from FastAPI -> generate TS client (openapi-typescript)
make e2e              # Playwright journeys against the local stack
make check            # full local quality gate (lint + typecheck + test + drift)
```

`make dev` for v0.5 requires the existing WorkOS variables and, for storage, the `STORAGE_*` variables (`.env.example` provides the MinIO-friendly defaults: `s3` provider, `http://localhost:9000`, `minioadmin` dev credentials, bucket `app-files`). Production fail-fast validation requires explicit S3 configuration when `STORAGE_PROVIDER=s3` (credentials, endpoint, bucket) and rejects `fake` in the production environment.

---

# 5. Acceptance Criteria

v0.5 is done when **all** of the following are true:

1. **Provider-neutral interface**: `app/storage/` exposes one `ObjectStorage` interface and at least two implementations (`S3Storage`, `FakeObjectStorage`) selected by `STORAGE_PROVIDER`; `grep -rn "boto3" backend/app | grep -v "app/storage"` is empty (SDK stays behind the adapter, BP §17 decision); the pytest suite runs fully with the fake adapter (no MinIO required for `make check`).
2. **S3-compatible adapter + MinIO**: with the `.env.example` defaults, `make dev` starts Postgres + Redis + MinIO and a signed upload round-trips through MinIO end to end; buckets are private — an unsigned GET of an object returns 403 (proven by test); the adapter lazily creates the bucket on first use; the `dev-docker` stack includes the worker and MinIO and passes the same journey.
3. **Signed upload flow (BP §17)**: `POST /api/v1/files` validates the declared filename/content-type/size, creates a `pending` file record with a server-generated object key, and returns `{file_id, upload_url, expires_at}`; the browser PUTs directly to the signed URL; `POST /api/v1/files/{file_id}/complete` verifies the object (exists, size matches the declared `size_bytes`, checksum when supplied) and marks it `uploaded`, then enqueues the processing job; request schemas use `extra="forbid"` so no client-supplied `object_key` or `storage_provider` is accepted.
4. **Files API and tenant isolation**: `GET /api/v1/files` (paginated, standard envelope, status filter), `GET /api/v1/files/{file_id}`, `GET /api/v1/files/{file_id}/download-url` (short-lived signed GET URL), `DELETE /api/v1/files/{file_id}` (soft delete via `deleted_at`, object removed from storage, audit `document.deleted`); a file from another organisation resolves to 404 (never a leak); list/detail exclude deleted files by default.
5. **Size and type validation (BP §30 file security)**: declaring a size above `STORAGE_MAX_UPLOAD_SIZE` is rejected (413/422) before any URL is issued; a disallowed content type/extension is rejected at intent time; completing with a stored object whose size does not match the declared size fails the file (`failed`/`quarantined`) and writes an audit event; the security suite covers the "oversized uploads rejected" case (BP §31 mandatory list).
6. **Durable job records (BP §18)**: the `jobs` table exists with the §18 shape; enqueuing writes the durable row (status `queued`) and then enqueues the task; the worker updates `status`, `progress`, `attempt_count`, `started_at`/`completed_at` and records `error_code`/`error_message` on failure; transient errors retry up to a bounded `MAX_ATTEMPTS` and permanent validation errors do not retry (both proven by test); there is no path where long-running work runs inside an HTTP handler (BP §18 rules).
7. **Job progress polling**: `GET /api/v1/jobs/{job_id}` returns `status` + `progress` (0–100) to a member of the same organisation and 404 to everyone else; `GET /api/v1/jobs` lists the caller's organisation's jobs with pagination and status/job_type filters; terminal states (`succeeded`/`failed`/`cancelled`) are never re-run.
8. **Example job end to end**: the full journey — intent → signed PUT → complete → `process_file` job → `ready` — passes as an integration test; in `make dev` the same journey runs against real MinIO + Redis + the worker and the frontend shows the file status advancing pending → uploaded → processing → ready.
9. **Protected-surface completeness**: every new `/api/v1` route is present in `PROTECTED_ROUTES` (`backend/tests/test_security_suite.py`) with the unauthenticated → 401, invalid-session → 401, disabled-user → 403, viewer-write → 403, cross-organisation → 404 and stack-trace non-exposure cases; the completeness guard test stays green.
10. **Frontend**: a `/files` route behind `requiresAuth` with a sidebar entry; the upload component shows direct-upload progress and then polls the job to completion; the file table shows status and supports delete/download; Vitest covers the `files`/`jobs` composables, the upload component and the files view; a Playwright journey covers the mocked upload-and-list flow; `make generate-client` produces no diff.
11. **Governance and audit**: `make check` passes from a clean checkout with zero lint errors, zero type errors, green tests and a diff-free generated client; `make e2e` passes; CI gains a Redis service on the backend-test job (durable-jobs integration test) and a MinIO-backed storage-integration job — both infrastructure changes human-reviewed per BP §33; new dependencies (`dramatiq`, `boto3`, test-only `moto`) are documented with justification; `.env.example` documents the `STORAGE_*` settings; `ARCHITECTURE.md`, `API_CONVENTIONS.md`, `SECURITY.md` (file security, SSRF, private storage, signed URLs) and `README.md` are updated; infrastructure, secret-handling (storage credentials) and major-dependency changes were human-reviewed per BP §33; the architecture audit (`prompts/04-architecture-audit.md`) reports no CRITICAL or MAJOR findings.

---

# 6. Progress Log

Check items off as they are completed. Keep one task `in_progress` at a time where practical.

Subsections are ordered so later work builds on earlier work: the storage interface precedes the adapter, the adapter precedes the files API, the job foundation precedes the file-processing job that unifies files and jobs, the UI closes the release. Dependencies are noted per subsection.

## 6.1 Storage Provider Interface

The foundation for the whole release (ADR-0006). No provider SDK outside `app/storage/`.

- [x] `app/storage/` package: `ObjectStorage` interface — `create_upload_url(file_id, object_key, content_type, size) -> SignedUrl`, `create_download_url(object_key)`, `head_object(object_key) -> ObjectInfo` (size/checksum/content-type), `delete_object(object_key)`, `ensure_bucket()` — plus `FakeObjectStorage` (in-memory, deterministic expiry, test-only)
- [x] Settings in `core/config.py`: `storage_provider` (`s3` default / `fake`), `storage_bucket`, `storage_endpoint_url`, `storage_public_endpoint_url` (defaults to endpoint_url; used for presigning when the browser cannot reach the API's storage host, e.g. dev-docker), `storage_region`, `storage_access_key_id`, `storage_secret_access_key`, `storage_max_upload_size`, `storage_allowed_content_types`; production fail-fast validation (no `fake` provider, S3 credentials/bucket/endpoint required)
- [x] `get_storage()` factory wired from settings (lru_cache singleton like `get_settings`); pytest conftest pins `storage_provider=fake`
- [x] Tests: interface contract against `FakeObjectStorage` (round-trip upload URL → put → head → delete, expiry, bucket ensure)

## 6.2 S3-Compatible Adapter + MinIO Local Development

Depends on §6.1 (interface). Makes the storage real, locally.

- [x] `S3Storage` adapter (boto3): signed PUT/GET URLs (presign host = `storage_public_endpoint_url`), `head_object`, `delete_object`, `ensure_bucket`; SDK import confined to the adapter
- [x] `minio` service in `deploy/compose/compose.local.yml` (infra set, so `make dev` starts it) + fullstack profile; dev defaults `minioadmin`/`minioadmin`, published `9000:9000`, volume; `STORAGE_*` dev defaults documented in `.env.example`
- [x] Worker wiring: `make worker` target (`uv run dramatiq app.workers`); `make dev` starts Postgres + Redis + MinIO and the API + frontend + worker natively; `dev-docker` fullstack profile gains a `worker` service (same backend image, `dramatiq app.workers` command, BP §36)
- [x] Tests: S3 adapter against MinIO (`pytest -m storage_integration` or dedicated marker, MinIO via compose or CI service); private-bucket proof (unsigned GET → 403); fake adapter used by the default suite

## 6.3 File Metadata Records and Files API

Depends on §6.1 and §6.2 (storage), §6.1's audit foundation from v0.4 (audit service reused). The org-scoped files module.

- [x] `files` table (BP §17 shape: id, organisation_id, storage_provider, storage_bucket, object_key, original_filename, content_type, size_bytes, checksum nullable, status, created_by_user_id, created_at, deleted_at) via Alembic migration; `modules/files/` models/queries/service/schemas/router registered in `db/base.py` and `main.py`
- [x] `POST /api/v1/files` (documents.upload): validate filename/content-type/size against `storage_allowed_content_types` and `storage_max_upload_size`; create `pending` record with server-generated key `organisations/{organisation_id}/documents/{file_id}/original`; return `{file_id, upload_url, expires_at}`; audit `file.upload_started`
- [x] `POST /api/v1/files/{file_id}/complete` (documents.upload): head the object, verify existence + size (+ checksum when supplied), transition uploaded → enqueue processing job (see §6.5); returns `200` with an explicit response schema — `FileDetail` (the file record with `status: uploaded`) plus `processing_job_id` (UUID of the durable job the client polls via `GET /api/v1/jobs/{job_id}`); mismatch → `failed`/`quarantined` + audit; audit `file.uploaded` *(enqueue itself deferred to §6.5 — see reviewer note)*
- [x] `GET /api/v1/files` (documents.read, paginated, status filter, standard envelope), `GET /api/v1/files/{file_id}` (documents.read), `GET /api/v1/files/{file_id}/download-url` (documents.read, short-lived signed GET URL), `DELETE /api/v1/files/{file_id}` (documents.delete: soft delete + object removal + audit `document.deleted`); cross-org file id → 404
- [x] Security suite: all files routes in `PROTECTED_ROUTES` with the full matrix (unauth 401, invalid session 401, disabled 403, viewer-write 403, cross-org 404, no stack traces); oversized-upload and disallowed-content-type cases

## 6.4 Dramatiq Worker and Durable Job Records

Depends on Redis (already in the stack) and the v0.4 audit service. Independent of §6.1–§6.3, sequenced here so §6.5 can unify files and jobs.

- [x] `dramatiq` dependency (redis broker on `REDIS_URL`); `app/workers.py` entrypoint importing all task modules; broker/worker configuration (concurrency from settings, structured logging context)
- [x] `jobs` table (BP §18 shape: id, organisation_id, job_type, status queued/running/succeeded/failed/cancelled, progress, input_reference, result_reference, error_code, error_message, attempt_count, created_by_user_id, created_at, started_at, completed_at) via Alembic migration; `modules/jobs/` models/service/schemas
- [x] Job service: `create_and_enqueue(organisation_id, job_type, input_reference, actor_user_id)` writes the durable row first, then enqueues; `mark_running` / `update_progress` / `succeed` / `fail(error_code, error_message)` helpers used by tasks; `MAX_ATTEMPTS` bounded retry policy (transient retried, permanent not)
- [x] CI infrastructure (human review required, BP §33): `redis` service added to the backend-test job so the durable-jobs integration test runs against a real broker; worker image covered by the existing container-build/scan matrix (backend image already carries the worker command)
- [x] Tests: job-record lifecycle (queued → running → succeeded, failure records error_code/error_message, attempt_count, terminal states), retry policy, idempotent `succeed`, audit row written on job completion/failure

## 6.5 File Processing Job and Job Progress Polling

Depends on §6.3 (files API) and §6.4 (job foundation). The capability that makes files and jobs demonstrably work together.

- [x] `process_file` Dramatiq task (`job_type="file.processing"`): update job progress (0→100), verify the stored object, transition the file uploaded → processing → ready; failure → file `failed` + job `failed` with `error_code`; idempotent (safe to re-run on retry)
- [x] Job endpoints: `GET /api/v1/jobs` (documents.read gate, paginated, status/job_type filters), `GET /api/v1/jobs/{job_id}` (documents.read gate, returns status + progress; cross-org → 404); both in `PROTECTED_ROUTES`
- [x] Integration test: intent → signed PUT (fake adapter) → complete → job runs (stub broker in unit tests, real broker + Redis in CI) → file `ready`, job `succeeded` with progress 100; failure path test (size mismatch → file failed/quarantined, job failed)

## 6.6 Files and Jobs Frontend

Depends on §6.3 and §6.5 (the full files/jobs API surface). The Vue documents page.

- [x] `make generate-client` regenerates types for the files/jobs endpoints; drift gate stays in `make check`
- [x] `src/queries/files.ts` composables keyed `['organisations', orgId, 'files', ...]` (list, detail, upload intent, complete, delete, download-url; invalidation after delete/complete) and `src/queries/jobs.ts` (job detail with polling via `refetchInterval` while running); no component/store imports `src/api/client.ts` directly
- [x] Router: `/files` route (`name: 'files'`, `meta.requiresAuth`) + `SidebarNav` entry; `FilesListView` with the existing `DataTable` (status badge, size, uploaded-at timestamp, actions), upload component (file picker → intent → direct PUT with `XHR` progress → complete → poll job progress bar → refresh), delete confirm, download link *(uploaded-by shows the created timestamp, not a user name: `FileListItem` carries only `created_by_user_id`, and a human-readable name needs a backend join outside this frontend-only unit)*
- [x] Vitest: files/jobs composables, upload component (mock PUT + polling), files view; Playwright journey: upload intent + file appears in the table (mocked `/api/v1/**` surface per existing e2e pattern)

## 6.7 Docs, ADR & Release Governance

Depends on §6.6 (exercises the release). Closes v0.5.

- [ ] Blueprint amendments applied where the release proves gaps (BP §17, §18, §30, §31 — see §7 of this file); ADR note or new ADR recording the boto3 S3 adapter as the first storage adapter implementation (ADR-0006 contract) and the `documents.*` gating decision for files/jobs
- [ ] `ARCHITECTURE.md` (storage interface, direct upload flow, worker/request flow), `API_CONVENTIONS.md`, `SECURITY.md` (file security, SSRF, private storage, signed URLs) and `README.md` updated; `.env.example` documents the `STORAGE_*` settings and worker commands
- [ ] CI changes landed and green: Redis service on backend-test, MinIO-backed storage-integration job; `make check` green from a clean checkout; generated-client drift clean; Playwright job green including the files journey
- [ ] Human review recorded for infrastructure changes (MinIO, worker, CI services), secret handling (storage credentials), and major dependency additions (dramatiq, boto3, moto) per BP §33; architecture audit clean (no CRITICAL/MAJOR)

---

# 7. Blueprint Reference Map

Each scope subsection (left column) maps to specific sections of `Internal_Custom_Application_Starter_Architecture_v2.md`. Implementers and reviewers read **only** the listed sections for a given task — not the whole blueprint. This keeps context lean and focused.

## Disambiguating section numbers

Two documents use the `§` symbol. Do not confuse them:

- **Scope §6.x** — a subsection of *this file's* checklist (e.g. Scope §6.3 = "File Metadata Records and Files API").
- **BP §N** — a section of the *blueprint* (e.g. BP §17 = "Storage Architecture", starting at line 851).

The map below always uses `BP §N` for blueprint sections and `Scope §6.x` for this file's checklist. **Never** write a bare `§6` or `§17` — always prefix with `Scope` or `BP` so the next reader knows which document you mean.

## Map

Line ranges were verified against the blueprint's table of contents and by reading each section's start and end (the range ends at the last content line before the next `#` heading). v0.4's recorded ranges predate the v0.4 blueprint amendments and are not reused; these numbers reflect the current file.

| Scope subsection | Blueprint sections | What to extract |
| --- | --- | --- |
| **Scope §6.1** Storage Provider Interface | **BP §17** (lines 851–947), **BP §10** (lines 496–575), **BP §30** (lines 1513–1575) | Provider-neutral interface decision and the adapter list, file-metadata field set, database conventions for the new columns (UUIDv7 ids, timestamps, constraints, naming), private-storage and file-security controls the interface must enable |
| **Scope §6.2** S3-Compatible Adapter + MinIO | **BP §17** (lines 851–947), **BP §36** (lines 1881–1930), **BP §32** (lines 1636–1685) | Object keys, direct upload flow and initial-implementation strategy (S3 + MinIO only), worker command and compose conventions (ADR-0008 native model), dependency rules (one package manager, justified additions, lock files) |
| **Scope §6.3** File Metadata Records and Files API | **BP §17** (lines 851–947), **BP §9** (lines 385–494), **BP §11** (lines 577–595), **BP §12** (lines 597–667), **BP §13** (lines 669–717), **BP §29** (lines 1465–1511), **BP §30** (lines 1513–1575) | File lifecycle and security (backend-controlled keys, client submits file id never an object path), organisation as isolation boundary and the `documents.*` permission set, service-owned transaction boundaries for the intent/complete steps, pagination and filtering conventions, structured error envelope, audit examples (`document.deleted`), file security (MIME/size limits, quarantine, no public read) |
| **Scope §6.4** Dramatiq Worker and Durable Job Records | **BP §18** (lines 949–1025), **BP §11** (lines 577–595), **BP §28** (lines 1422–1463), **BP §36** (lines 1881–1930), **BP §37** (lines 1932–1973) | Job table shape, statuses and rules (idempotency, bounded retries, queues, worker concurrency), atomic record-then-enqueue, logging context for the worker, worker command and compose wiring, CI checks the new infra must not break |
| **Scope §6.5** File Processing Job and Job Progress Polling | **BP §18** (lines 949–1025), **BP §17** (lines 851–947), **BP §9** (lines 385–494), **BP §12** (lines 597–667) | Task-writing conventions and terminal states, the processing step of the direct-upload flow, org-scoped rules for the job endpoints, pagination/filter conventions for the job list |
| **Scope §6.6** Files and Jobs Frontend | **BP §14** (lines 719–774), **BP §15** (lines 776–810), **BP §16** (lines 812–849), **BP §12** (lines 597–667) | Frontend folder structure and state boundaries (server state in queries, client state in Pinia), generated-client rules (never hand-write duplicates, drift in CI), design-system rules (reusable application components above primitives), pagination conventions for the tables |
| **Scope §6.7** Docs, ADR & Release Governance | **BP §31** (lines 1577–1634), **BP §32** (lines 1636–1685), **BP §33** (lines 1687–1738), **BP §37** (lines 1932–1973), **BP §42** (lines 2109–2129) | Mandatory reusable security tests including "oversized uploads rejected", integration-test priority (background-job creation, audit), dependency rules, coding-agent governance and the human-review list (infrastructure, secrets, major dependencies), CI checks (Redis/MinIO services, migration validity, container build), template validation (start local services, migrations, client drift) |

If a task touches a concern not listed here (e.g. the security baseline details for a specific control), consult the blueprint's table of contents and read only the relevant section. When in doubt, read less rather than more — this file's §2–§5 already encodes the v0.5 contract, and ADR-0004 / ADR-0006 carry the design rationale.

---

# 8. Status

```text
Release:    v0.5.0 (files and jobs)
State:      in progress
Started:    —
Completed:  —
```

When every acceptance criterion in §5 is met and every box in §6 is checked, update the version recording in `pyproject.toml` and `frontend/package.json`, and tag `v0.5.0`. Then open `TEMPLATE_V0_6_SCOPE.md`.
