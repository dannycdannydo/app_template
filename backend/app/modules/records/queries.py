"""Reusable org-scoped record queries (v0.2 Scope §6.5, plan P8, blueprint §12).

Every records query filters on ``organisation_id`` first; the statements here
are the single source of that scoping so a future endpoint cannot forget it and
leak a record across organisations. The service layer consumes them with its own
ordering, paging and transaction boundaries.

The locking statement is the P8 optimistic-concurrency primitive: the mutating
service locks the org-scoped row ``FOR UPDATE``, compares the caller's version
and only then writes, so two concurrent updates serialise and the stale one
returns 409 (blueprint §10, §11).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select

from app.modules.records.models import Record, RecordRevision


def org_scoped_records_statement(organisation_id: uuid.UUID) -> Select[tuple[Record]]:
    """Return the org-scoped select shared by every records query.

    A record outside the caller's organisation is not matched, which is what
    makes cross-organisation access a 404 rather than a leak (acceptance §5.7).
    """
    return select(Record).where(Record.organisation_id == organisation_id)


def org_records_count_statement(organisation_id: uuid.UUID) -> Select[tuple[int]]:
    """Return the org-scoped count for pagination envelopes."""
    return select(func.count()).select_from(Record).where(Record.organisation_id == organisation_id)


def org_scoped_record_for_update_statement(
    organisation_id: uuid.UUID, record_id: uuid.UUID
) -> Select[tuple[Record]]:
    """Return the org-scoped row lock used by the conditional update/delete.

    ``FOR UPDATE`` serialises concurrent writers on the same row, so the
    version comparison and the version increment happen against a row a
    competing writer cannot change in between (BP §10 optimistic concurrency,
    BP §11 transactional service boundary).
    """
    return (
        select(Record)
        .where(
            Record.organisation_id == organisation_id,
            Record.id == record_id,
        )
        .with_for_update()
    )


def record_revisions_statement(
    *,
    organisation_id: uuid.UUID | None = None,
    record_id: uuid.UUID | None = None,
) -> Select[tuple[RecordRevision]]:
    """Return the append-only revision history, filtered and oldest-first.

    Both filters are optional and combined with AND. Callers order the result
    by ``(created_at, id)`` to reconstruct a record's approved changes in
    sequence; the snapshot rows are never mutated or deleted.
    """
    statement = select(RecordRevision)
    if organisation_id is not None:
        statement = statement.where(RecordRevision.organisation_id == organisation_id)
    if record_id is not None:
        statement = statement.where(RecordRevision.record_id == record_id)
    return statement.order_by(RecordRevision.created_at, RecordRevision.id)
