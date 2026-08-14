"""Durable job record ORM model (Scope §6.4, blueprint §18).

The ``jobs`` table is the application's durable record of every background
job: it exists so a client can poll status and progress (``GET /api/v1/jobs``
in Scope §6.5) and so the worker can be audited against the request that
started it. It deliberately follows the blueprint §18 shape exactly: no
``updated_at`` column (like ``audit_events``, the lifecycle timing lives in
the explicit ``started_at`` / ``completed_at`` columns), and every row hangs
off exactly one organisation so the job endpoints can apply the same
tenant-boundary discipline as ``files`` and ``records``.

The statuses are the blueprint §18 set (``queued`` / ``running`` /
``succeeded`` / ``failed`` / ``cancelled``). ``queued`` means the durable row
exists and the task has been enqueued; the worker moves the row through
``running`` to a terminal state. Terminal states are never re-run: the service
helpers refuse to transition a row that already reached ``succeeded``,
``failed`` or ``cancelled`` (acceptance §5.7).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import UuidV7, uuid7


class JobStatus(enum.StrEnum):
    """Lifecycle state of a durable job (blueprint §18 statuses)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _job_status_values(enum_class: type[JobStatus]) -> list[str]:
    """Return the values a status column stores, not the enum names."""
    return [member.value for member in enum_class]


class Job(Base):
    """One durable job record inside an organisation.

    The blueprint §18 shape: a reference to what the job operates on
    (``input_reference``), an optional reference to what it produced
    (``result_reference``), the retry-visible ``attempt_count``, and the
    failure surface (``error_code`` / ``error_message``) the polling endpoints
    and the audit trail read. ``progress`` is an integer 0-100 enforced by a
    check constraint. The org-scoped list is served by the composite index
    ``(organisation_id, created_at)``, newest first, exactly like files and
    records.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_organisation_id_created_at", "organisation_id", "created_at"),
        # Ownership lookups (plan P2) settle a job by its captured dispatch id.
        Index("ix_jobs_dispatch_id", "dispatch_id"),
        # Queued-job reconciliation (durable delivery plan P4) scans
        # non-terminal rows by status; the composite index serves both the
        # status filter and the age-ordered scan.
        Index("ix_jobs_status_created_at", "status", "created_at"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="job_status",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="job_progress_range"),
        CheckConstraint("attempt_count >= 0", name="non_negative_attempt_count"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The job kind the worker dispatches on (e.g. ``file.processing``). The
    # value is a plain string, not an enum: new job producers are expected
    # (each with its own task), so the catalogue is open-ended (rule of
    # three) and only the status values are closed.
    job_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="job_status",
            native_enum=False,
            length=16,
            # Persist the enum values ("queued", ...) so rows match the check
            # constraint and server default; SQLAlchemy defaults to names
            # ("QUEUED") for Python enums, which the constraint rejects.
            values_callable=_job_status_values,
        ),
        nullable=False,
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # A string reference to the resource the job operates on (for the example
    # job: the file id). It is deliberately not a foreign key: a job may in
    # future reference a record, an import batch or an email, so the reference
    # stays provider-neutral and the resource type lives in ``job_type``.
    input_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    result_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        # A job outlives its requester: users are deactivated, never hard
        # deleted, but if a user row is ever removed the job stays and the
        # creator reference is nulled (SET NULL, matching audit rows).
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Internal delivery ownership fields (durable delivery plan P1/P2). They
    # are deliberately not exposed by the JobDetail/list schemas and are not
    # foreign keys: ``dispatch_id`` names the outbox event whose publication
    # requested the current dispatch (outbox rows are cleaned up after the
    # retention window, so a FK would block cleanup), and
    # ``execution_lease_expires_at`` bounds how long a worker may own the
    # attempt before a stale/duplicate may take it over. A non-terminal legacy
    # row without a dispatch id receives one atomically on first claim.
    dispatch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, default=None)
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
