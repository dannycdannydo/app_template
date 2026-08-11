"""Reusable AI persistence queries (v0.7 Scope §6.5, BP §10, §11).

The statements below are the only places that name the AI tables' filter
columns, so the service and the real-database tests share one source of truth.
Every read is org-scoped: a row from another organisation simply does not
match, making cross-organisation reads indistinguishable from missing rows.
Request rows additionally filter on the caller-visible ``request_id`` and the
per-execution ``attempt_number``, so one execution's attempt rows are a
distinct, org-scoped namespace.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Select, func, select

from app.ai.persistence.models import AIOutputRecord, AIRequestRecord, OrganisationAISettings

#: Statuses that count towards committed monthly spend (v0.7 Scope §6.5
#: documented reservation policy): a ``running`` row is an in-flight
#: reservation, a terminal row is committed spend. Both billed (or reserved)
#: real cost.
_COMMITTED_SPEND_STATUSES = ("running", "succeeded", "failed")


def organisation_ai_settings_statement(
    organisation_id: uuid.UUID,
) -> Select[tuple[OrganisationAISettings]]:
    """Return the one policy row for an organisation."""
    return select(OrganisationAISettings).where(
        OrganisationAISettings.organisation_id == organisation_id
    )


def organisation_ai_settings_for_update_statement(
    organisation_id: uuid.UUID,
) -> Select[tuple[OrganisationAISettings]]:
    """Return the policy row locked for the budget reservation transaction.

    The ``FOR UPDATE`` row lock serializes concurrent reservations for one
    organisation, which is what makes the reservation policy transaction-safe
    (v0.7 Scope §6.5): two in-flight executions can never both pass the budget
    check against the same headroom.
    """
    return organisation_ai_settings_statement(organisation_id).with_for_update()


def ai_request_by_request_id_statement(
    organisation_id: uuid.UUID,
    request_id: str,
    attempt_number: int,
) -> Select[tuple[AIRequestRecord]]:
    """Return one attempt row by org, execution id and attempt number.

    The row is uniquely identified by the triple, which is the idempotency key
    for job redelivery: a re-delivered execution re-uses its existing rows
    instead of double-reserving budget or duplicating records (v0.7 Scope
    §6.5/§6.6).
    """
    return select(AIRequestRecord).where(
        AIRequestRecord.organisation_id == organisation_id,
        AIRequestRecord.request_id == request_id,
        AIRequestRecord.attempt_number == attempt_number,
    )


def ai_request_record_statement(
    ai_request_id: uuid.UUID,
    organisation_id: uuid.UUID,
) -> Select[tuple[AIRequestRecord]]:
    """Return one request row by its primary key, scoped to the organisation.

    The org filter makes a cross-organisation settle/output lookup impossible:
    a caller that supplies another organisation's row id finds nothing, so it
    can never mutate (or audit under) a row it does not own (BP §9 tenant
    boundary).
    """
    return select(AIRequestRecord).where(
        AIRequestRecord.id == ai_request_id,
        AIRequestRecord.organisation_id == organisation_id,
    )


def ai_month_spend_statement(
    organisation_id: uuid.UUID,
    month_start: datetime,
) -> Select[tuple[Decimal]]:
    """Return the committed + reserved spend for one org in the month.

    Both the running reservations and the terminal rows count, so a budget can
    never be overrun by concurrent in-flight executions (v0.7 Scope §6.5). The
    result is the ``SUM(cost)`` (``None`` when no rows match; the caller
    treats that as zero).
    """
    return select(func.sum(AIRequestRecord.cost)).where(
        AIRequestRecord.organisation_id == organisation_id,
        AIRequestRecord.created_at >= month_start,
        AIRequestRecord.status.in_(_COMMITTED_SPEND_STATUSES),
    )


def expired_ai_outputs_statement(
    organisation_id: uuid.UUID,
    older_than: datetime,
) -> Select[tuple[AIOutputRecord]]:
    """Return the org's output records older than a retention cut-off."""
    return select(AIOutputRecord).where(
        AIOutputRecord.organisation_id == organisation_id,
        AIOutputRecord.created_at < older_than,
    )


def organisations_with_retention_policy_statement() -> Select[tuple[OrganisationAISettings]]:
    """Return every policy row that declares a retention policy.

    The retention job sweeps exactly these organisations for expired outputs
    and scratch objects (v0.7 Scope §6.5).
    """
    return select(OrganisationAISettings).where(
        OrganisationAISettings.retention_policy_days.is_not(None)
    )


def stale_running_requests_statement(
    older_than: datetime,
) -> Select[tuple[AIRequestRecord]]:
    """Return request rows stuck in ``running`` longer than a cut-off.

    A row that never settled is a crashed worker execution; the retention job
    marks it ``failed`` keeping its reserved cost, so the budget it reserved
    is never silently released (v0.7 Scope §6.5 documented reservation
    policy). Deliberately not org-scoped by a parameter: every organisation's
    crashed reservations are reconciled, independent of whether that
    organisation configured an output retention policy.
    """
    return select(AIRequestRecord).where(
        AIRequestRecord.status == "running",
        AIRequestRecord.created_at < older_than,
    )
