# ADR 0020: Record Versioning, Immutable Revisions and Append-Only Audit

Status: Accepted

## Context

The v0.2 representative records module was last-write-wins and audited only by
an action-only `record.updated` event. Two writers could silently overwrite each
other, the audit trail could not reconstruct what a record contained before a
change, and a hard delete left no recoverable history. The blueprint asks
collaboratively edited records to carry an integer `version` with stale writes
returning `409` (blueprint §10) and audit events to be append-only (§29).

`audit_events` was append-only only by convention: no update/delete endpoint
existed, but nothing stopped a direct `UPDATE`/`DELETE`, and its
`actor_user_id`/`organisation_id` foreign keys used `ON DELETE SET NULL`, so
deleting a referenced user or organisation silently erased the actor/tenant
provenance the trail exists to preserve.

## Options considered

1. **Database trigger rejecting mutation** (chosen for append-only). Rejects
   `UPDATE`/`DELETE` on `audit_events` and `record_revisions` in PostgreSQL, so
   the guarantee holds for any writer, not just the API. A separately held
   immutable export was the alternative the plan allowed, but it adds
   infrastructure for a weaker (external-copy) guarantee.
2. **Full event sourcing for records** (rejected). The plan scopes revisions to
   a narrow operational ledger for one representative record, not a
   whole-system rewrite.
3. **Revision rows that cascade with the record** (rejected). Hard-deleting a
   record would then erase the history that must reconstruct it; the ledger
   keeps `record_id` as an opaque UUID instead.
4. **Keep referential actions on the append-only tables** (rejected). A
   referential action is itself an `UPDATE`/`DELETE`, which the append-only
   trigger must reject, and `SET NULL` destroys provenance.

## Decision

- **Optimistic concurrency.** `records.version` starts at 1. The PATCH body and
  the DELETE `version` query parameter carry the version the caller last read;
  the service locks the row `FOR UPDATE`, compares, and a stale value is
  `409 record_version_conflict`. Responses expose the current version, and the
  generated frontend types drive the conditional payload/parameter.
- **Immutable history.** Every accepted create/update/delete writes one
  `record_revisions` row in the same transaction: the bounded `title`/`body`
  snapshot, the record version, the actor, and a closed
  `created`/`updated`/`deleted` action. There is no public endpoint; the ledger
  is internal provenance. `audit_events.metadata` carries only the version
  number, never record content.
- **Provenance survives deletion.** `record_revisions.record_id`/`actor_user_id`
  and `audit_events.organisation_id`/`actor_user_id` are opaque non-foreign-key
  UUIDs. Hard-deleting a record or user leaves the ledger intact.
- **Database-enforced append-only.** Two triggers on each table reject
  mutation: a row-level `BEFORE UPDATE OR DELETE` trigger, plus a
  statement-level `BEFORE TRUNCATE` trigger. `TRUNCATE` fires no row-level
  trigger, and the application connects as the table-owning role, so without
  the statement-level trigger a direct `TRUNCATE audit_events`/
  `TRUNCATE record_revisions` could empty the ledger. The triggers and the
  absence of referential actions are deliberately paired: a cascade or
  `SET NULL` would mutate the append-only rows.
- **Hard delete, no restore.** Deletion is permanent. There is deliberately no
  record-level restore operation or endpoint; the service exposes
  `restore_record` only to reject it with `record_restore_unsupported`. The
  immutable revisions are reconstruction *evidence* for a human/operator, not a
  programmatic resurrection, and the closed `created`/`updated`/`deleted`
  revision actions have no `restored` member.

## Reviewed retention tradeoff

The plan's human-review gate covers tenant isolation, retention and audit
privacy. This is the recorded decision:

- The revision/audit rows retain an **opaque identifier only** — no email, name,
  provider response or document content beyond the record's own API-bounded
  `title`/`body`. The identifier is pseudonymous, not a directory of people.
- Because the tables are append-only, **tenant erasure is not automatic**:
  deleting an organisation cannot cascade into `record_revisions` or
  `audit_events`. Erasing a tenant's business history requires a separately
  reviewed purge that deliberately disables the trigger (or a future
  partition-drop strategy). This is the tradeoff for tamper-evident provenance.
- `audit_events.actor_user_id` is retained after the user row is removed. GDPR
  erasure of an individual actor therefore needs the same reviewed purge.
- The downgrade of the P8 migration is **lossy** for orphaned identities (it
  nulls `actor_user_id`/`organisation_id` that no longer have a referent) so the
  prior foreign-key schema can be restored. Downgrades are for rollback/local
  work, never the documented production path.

## Human review

Approved by the template owner on 2026-09-17 as part of the P8
apply-and-commit gate. The recorded approval covers every category the
checkpoint names:

- **public API:** the additive `version` on record responses and the breaking
  requirement that PATCH carry `version` in the body and DELETE carry it as the
  `?version=` query parameter, with the regenerated client covering all
  first-party callers.
- **permissions:** no permission-code change; the existing
  `records.read/create/update/delete` permissions and the platform-controlled
  `records.deletion` flag still gate the routes.
- **tenant isolation:** opaque tenant/actor identifiers with the org-scoped
  record and revision queries retained.
- **destructive migration/retention:** additive `records.version` and
  `record_revisions`; the non-cascading append-only retention of actor/org
  identity and the reviewed tenant-erasure purge are accepted.
- **backup/recovery:** the PITR/restore contract and the lossy downgrade are
  accepted for rollback/local use only.
- **audit privacy:** `audit_events.metadata` carries only `{"version": N}`, and
  the revision ledger holds only the record's API-bounded `title`/`body`.

## Consequences

- The records update/delete request contract changes: `version` is required. The
  frontend client and types are regenerated, and stale writes surface a
  `record_version_conflict` the UI resolves by reloading.
- `record_revisions` is a new internal table; `records` gains a non-null
  version. No existing row or column is dropped, and existing rows backfill to
  version 1.
- `audit_events` loses its two foreign keys; the filter indexes remain. Direct
  mutation of `audit_events`/`record_revisions` now raises a database error.
- One migration (`f4a1b2c3d4e5`) carries the additive columns/table and the
  trigger; `alembic check` stays clean.

This decision implements closure-plan P8 and follows blueprint §10 (optimistic
concurrency), §11 (transactional service boundaries) and §29 (append-only
audit).

---
