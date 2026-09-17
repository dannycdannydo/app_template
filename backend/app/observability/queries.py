"""Low-cardinality reliability metric queries (blueprint §28)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select

from app.modules.jobs.models import Job, JobAttempt, JobAttemptStatus, JobStatus
from app.modules.maintenance.models import MaintenanceRun, MaintenanceRunStatus
from app.modules.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryStatus,
)
from app.modules.outbox.models import OutboxEvent, OutboxEventStatus


def job_attempt_metric_rows_statement() -> Select[tuple[JobAttemptStatus, int]]:
    return select(JobAttempt.status, func.count()).group_by(JobAttempt.status)


def attention_required_delivery_count_statement() -> Select[tuple[int]]:
    return (
        select(func.count())
        .select_from(NotificationDelivery)
        .where(NotificationDelivery.status == NotificationDeliveryStatus.ATTENTION_REQUIRED)
    )


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
