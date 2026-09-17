"""Durable maintenance-run ORM model (plan P4, blueprint §18-§19).

Scheduled maintenance used to be an ordinary Dramatiq actor with broker-only
retries: a published outbox event proved that the sweep was *enqueued*, never
that it *completed* (plan finding on ``app/ai/persistence/tasks.py``). This
table is the PostgreSQL-owned record of every scheduled sweep, so completion,
retry, exhaustion, lease-expired takeover and duplicate delivery are all
visible in the database rather than inferred from logs.

The table is deliberately **global infrastructure**: there is no
``organisation_id`` and no tenant payload of any kind (plan "maintenance runs
are global and contain no tenant payload"). A row carries only a closed task
type, the UTC schedule bucket that created it, ownership/lease fields, a
bounded safe error code and timestamps — never object keys, prompts,
recipients, provider responses, URLs or credentials (BP §28).

The lifecycle mirrors ``jobs`` so operators read one state machine across the
durable platform: ``queued`` -> ``running`` -> ``succeeded``/``failed``.
Exhaustion is not a separate status; it is ``failed`` carrying
``ERROR_CODE_MAINTENANCE_EXHAUSTED``, exactly as a durable job records its own
global-attempt-ceiling settlement.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import UuidV7, uuid7


class MaintenanceTaskType(enum.StrEnum):
    """The closed catalogue of scheduled maintenance workloads.

    The values equal the outbox maintenance event types
    (``app.modules.outbox.contracts``) so one string identifies the schedule
    bucket, the outbox row and the durable run. A new sweep requires an
    explicit entry here, in the outbox contracts and in the dispatch registry.
    """

    AI_RETENTION = "ai.retention"
    TRANSFER_RECONCILE = "ai.transfer_reconcile"


class MaintenanceRunStatus(enum.StrEnum):
    """Lifecycle state of one durable maintenance run.

    The set matches the non-cancellable half of the blueprint §18 job
    statuses. Retry exhaustion is ``FAILED`` with a distinguishing safe error
    code, not a separate state.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _task_type_values(enum_class: type[MaintenanceTaskType]) -> list[str]:
    """Return the values stored by the task-type column, not the enum names."""
    return [member.value for member in enum_class]


def _run_status_values(enum_class: type[MaintenanceRunStatus]) -> list[str]:
    """Return the values stored by the status column, not the enum names."""
    return [member.value for member in enum_class]


class MaintenanceRun(Base):
    """One durable, globally scoped maintenance execution record.

    ``schedule_key`` is the UTC-bucket identity shared with the maintenance
    outbox row's deduplication key, and its unique constraint is the
    concurrency boundary: two coordinator replicas ticking the same bucket
    converge on exactly one run and exactly one dispatch.

    ``owner_token`` is the attempt-distinguishing credential rotated on every
    claim (including an expired-lease takeover), so a superseded worker's
    captured token can never settle a run a newer attempt owns — the same
    fence ``jobs.owner_token`` applies to durable jobs.
    """

    __tablename__ = "maintenance_runs"
    __table_args__ = (
        # One run per UTC schedule bucket: the durable half of the
        # deduplication the outbox row performs for publication.
        UniqueConstraint("schedule_key", name="uq_maintenance_runs_schedule_key"),
        # A claim's captured credential identifies exactly one run.
        UniqueConstraint("owner_token", name="uq_maintenance_runs_owner_token"),
        # Expired-lease recovery scans running rows by lease bound.
        Index("ix_maintenance_runs_status_lease", "status", "lease_expires_at"),
        # Operator history and the P5 stale/failed-run alerts read one task
        # type newest-first.
        Index("ix_maintenance_runs_task_scheduled", "task_type", "scheduled_for"),
        CheckConstraint(
            "task_type IN ('ai.retention', 'ai.transfer_reconcile')",
            name="maintenance_run_task_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="maintenance_run_status",
        ),
        CheckConstraint("attempt_count >= 0", name="non_negative_attempt_count"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    task_type: Mapped[MaintenanceTaskType] = mapped_column(
        Enum(
            MaintenanceTaskType,
            name="maintenance_run_task_type",
            native_enum=False,
            length=32,
            values_callable=_task_type_values,
        ),
        nullable=False,
    )
    # The UTC schedule bucket that produced this run, e.g.
    # ``ai.retention:schedule:20123``. Unique, and identical to the
    # deduplication key of the outbox row created in the same transaction.
    schedule_key: Mapped[str] = mapped_column(String(120), nullable=False)
    # The start of the UTC bucket, kept as a real timestamp so operators can
    # order and window runs without parsing the key.
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[MaintenanceRunStatus] = mapped_column(
        Enum(
            MaintenanceRunStatus,
            name="maintenance_run_status",
            native_enum=False,
            length=16,
            values_callable=_run_status_values,
        ),
        nullable=False,
        default=MaintenanceRunStatus.QUEUED,
        server_default=MaintenanceRunStatus.QUEUED.value,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    owner_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Sticky evidence that this run has ever taken over an expired lease (a
    # crashed or superseded worker), so later success cannot erase recovery
    # history operators need to distinguish from an ordinary retry.
    taken_over: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # A bounded value from the closed safe-error vocabulary in
    # ``app.modules.maintenance.service``; never an exception message.
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
