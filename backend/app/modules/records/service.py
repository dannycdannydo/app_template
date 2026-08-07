"""Record CRUD service (v0.2 Scope §6.5, blueprint §11, §12).

The service owns transaction boundaries: each function is one atomic
operation that commits itself, and the router never commits (BP §11). Every
query is org-scoped through ``queries.org_scoped_records_statement``, so a
record that exists but belongs to another organisation surfaces as a 404, and
domain failures are raised as domain exceptions for the central handlers
(``NotFoundError`` → 404).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDenied
from app.core.feature_flags import FEATURE_RECORDS_DELETION, is_feature_enabled
from app.modules.audit.service import (
    ACTION_RECORD_CREATED,
    ACTION_RECORD_DELETED,
    ACTION_RECORD_UPDATED,
    record_event,
)
from app.modules.records.models import Record
from app.modules.records.queries import (
    org_records_count_statement,
    org_scoped_records_statement,
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def _not_found() -> NotFoundError:
    return NotFoundError(
        code="record_not_found",
        message="The record could not be found.",
    )


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
    organisation id explicitly so the provenance stays visible. The audit row
    commits inside the same transaction.
    """
    record = Record(organisation_id=organisation_id, title=title, body=body)
    session.add(record)
    await session.flush()
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_RECORD_CREATED,
        resource_type="record",
        resource_id=str(record.id),
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
    title: str | None,
    body: str | None,
    actor_user_id: uuid.UUID | None = None,
) -> Record:
    """Apply a partial update; unchanged fields keep their values."""
    record = await get_record(session, organisation_id=organisation_id, record_id=record_id)
    if title is not None:
        record.title = title
    if body is not None:
        record.body = body
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_RECORD_UPDATED,
        resource_type="record",
        resource_id=str(record.id),
    )
    await session.commit()
    await session.refresh(record)
    return record


async def delete_record(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    record_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Delete a record; a record outside the organisation is a 404.

    Deletion is gated by the platform-controlled ``records.deletion`` feature
    flag (Scope §6.7, blueprint §27): it is off by default, so an organisation
    keeps the destructive operation unavailable until a platform administrator
    enables it. The flag is enforced here in the service, never in a router,
    and the permission check runs first — a caller without ``records.delete``
    is still denied by the route's permission dependency, and a missing or
    cross-organisation record is still a 404 before the flag is consulted.
    """
    record = await get_record(session, organisation_id=organisation_id, record_id=record_id)
    if not await is_feature_enabled(
        session,
        organisation_id=organisation_id,
        feature_key=FEATURE_RECORDS_DELETION,
    ):
        raise PermissionDenied(
            code="feature_disabled",
            message="Record deletion is not enabled for this organisation.",
        )
    await session.delete(record)
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=ACTION_RECORD_DELETED,
        resource_type="record",
        resource_id=str(record.id),
    )
    await session.commit()
