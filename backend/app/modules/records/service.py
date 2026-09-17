"""Record CRUD service (v0.2 Scope §6.5, plan P8, blueprint §10, §11, §12).

The service owns transaction boundaries: each function is one atomic
operation that commits itself, and the router never commits (BP §11). Every
query is org-scoped through ``queries.org_scoped_records_statement``, so a
record that exists but belongs to another organisation surfaces as a 404, and
domain failures are raised as domain exceptions for the central handlers
(``NotFoundError`` → 404).

Plan P8 turns the update/delete paths into a conditional contract (BP §10):
the row is locked ``FOR UPDATE``, the caller's ``expected_version`` is compared
against the stored ``version``, and a stale write is rejected with
``ConflictError`` (409 ``record_version_conflict``) instead of overwriting a
later writer. Every accepted create/update/delete writes one immutable
:class:`RecordRevision` snapshot in the same transaction, so the audit event
carries only the safe version number and never record content.

Deletion is a **hard** delete with **no restore path**: the immutable
``record_revisions`` ledger lets a human/operator reconstruct the prior state,
but neither the API nor the service resurrects a deleted row.
:func:`restore_record` exists purely to state that contract and give a future
caller a stable, tested rejection instead of a silent re-create (plan P8 item 3,
ADR-0020).
"""

from __future__ import annotations

import uuid
from typing import NoReturn

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.core.feature_flags import FEATURE_RECORDS_DELETION, is_feature_enabled
from app.modules.audit.service import (
    ACTION_RECORD_CREATED,
    ACTION_RECORD_DELETED,
    ACTION_RECORD_UPDATED,
    record_event,
)
from app.modules.records.models import Record, RecordRevision, RecordRevisionAction
from app.modules.records.queries import (
    org_records_count_statement,
    org_scoped_record_for_update_statement,
    org_scoped_records_statement,
    record_revisions_statement,
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def _not_found() -> NotFoundError:
    return NotFoundError(
        code="record_not_found",
        message="The record could not be found.",
    )


def _version_conflict() -> ConflictError:
    return ConflictError(
        code="record_version_conflict",
        message="The record was changed by someone else. Reload it and try again.",
    )


async def _append_revision(
    session: AsyncSession,
    *,
    record: Record,
    action: RecordRevisionAction,
    actor_user_id: uuid.UUID | None,
) -> RecordRevision:
    """Append one immutable snapshot of the record's bounded fields.

    Called in the same transaction as the record change and its audit event;
    the snapshot records the post-change ``title``/``body`` and the record
    ``version`` the change produced. ``record_id``/``actor_user_id`` are stored
    as opaque UUIDs (no foreign key) so hard-deleting the record or the user
    cannot erase the provenance (plan P8 retention decision, ADR-0020).
    """
    revision = RecordRevision(
        record_id=record.id,
        organisation_id=record.organisation_id,
        version=record.version,
        action=action,
        title=record.title,
        body=record.body,
        actor_user_id=actor_user_id,
    )
    session.add(revision)
    await session.flush()
    return revision


async def create_record(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    title: str,
    body: str,
    actor_user_id: uuid.UUID | None = None,
) -> Record:
    """Create a record inside the caller's organisation (one transaction).

    The organisation id comes from the validated request context, never from
    the request body (acceptance §5.4); the caller passes the membership's
    organisation id explicitly so the provenance stays visible. The record's
    first version is 1 and the matching create revision and audit row commit
    inside the same transaction.
    """
    record = Record(organisation_id=organisation_id, title=title, body=body, version=1)
    session.add(record)
    await session.flush()
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_RECORD_CREATED,
        resource_type="record",
        resource_id=str(record.id),
        metadata={"version": record.version},
    )
    await _append_revision(
        session,
        record=record,
        action=RecordRevisionAction.CREATED,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    await session.refresh(record)
    return record


async def list_records(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    page: int,
    page_size: int,
) -> tuple[list[Record], int]:
    """Return one page of the caller's organisation's records plus the total.

    Newest first, ties broken by id so paging is stable. ``page`` and
    ``page_size`` are validated by the router's query parameters before the
    service is reached; the service still clamps defensively.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total = await session.scalar(org_records_count_statement(organisation_id))
    rows = await session.scalars(
        org_scoped_records_statement(organisation_id)
        .order_by(Record.created_at.desc(), Record.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.all()), total or 0


async def get_record(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    record_id: uuid.UUID,
) -> Record:
    """Return one record; a record outside the organisation is a 404.

    The org-scoped filter is the isolation boundary: a record id that exists
    in another organisation simply does not match, so cross-organisation reads
    are indistinguishable from missing rows (acceptance §5.7).
    """
    record = await session.scalar(
        org_scoped_records_statement(organisation_id).where(Record.id == record_id)
    )
    if record is None:
        raise _not_found()
    return record


async def update_record(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    record_id: uuid.UUID,
    expected_version: int,
    title: str | None,
    body: str | None,
    actor_user_id: uuid.UUID | None = None,
) -> Record:
    """Apply a partial update with optimistic concurrency (BP §10, §11).

    The org-scoped row is locked, the caller's ``expected_version`` is compared
    against the stored version, and a mismatch raises a 409 conflict so the
    caller reloads rather than clobbering a later writer. On success the version
    increments, one update revision and one ``record.updated`` audit event
    (carrying only the new version number) commit atomically.
    """
    record = await session.scalar(
        org_scoped_record_for_update_statement(organisation_id, record_id)
    )
    if record is None:
        raise _not_found()
    if record.version != expected_version:
        raise _version_conflict()
    if title is not None:
        record.title = title
    if body is not None:
        record.body = body
    record.version += 1
    await session.flush()
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_RECORD_UPDATED,
        resource_type="record",
        resource_id=str(record.id),
        metadata={"version": record.version},
    )
    await _append_revision(
        session,
        record=record,
        action=RecordRevisionAction.UPDATED,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    await session.refresh(record)
    return record


async def delete_record(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    record_id: uuid.UUID,
    expected_version: int,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Delete a record with the same conditional contract; 404 if not found.

    Deletion is gated by the platform-controlled ``records.deletion`` feature
    flag (Scope §6.7, blueprint §27): it is off by default, so an organisation
    keeps the destructive operation unavailable until a platform administrator
    enables it. The flag is enforced here in the service, never in a router,
    and the permission check runs first — a caller without ``records.delete``
    is still denied by the route's permission dependency, and a missing or
    cross-organisation record is still a 404 before the flag is consulted.

    A stale ``expected_version`` is a 409 before any mutation. On success the
    final snapshot is written as a ``deleted`` revision (its ``record_id`` is an
    opaque UUID, so the history outlives the hard delete) and the
    ``record.deleted`` audit event is committed in the same transaction.
    """
    record = await session.scalar(
        org_scoped_record_for_update_statement(organisation_id, record_id)
    )
    if record is None:
        raise _not_found()
    if record.version != expected_version:
        raise _version_conflict()
    if not await is_feature_enabled(
        session,
        organisation_id=organisation_id,
        feature_key=FEATURE_RECORDS_DELETION,
    ):
        raise PermissionDenied(
            code="feature_disabled",
            message="Record deletion is not enabled for this organisation.",
        )
    await _append_revision(
        session,
        record=record,
        action=RecordRevisionAction.DELETED,
        actor_user_id=actor_user_id,
    )
    await session.delete(record)
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_RECORD_DELETED,
        resource_type="record",
        resource_id=str(record.id),
        metadata={"version": record.version},
    )
    await session.commit()


