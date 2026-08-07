"""File metadata ORM model (Scope §6.3, blueprint §17, §10, §30).

The ``files`` table is the application's record of every object stored through
the direct-upload flow: the provider-neutral reference to where the bytes live
(provider, bucket, server-generated key) plus the declared metadata that was
validated before any signed URL was issued. The object key is always generated
by the server (``organisations/{organisation_id}/documents/{file_id}/original``)
and never accepted from a client; the client submits only the file id.

Every row hangs off exactly one organisation and every query filters on
``organisation_id`` first, so a file from another organisation is simply not
found (404), never visible — the same tenant-boundary discipline as ``records``.
Deletion is a soft delete (``deleted_at`` + ``status``), and list/detail
queries exclude deleted rows by default.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7


class FileStatus(enum.StrEnum):
    """Lifecycle state of a stored file (blueprint §17 lifecycle)."""

    PENDING = "pending"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    DELETED = "deleted"


def _file_status_values(enum_class: type[FileStatus]) -> list[str]:
    """Return the values a status column stores, not the enum names."""
    return [member.value for member in enum_class]


class File(Base, TimestampMixin):
    """One stored file's metadata record inside an organisation."""

    __tablename__ = "files"
    __table_args__ = (
        # The org-scoped list is the hot path, ordered newest-first; the
        # composite index serves both the filter and the sort, exactly like the
        # records list. The object key is server-generated per file id, so it
        # is unique: the constraint is cheap insurance against accidental key
        # reuse across providers or orgs.
        Index("ix_files_organisation_id_created_at", "organisation_id", "created_at"),
        CheckConstraint(
            "status IN ('pending', 'uploaded', 'processing', 'ready', "
            "'failed', 'quarantined', 'deleted')",
            name="file_status",
        ),
        CheckConstraint("size_bytes > 0", name="positive_size_bytes"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # The provider plane captured at intent time (blueprint §17: the metadata
    # record names the provider, the bucket and the key; URLs are never the
    # primary reference).
    storage_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Provider checksum (S3 ETag, fake SHA-256); opaque to application code,
    # which only ever compares it for equality (Scope §6.3).
    checksum: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    status: Mapped[FileStatus] = mapped_column(
        Enum(
            FileStatus,
            name="file_status",
            native_enum=False,
            length=16,
            # Persist the enum values ("pending", ...) so rows match the check
            # constraint and server default; SQLAlchemy defaults to names
            # ("PENDING") for Python enums, which the constraint rejects.
            values_callable=_file_status_values,
        ),
        nullable=False,
        default=FileStatus.PENDING,
        server_default=FileStatus.PENDING.value,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        # A file outlives its uploader: users are deactivated, never hard
        # deleted, but if a user row is ever removed the file stays and the
        # creator reference is nulled (SET NULL, matching audit rows).
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
