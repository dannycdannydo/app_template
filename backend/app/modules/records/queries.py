"""Reusable org-scoped record queries (Scope §6.5, blueprint §12).

Every records query filters on ``organisation_id`` first; the two statements
here are the single source of that scoping so a future endpoint cannot forget
it and leak a record across organisations. The service layer consumes them
with its own ordering, paging and transaction boundaries.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select

from app.modules.records.models import Record


def org_scoped_records_statement(organisation_id: uuid.UUID) -> Select[tuple[Record]]:
    """Return the org-scoped select shared by every records query.

    A record outside the caller's organisation is not matched, which is what
    makes cross-organisation access a 404 rather than a leak (acceptance §5.7).
    """
    return select(Record).where(Record.organisation_id == organisation_id)


def org_records_count_statement(organisation_id: uuid.UUID) -> Select[tuple[int]]:
    """Return the org-scoped count for pagination envelopes."""
    return select(func.count()).select_from(Record).where(Record.organisation_id == organisation_id)