async def restore_record(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    record_id: uuid.UUID,
) -> NoReturn:
    """Reject record-level restoration: hard delete is permanent (plan P8).

    P8 deliberately chose hard deletion with an immutable revision ledger. The
    ledger is reconstruction *evidence* for a human/operator, not a
    programmatic resurrection: the closed revision actions are
    ``created``/``updated``/``deleted`` and there is no public restore endpoint.
    This explicit, tested rejection defines that contract, so any future caller
    that tries to restore a deleted record gets a stable error rather than a
    silent re-create that would reuse an identity or skip the audit trail.

    The ``organisation_id``/``record_id`` lookup uses the same org-scoped read
    as every other service call, so a cross-organisation id is handled on the
    same tenant boundary even on this rejection path.
    """
    await session.scalar(
        org_scoped_records_statement(organisation_id).where(Record.id == record_id)
    )
    raise BadRequestError(
        code="record_restore_unsupported",
        message=(
            "Deleted records cannot be restored. Their immutable revisions remain "
            "available for reconstruction."
        ),
    )


async def list_record_revisions(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    record_id: uuid.UUID,
) -> list[RecordRevision]:
    """Return one record's immutable history, oldest-first.

    Internal provenance only: there is deliberately no public endpoint, so the
    revision history is protected from normal API callers (plan P8 item 3).
    The filter includes the organisation so even the internal read stays
    tenant-scoped (a cross-organisation record id returns no history).
    """
    rows = await session.scalars(
        record_revisions_statement(organisation_id=organisation_id, record_id=record_id)
    )
    return list(rows.all())
