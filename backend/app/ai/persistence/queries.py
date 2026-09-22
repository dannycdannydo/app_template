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

from sqlalchemy import Select, case, func, select

from app.ai.persistence.models import (
    AIAttachmentReference,
    AIOutputRecord,
    AIRequestRecord,
    AIScratchUpload,
    OrganisationAISettings,
)
from app.modules.organisations.models import Organisation

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


def ai_winning_attempt_statement(
    organisation_id: uuid.UUID,
    request_id: str,
) -> Select[tuple[AIRequestRecord]]:
    """Return the succeeded (winning) attempt for one execution, if any.

    A multi-attempt execution settles every non-winning attempt ``failed`` and
    exactly one ``succeeded`` (v0.7 Scope §6.4/§6.5). Querying the winning
    attempt — rather than hard-coding ``attempt_number == 1`` — means a
    transient first failure followed by a later success is reported correctly
    after the job completes (v0.7 Scope §6.6).
    """
    return select(AIRequestRecord).where(
        AIRequestRecord.organisation_id == organisation_id,
        AIRequestRecord.request_id == request_id,
        AIRequestRecord.status == "succeeded",
    )


def ai_latest_attempt_statement(
    organisation_id: uuid.UUID,
    request_id: str,
) -> Select[tuple[AIRequestRecord]]:
    """Return the highest-numbered attempt row for one execution.

    Used as a fallback when no attempt has succeeded: the latest row's status
    is the execution-level outcome (``running``, ``queued`` or ``failed``).
    Ordered by ``attempt_number`` descending so the most recent dispatch wins.
    """
    return (
        select(AIRequestRecord)
        .where(
            AIRequestRecord.organisation_id == organisation_id,
            AIRequestRecord.request_id == request_id,
        )
        .order_by(AIRequestRecord.attempt_number.desc())
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


def ai_output_for_request_statement(
    organisation_id: uuid.UUID,
    ai_request_id: uuid.UUID,
) -> Select[tuple[AIOutputRecord]]:
    """Return one validated output by its request id and organisation."""
    return select(AIOutputRecord).where(
        AIOutputRecord.organisation_id == organisation_id,
        AIOutputRecord.ai_request_id == ai_request_id,
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


def scratch_upload_by_upload_id_statement(
    organisation_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> Select[tuple[AIScratchUpload]]:
    """Return one scratch-upload intent by its org-scoped caller-visible id."""
    return select(AIScratchUpload).where(
        AIScratchUpload.organisation_id == organisation_id,
        AIScratchUpload.upload_id == upload_id,
    )


def scratch_upload_by_object_key_statement(
    organisation_id: uuid.UUID,
    object_key: str,
) -> Select[tuple[AIScratchUpload]]:
    """Return the org-scoped scratch intent that owns one scratch object key.

    Plan P6: a scratch storage reference is only authoritative when a live
    durable intent backs it; this lookup is the scratch half of the source
    authority.
    """
    return select(AIScratchUpload).where(
        AIScratchUpload.organisation_id == organisation_id,
        AIScratchUpload.object_key == object_key,
    )


def all_organisation_ids_statement() -> Select[tuple[uuid.UUID]]:
    """Return every organisation id in stable order.

    Plan P3 group 3: the AI-data policies are default-deny and the global
    retention/scratch/reconciliation sweeps run on the restricted runtime role,
    so a cross-tenant scan can no longer read the AI tables directly. The
    ``organisations`` table is a global, unprotected table, so the sweep can
    still enumerate the tenants and bind each organisation's transaction-local
    context before touching its AI rows (ADR-0022 decision 4: no universal
    bypass). Ordering by id keeps a run deterministic.
    """
    return select(Organisation.id).order_by(Organisation.id)


def expired_scratch_uploads_statement(
    organisation_id: uuid.UUID,
    *,
    expired_before: datetime,
    batch_size: int,
) -> Select[tuple[AIScratchUpload]]:
    """Return the bounded next batch of one organisation's expired intents.

    Plan P6: the sweep runs for every organisation, independent of any
    per-organisation retention policy, so a global maximum lifetime applies to
    every scratch object. Only non-terminal rows are candidates. The
    organisation filter keeps the default-deny RLS policy and the explicit
    application predicate in agreement (plan P3, ADR-0022 decision 9).
    """
    return (
        select(AIScratchUpload)
        .where(
            AIScratchUpload.organisation_id == organisation_id,
            AIScratchUpload.expires_at <= expired_before,
            AIScratchUpload.status.in_(("pending", "ready")),
        )
        .order_by(AIScratchUpload.expires_at.asc())
        .limit(batch_size)
    )


def stale_running_requests_statement(
    organisation_id: uuid.UUID,
    older_than: datetime,
) -> Select[tuple[AIRequestRecord]]:
    """Return one organisation's request rows stuck in ``running``.

    A row that never settled is a crashed worker execution; the retention job
    marks it ``failed`` keeping its reserved cost, so the budget it reserved
    is never silently released (v0.7 Scope §6.5 documented reservation
    policy). The sweep covers every organisation's crashed reservations,
    independent of whether that organisation configured an output retention
    policy; it iterates organisations and binds each one rather than scanning
    across tenants, because the ``ai_requests`` RLS policy is default-deny
    (plan P3 group 3, ADR-0022 decision 4). The explicit organisation filter
    keeps the application predicate and the policy in agreement.
    """
    return select(AIRequestRecord).where(
        AIRequestRecord.organisation_id == organisation_id,
        AIRequestRecord.status == "running",
        AIRequestRecord.created_at < older_than,
    )


def expired_execution_metadata_statement(
    organisation_id: uuid.UUID,
    expired_before: datetime,
) -> Select[tuple[AIRequestRecord]]:
    """Return queued/executing task variables past their global hard expiry."""
    return select(AIRequestRecord).where(
        AIRequestRecord.organisation_id == organisation_id,
        AIRequestRecord.execution_metadata.is_not(None),
        AIRequestRecord.execution_metadata_expires_at <= expired_before,
    )


# --- v0.8 Scope §6.3: durable transfer references ----------------------------


def ai_attachment_reference_by_key_statement(
    organisation_id: uuid.UUID,
    idempotency_key: str,
) -> Select[tuple[AIAttachmentReference]]:
    """Return one reference row by its derived idempotency key, org-scoped.

    The org filter makes a cross-organisation reference lookup
    indistinguishable from a missing row (BP §9 tenant boundary): a caller
    that supplies another organisation's key finds nothing and can never adopt,
    expire or delete a row it does not own. The row is uniquely identified
    inside the organisation by the key when it is live (partial unique index,
    Scope §2.3).
    """
    return select(AIAttachmentReference).where(
        AIAttachmentReference.organisation_id == organisation_id,
        AIAttachmentReference.idempotency_key == idempotency_key,
    )


def ai_live_attachment_reference_by_key_statement(
    organisation_id: uuid.UUID,
    idempotency_key: str,
) -> Select[tuple[AIAttachmentReference]]:
    """Return the one live reference row for an idempotency key, if any.

    Reuse is allowed only while the record is ``live`` (Scope §2.1 retry-only
    reuse); expired and deleted rows are terminal and must be replaced by a
    new idempotent transfer. The caller additionally checks the provider
    expiry timestamp, marking a time-expired row ``expired`` rather than
    reusing it.
    """
    return ai_attachment_reference_by_key_statement(organisation_id, idempotency_key).where(
        AIAttachmentReference.status == "live"
    )


def ai_attachment_reference_for_deletion_statement(
    organisation_id: uuid.UUID,
    idempotency_key: str,
) -> Select[tuple[AIAttachmentReference]]:
    """Resolve the authoritative row for terminal deletion of one key.

    Deletion must act on the row that owns the current provider copy, not on a
    caller-supplied possibly stale reference: the live row is preferred (it
    names the copy the last create/adopt left in place), falling back to the
    newest non-deleted (expired) row so a terminal sweep after
    ``expire_all_for_request`` still removes the copies of expired references.
    The partial unique index guarantees at most one live row per key and this
    ordering makes the result unique; ``None`` means every row for the key is
    already terminal. Strictly org-scoped (BP §9).
    """
    return (
        select(AIAttachmentReference)
        .where(
            AIAttachmentReference.organisation_id == organisation_id,
            AIAttachmentReference.idempotency_key == idempotency_key,
            AIAttachmentReference.status != "deleted",
        )
        .order_by(
            case((AIAttachmentReference.status == "live", 0), else_=1),
            AIAttachmentReference.created_at.desc(),
        )
        .limit(1)
    )


def ai_attachment_references_for_request_statement(
    organisation_id: uuid.UUID,
    logical_request_id: str,
) -> Select[tuple[AIAttachmentReference]]:
    """Return every reference row belonging to one logical request.

    Used by the terminal-lifecycle operations (expire/delete all references of
    one logical request) and by the reconciliation surfaces; strictly
    org-scoped so one request id can never reach another organisation's rows.
    """
    return select(AIAttachmentReference).where(
        AIAttachmentReference.organisation_id == organisation_id,
        AIAttachmentReference.logical_request_id == logical_request_id,
    )


def ai_attachment_references_needing_reconciliation_statement(
    organisation_id: uuid.UUID,
    *,
    retry_after: datetime,
    batch_size: int,
) -> Select[tuple[AIAttachmentReference]]:
    """Return the bounded next batch of one organisation's references.

    v0.8 Scope §2.5/§6.7: the reconciliation sweep covers exactly the
    provider-hosted copies (``provider_upload`` mode) that terminal cleanup
    did not remove: rows still owning a copy (``status <> 'deleted'``) whose
    owning logical AI request is terminal — no further dispatch can reuse the
    reference — or whose request row is missing entirely (an orphan). A row
    whose last deletion attempt failed is re-claimed only after the bounded
    backoff window (``deletion_attempted_at <= retry_after``), so a failing
    provider is not hammered. Managed signed URLs (no provider copy), Vertex
    GCS staging objects (deployer-owned lifecycle) and feature-owned source
    objects never match this statement (BP §28, Scope §2.5). The sweep
    iterates organisations because ``ai_attachment_references`` is
    default-deny under RLS (plan P3 group 3, ADR-0022 decision 4); the
    organisation filter keeps the application predicate and the policy
    aligned.
    """
    latest_attempt = (
        select(
            AIRequestRecord.organisation_id.label("organisation_id"),
            AIRequestRecord.request_id.label("request_id"),
            func.max(AIRequestRecord.attempt_number).label("max_attempt"),
        )
        .group_by(AIRequestRecord.organisation_id, AIRequestRecord.request_id)
        .subquery()
    )
    latest_status = (
        select(
            latest_attempt.c.organisation_id,
            latest_attempt.c.request_id,
            AIRequestRecord.status.label("status"),
        )
        .join(
            AIRequestRecord,
            (AIRequestRecord.organisation_id == latest_attempt.c.organisation_id)
            & (AIRequestRecord.request_id == latest_attempt.c.request_id)
            & (AIRequestRecord.attempt_number == latest_attempt.c.max_attempt),
        )
        .subquery()
    )
    return (
        select(AIAttachmentReference)
        .outerjoin(
            latest_status,
            (latest_status.c.organisation_id == AIAttachmentReference.organisation_id)
            & (latest_status.c.request_id == AIAttachmentReference.logical_request_id),
        )
        .where(
            AIAttachmentReference.organisation_id == organisation_id,
            AIAttachmentReference.transfer_mode == "provider_upload",
            AIAttachmentReference.status != "deleted",
            # A terminal owning request (succeeded/failed) or an orphan makes
            # the copy AI-orphaned and cleanable; a still-running/queued
            # request is never touched. The orphan side is explicit: the outer
            # join extends every unmatched reference row with NULL columns, and
            # ``status IN (...`` alone evaluates to UNKNOWN (not TRUE) for NULL,
            # so an owning-request row that is missing entirely must be stated
            # as its own predicate or it would never be selected.
            (
                latest_status.c.status.in_(("succeeded", "failed"))
                | latest_status.c.request_id.is_(None)
            ),
            # Never claimed, or failed before and past the bounded backoff.
            AIAttachmentReference.deletion_attempted_at.is_(None)
            | (AIAttachmentReference.deletion_attempted_at <= retry_after),
        )
        .order_by(AIAttachmentReference.deletion_attempted_at.asc().nulls_first())
        .limit(batch_size)
    )


def ai_attachment_reference_reconciliation_backlog_statement(
    organisation_id: uuid.UUID,
    *,
    retry_after: datetime,
) -> Select[tuple[int]]:
    """Count one organisation's currently eligible provider-file references.

    The same predicate as :func:`ai_attachment_references_needing_reconciliation_statement`
    without the batch limit; the per-organisation counts are summed into the
    low-cardinality ``ai_transfer_cleanup_backlog`` gauge the §6.7 runbook
    alerts on. Only provider-hosted copies are counted — managed URLs and GCS
    staging objects never are (Scope §2.5). The organisation filter is the
    explicit predicate that agrees with the default-deny policy (plan P3 group
    3).
    """
    latest_attempt = (
        select(
            AIRequestRecord.organisation_id.label("organisation_id"),
            AIRequestRecord.request_id.label("request_id"),
            func.max(AIRequestRecord.attempt_number).label("max_attempt"),
        )
        .group_by(AIRequestRecord.organisation_id, AIRequestRecord.request_id)
        .subquery()
    )
    latest_status = (
        select(
            latest_attempt.c.organisation_id,
            latest_attempt.c.request_id,
            AIRequestRecord.status.label("status"),
        )
        .join(
            AIRequestRecord,
            (AIRequestRecord.organisation_id == latest_attempt.c.organisation_id)
            & (AIRequestRecord.request_id == latest_attempt.c.request_id)
            & (AIRequestRecord.attempt_number == latest_attempt.c.max_attempt),
        )
        .subquery()
    )
    return (
        select(func.count(AIAttachmentReference.id))
        .select_from(AIAttachmentReference)
        .outerjoin(
            latest_status,
            (latest_status.c.organisation_id == AIAttachmentReference.organisation_id)
            & (latest_status.c.request_id == AIAttachmentReference.logical_request_id),
        )
        .where(
            AIAttachmentReference.organisation_id == organisation_id,
            AIAttachmentReference.transfer_mode == "provider_upload",
            AIAttachmentReference.status != "deleted",
            # Same terminal-or-orphan predicate as the candidate statement:
            # the outer join's NULL side must be stated explicitly (see
            # :func:`ai_attachment_references_needing_reconciliation_statement`).
            (
                latest_status.c.status.in_(("succeeded", "failed"))
                | latest_status.c.request_id.is_(None)
            ),
            AIAttachmentReference.deletion_attempted_at.is_(None)
            | (AIAttachmentReference.deletion_attempted_at <= retry_after),
        )
    )
