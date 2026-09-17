"""Maintenance-run query helpers (plan P4, blueprint §19, §28).

Maintenance runs are global infrastructure rows, so these statements carry no
organisation filter. Each one is bounded: the coordinator recovery sweep takes
a limited batch under ``FOR UPDATE SKIP LOCKED``, and the observability count
returns a number rather than ids, so no schedule identity or task detail
leaves the database for a metric label (BP §28).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, func, select

from app.modules.maintenance.models import (
    MaintenanceRun,
    MaintenanceRunStatus,
    MaintenanceTaskType,
)


def maintenance_run_by_id_statement(run_id: uuid.UUID) -> Select[tuple[MaintenanceRun]]:
    """Select one run by primary key (claim and owner-checked settlement)."""
    return select(MaintenanceRun).where(MaintenanceRun.id == run_id)


def maintenance_run_by_schedule_key_statement(schedule_key: str) -> Select[tuple[MaintenanceRun]]:
    """Select the single run owning one UTC schedule bucket."""
    return select(MaintenanceRun).where(MaintenanceRun.schedule_key == schedule_key)


def expired_running_maintenance_runs_statement(
    *, lease_expired_before: datetime, limit: int
) -> Select[tuple[MaintenanceRun]]:
    """Select running runs whose execution lease expired, oldest bucket first.

    A worker that died after claiming leaves its run ``running`` forever when
    Redis holds no copy of the message, so this is the PostgreSQL-owned
    recovery candidate set (plan P4, AC7). Ordering by ``scheduled_for``
    recovers the oldest stranded bucket first; the caller bounds the batch and
    locks each row with ``SKIP LOCKED``.
    """
    return (
        select(MaintenanceRun)
        .where(
            MaintenanceRun.status == MaintenanceRunStatus.RUNNING,
            MaintenanceRun.lease_expires_at.is_not(None),
            MaintenanceRun.lease_expires_at < lease_expired_before,
        )
        .order_by(MaintenanceRun.scheduled_for.asc())
        .limit(limit)
    )


def maintenance_run_history_statement(
    task_type: str, *, limit: int
) -> Select[tuple[MaintenanceRun]]:
    """Select one task type's recent runs, newest bucket first (operators)."""
    return (
        select(MaintenanceRun)
        .where(MaintenanceRun.task_type == task_type)
        .order_by(MaintenanceRun.scheduled_for.desc())
        .limit(limit)
    )


def maintenance_metric_rows_statement() -> Select[
    tuple[MaintenanceTaskType, MaintenanceRunStatus, int]
]:
    """Return bounded run counts grouped by closed task type and status.

    Both dimensions are closed enums, so the cardinality of any gauge built
    from this statement is fixed by the schema (BP §28 low-cardinality rule).
    """
    return select(
        MaintenanceRun.task_type,
        MaintenanceRun.status,
        func.count().label("total"),
    ).group_by(MaintenanceRun.task_type, MaintenanceRun.status)
