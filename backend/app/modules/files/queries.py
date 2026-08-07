"""Reusable org-scoped file queries (Scope §6.3, blueprint §12).

Every files query filters on ``organisation_id`` first, and by default also on
``deleted_at IS NULL``, so a file outside the caller's organisation — or a
soft-deleted one — is simply not matched: cross-organisation access and
deleted-file access are both indistinguishable from a missing row (404, never
a leak). The service layer consumes these statements with its own ordering,
paging and transaction boundaries.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select

from app.modules.files.models import File, FileStatus


def org_scoped_files_statement(
    organisation_id: uuid.UUID,
    *,
    status: FileStatus | None = None,
    include_deleted: bool = False,
) -> Select[tuple[File]]:
    """Return the org-scoped select shared by every files query.

    ``status`` is the only approved filter field (BP §12); the router validates
    the value against the enum before the service is reached. Deleted rows are
    excluded unless ``include_deleted`` is set.
    """
    statement = select(File).where(File.organisation_id == organisation_id)
    if status is not None:
        statement = statement.where(File.status == status)
    if not include_deleted:
        statement = statement.where(File.deleted_at.is_(None))
    return statement


def org_files_count_statement(
    organisation_id: uuid.UUID,
    *,
    status: FileStatus | None = None,
    include_deleted: bool = False,
) -> Select[tuple[int]]:
    """Return the org-scoped count for pagination envelopes."""
    return select(func.count()).select_from(
        org_scoped_files_statement(
            organisation_id,
            status=status,
            include_deleted=include_deleted,
        ).subquery()
    )
