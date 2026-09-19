"""Low-cardinality reliability metric queries (blueprint §28)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select

from app.modules.jobs.models import Job, JobAttempt, JobAttemptStatus, JobStatus
from app.modules.maintenance.models import MaintenanceRun, MaintenanceRunStatus
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus


def job_attempt_metric_rows_statement() -> Select[tuple[JobAttemptStatus, int]]:
    return select(JobAttempt.status, func.count()).group_by(JobAttempt.status)


def attention_required_delivery_count_statement() -> Select[tuple[int]]:
    """Count attention-required deliveries through the operational read.

    ``notification_deliveries`` is user-private and RLS-enforced (plan P3
    group 2), so the in-process metrics loop cannot read the table directly
    without tenant context. It calls the ``app_attention_required_delivery_count()``
    aggregate instead: the function executes as the narrow non-bypass
    ``app_metrics`` role and returns only a scalar count, so the metric stays
    truthful without exposing delivery rows (ADR-0022 decision 3).
    """
    return select(func.app_attention_required_delivery_count())


def maintenance_run_metric_rows_statement(
    *, lease_expired_before: datetime
) -> Select[tuple[MaintenanceRunStatus, bool, int]]:
    """Group runs by status and whether a running lease is stale."""
    stale = (
        (MaintenanceRun.status == MaintenanceRunStatus.RUNNING)
        & (MaintenanceRun.lease_expires_at.is_not(None))
        & (MaintenanceRun.lease_expires_at <= lease_expired_before)
    ).label("stale")
    return select(MaintenanceRun.status, stale, func.count()).group_by(MaintenanceRun.status, stale)


def dead_current_dispatch_count_statement() -> Select[tuple[int]]:
    """Count queued jobs still pointing at a permanently dead dispatch."""
    return (
        select(func.count())
        .select_from(Job)
        .join(OutboxEvent, OutboxEvent.id == Job.dispatch_id)
        .where(
            Job.status == JobStatus.QUEUED,
            OutboxEvent.status == OutboxEventStatus.DEAD,
            OutboxEvent.aggregate_type == "job",
            OutboxEvent.aggregate_id == Job.id,
        )
    )
