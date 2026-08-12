"""AI persistence ORM models (v0.7 Scope §6.5, BP §10, §27, §29).

Three tables implement the organisation controls and the usage/cost/audit
contract:

- ``organisation_ai_settings`` — one row per organisation (the unique
  ``organisation_id`` is the invariant), created at organisation creation time
  and defaulted to **off**, so AI is default-deny for every new organisation
  (v0.7 Scope §6.5, BP §27 "default off"). Provider/model ids are plain validated
  configuration validated against the registries at write time, never enum
  columns (the registries are the single source of truth).
- ``ai_requests`` — one row per attempted provider execution. The row is
  inserted **before** dispatch with ``status='running'`` and that attempt's
  bounded execution cost: the first running row *is* the budget reservation
  (v0.7 Scope §6.5 documented reservation policy), so concurrent executions serialize on
  the settings-row lock and a crashed execution cannot silently release its
  reservation. The first attempt row carries the execution's bounded worst-case
  gate; every further dispatch gets its own row via ``attempt_number``.
  Settlement updates each row to ``succeeded``/``failed`` with the actual
  usage/cost. ``request_id`` is the caller-visible AI request id; the triple
  ``(organisation_id, request_id, attempt_number)`` is unique, so a job retry
  that re-uses it cannot double-reserve budget and an attempt row can never be
  confused with another organisation's (BP §9). ``input_reference``/
  ``input_digest`` record where the input came from (a private storage
  reference and the SHA-256 digest of the resolved attachment) — never the
  bytes themselves (BP §28, ADR-0017).
- ``ai_outputs`` — the validated result of one request: the validated output
  JSON (only when the task-level opt-in and the organisation retention policy
  both permit content retention, v0.7 Scope §2) plus an output
  reference/digest and the input reference/digest it was derived from.
  Retention (``retention_policy_days``) is enforced by the §6.5 retention job,
  which deletes expired rows (and any scratch object they reference) and
  audits ``ai.retention_deleted``.

Cost is ``NUMERIC`` and all timestamps are timezone-aware UTC (BP §10). Every
row hangs off exactly one organisation; every read is org-scoped.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.ai.staging import ExternalReferenceStatus
from app.ai.transfer import (
    MAX_LARGE_ATTACHMENT_BYTES,
    TransferMode,
)
from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7


class AIRequestStatus(enum.StrEnum):
    """Lifecycle of one attempted provider execution (v0.7 Scope §6.5).

    ``queued`` is a pre-enqueue placeholder created before the broker message
    is published (v0.7 Scope §5.8) so the result endpoint is coherent
    immediately after ``202``; the row carries no routing or budget
    information. ``reserve()`` promotes it to ``running`` (the budget
    reservation) when the worker dispatches. Settlement moves the row to
    ``succeeded`` or ``failed``. Terminal rows are never re-run, and a row
    stuck in ``running`` (a crashed worker) is reconciled by the retention
    job, which marks it ``failed`` and keeps its reserved cost — conservative,
    never a silent budget release.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _request_status_values(enum_class: type[AIRequestStatus]) -> list[str]:
    """Return the values a status column stores, not the enum names."""
    return [member.value for member in enum_class]


class OrganisationAISettings(Base, TimestampMixin):
    """One organisation's AI policy row (default off, v0.7 Scope §6.5)."""

    __tablename__ = "organisation_ai_settings"
    __table_args__ = (
        CheckConstraint(
            "monthly_budget IS NULL OR monthly_budget >= 0",
            name="non_negative_monthly_budget",
        ),
        CheckConstraint(
            "retention_policy_days IS NULL OR retention_policy_days > 0",
            name="positive_retention_policy_days",
        ),
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint(
            "max_large_attachment_bytes > 0 AND max_large_attachment_bytes <= 50000000",
            name="max_large_attachment_bytes_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    # Default-deny: an organisation's AI capability starts off and stays off
    # until a platform administrator explicitly enables it (v0.7 Scope §6.5, BP §27
    # "default off"). Enforcement happens in AIService through the persistence
    # port, never in a router or the frontend.
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    # Registry-validated allowlists: empty list means "no restriction from
    # this knob" (the organisation opted into AI without constraining it).
    allowed_provider_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    allowed_model_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # Optional forced provider/model (v0.7 Scope §6.5 "optional provider/model
    # override"): validated against the registries at write time so an unknown
    # id can never be stored, and coherent (a forced model must live under the
    # forced provider) so routing can never silently mis-resolve.
    provider_override: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_override: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Monthly budget in the pricing currency (NUMERIC, BP §10). ``NULL`` means
    # no budget is configured. Enforcement and the reservation policy live in
    # ``app/ai/persistence/service.py``.
    monthly_budget: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True, default=None
    )
    # How long ai_outputs records (and the scratch objects they reference)
    # are kept. ``NULL`` means no retention deletion is scheduled.
    retention_policy_days: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # v0.8 Scope §2.2 transfer policy: the organisation's allowed transfer
    # modes default to ``inline`` only (default-deny — a non-inline mode is
    # never eligible until a platform administrator explicitly enables it) and
    # ``max_large_attachment_bytes`` tightens the 50,000,000-byte template
    # ceiling. Mode ids are plain validated configuration checked against the
    # transfer contract at write time, never enum columns (the registry and
    # provider contract are the single source of truth, like the allowlists).
    allowed_transfer_modes: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: [TransferMode.INLINE.value],
        server_default=text("'[\"inline\"]'::jsonb"),
    )
    max_large_attachment_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=MAX_LARGE_ATTACHMENT_BYTES,
        server_default=text("50000000"),
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        # Settings outlive the administrator who last changed them; a user row
        # is never hard-deleted, but if it ever is the reference is nulled.
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Full policy replacements are collaboratively managed by platform
    # administrators. Clients must submit the version they read; the service
    # locks the row and rejects stale replacements with 409 Conflict (BP §10).
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )


class AIRequestRecord(Base, TimestampMixin):
    """One attempted provider execution and its usage/cost outcome."""

    __tablename__ = "ai_requests"
    __table_args__ = (
        # The org-scoped month-spend scan and the retention query are the hot
        # paths; the composite index serves the org filter plus the
        # newest-first ordering, exactly like files/records/jobs.
        Index("ix_ai_requests_organisation_id_created_at", "organisation_id", "created_at"),
        # One row per actual provider dispatch, uniquely identified inside the
        # organisation by the caller-visible execution id and the 1-based
        # attempt number (v0.7 Scope §2). Org-scoped so a reused execution id
        # can never collide across tenants, and the idempotency key for job
        # redelivery is the org + execution id.
        UniqueConstraint(
            "organisation_id",
            "request_id",
            "attempt_number",
            name="uq_ai_requests_org_request_attempt",
        ),
        # The unique (id, organisation_id) pair the ai_outputs composite
        # foreign key references, so an output can never belong to a different
        # organisation than its parent request (BP §9).
        UniqueConstraint("id", "organisation_id", name="uq_ai_requests_id_organisation_id"),
        CheckConstraint("attempt_number >= 1", name="positive_attempt_number"),
        CheckConstraint("input_tokens >= 0", name="non_negative_input_tokens"),
        CheckConstraint("output_tokens >= 0", name="non_negative_output_tokens"),
        CheckConstraint("estimated_cost >= 0", name="non_negative_estimated_cost"),
        CheckConstraint("cost >= 0", name="non_negative_cost"),
        CheckConstraint("latency_ms >= 0", name="non_negative_latency_ms"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ai_request_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The initiating user. Users are deactivated, never hard-deleted; if a user
    # row is ever removed the reference is nulled (SET NULL, matching audit).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # The caller-visible AI request id (``AIService.execute`` request_id), the
    # execution-level idempotency key shared by all of one execution's attempt
    # rows. Uniqueness is per (organisation_id, request_id, attempt_number).
    request_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # 1-based position of this dispatch within its execution. Every attempted
    # provider execution gets its own row, so retries and fallback attempts are
    # all persisted and priced with their own model (v0.7 Scope §2).
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    task: Mapped[str] = mapped_column(String(128), nullable=False)
    # Routing columns are NULL while a request is ``queued`` (created before
    # enqueue but not yet dispatched); the provider/model/prompt are filled by
    # ``reserve()`` when it promotes the row to ``running`` (v0.7 Scope §5.8).
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    prompt_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    routing_reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # The adapter's configured deployment region (v0.7 Scope §6.3 regional
    # amendment); empty where the provider exposes no template-controlled
    # pinning. Never used as a routing input here — it records where the
    # request actually ran so residency can be verified after the fact.
    region: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[AIRequestStatus] = mapped_column(
        Enum(
            AIRequestStatus,
            name="ai_request_status",
            native_enum=False,
            length=16,
            values_callable=_request_status_values,
        ),
        nullable=False,
        default=AIRequestStatus.RUNNING,
        server_default=AIRequestStatus.RUNNING.value,
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # This dispatch's own route estimate. ``cost`` separately holds the first
    # row's bounded execution reservation while running and actual billed cost
    # after settlement.
    estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    latency_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Safe error code from the AI taxonomy when the execution failed; null on
    # success. Never a stack trace, prompt, or provider response (BP §28).
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Where the input came from: the private storage reference and the SHA-256
    # digest of the resolved attachment. Never the bytes (BP §28, ADR-0017).
    input_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AIOutputRecord(Base, TimestampMixin):
    """The validated, privacy-safe result of one AI request (v0.7 Scope §6.5)."""

    __tablename__ = "ai_outputs"
    __table_args__ = (
        # One output per request (the settlement path writes at most one row).
        UniqueConstraint("ai_request_id", name="uq_ai_outputs_ai_request_id"),
        # The output's organisation must be its parent request's organisation:
        # the composite foreign key makes the request/output tenant
        # relationship a database invariant, not just an application check
        # (BP §9). Requires the matching unique pair on ai_requests.
        ForeignKeyConstraint(
            ["ai_request_id", "organisation_id"],
            ["ai_requests.id", "ai_requests.organisation_id"],
            name="fk_ai_outputs_ai_request_org_ai_requests",
            ondelete="CASCADE",
        ),
        # Retention sweeps the org's outputs oldest-first; the composite index
        # serves both the org filter and the created_at ordering.
        Index("ix_ai_outputs_organisation_id_created_at", "organisation_id", "created_at"),
        CheckConstraint(
            "human_rating IS NULL OR human_rating BETWEEN 1 AND 5",
            name="human_rating_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    ai_request_id: Mapped[uuid.UUID] = mapped_column(
        # The composite constraint below is the sole request FK; adding a
        # redundant single-column FK here makes ORM metadata drift from the
        # migration without strengthening tenant integrity.
        nullable=False
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The validated structured output (or text result) the service returned.
    # The task's Pydantic model already validated it before it reached here,
    # so invalid data can never be stored as a successful output (v0.7 Scope §6.4).
    # ``NULL`` is the safe default: content is stored only when the task-level
    # opt-in and the organisation retention policy both permit it (v0.7 Scope
    # §2), otherwise the record is references/digests only.
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # When the output itself lives in storage (e.g. an analyse-only scratch
    # object), the private reference and its digest. Never the bytes.
    output_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The input the output was derived from, copied from the request so the
    # record is self-describing: a reference and/or SHA-256 digest, never the
    # source content (BP §28, ADR-0017).
    input_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Human review fields (v0.7 Scope §2 ai_outputs contract): filled by a feature
    # when a human rates/approves the result.
    human_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


def _transfer_mode_values() -> list[str]:
    """Return the values the transfer-mode column stores, not the enum names.

    The table is the durable record of non-inline transfers (Scope §2.3);
    inline transfers produce no durable reference and are refused by the
    orchestrator, so the column never stores ``inline`` — a database invariant,
    not just an application check.
    """
    return [mode.value for mode in TransferMode if mode is not TransferMode.INLINE]


def _reference_status_values() -> list[str]:
    """Return the values the reference-status column stores."""
    return [status.value for status in ExternalReferenceStatus]


class AIAttachmentReference(Base, TimestampMixin):
    """One organisation-scoped durable transfer reference (v0.8 Scope §2.3, §6.3).

    The durable record of one non-inline transfer of a private source object:
    the logical request it belongs to, the provider/mode/region, the opaque
    external id (a provider file id or ``gs://`` URI — never a managed signed
    URL), the verified source identity (reference, SHA-256 digest, size, MIME,
    lifecycle) and the lifecycle state (live/expired/deleted) with its
    timestamps. It **never** stores bytes, credentials, request headers, a
    managed signed URL or its query string, or raw provider responses (Scope
    §2.3, BP §28). A managed URL is minted anew for each dispatch and is not a
    reusable durable reference.

    The idempotency contract (Scope §2.3): the partial unique index on
    ``(organisation_id, idempotency_key) WHERE status = 'live'`` enforces at
    most **one live transfer** per derived idempotency key per organisation.
    The key is the SHA-256 of the exact reuse predicate (provider, mode,
    organisation, logical request, digest, region —
    :func:`app.ai.transfer.derive_idempotency_key`), so a retry reuses only a
    live matching record from the same logical request and a changed digest,
    provider, mode or region produces a different key and therefore a new
    idempotent transfer (Scope §5.4). Expired and deleted rows are excluded by
    the partial predicate, so an expired reference never blocks its
    replacement.

    Rows hang off exactly one organisation and every read is org-scoped (BP
    §9); a cross-organisation lookup is indistinguishable from a missing row.
    """

    __tablename__ = "ai_attachment_references"
    __table_args__ = (
        # Retry-only reuse idempotency (Scope §2.3): exactly one live transfer
        # per organisation per derived idempotency key. The partial predicate
        # lets an expired/deleted row be replaced without first deleting it.
        Index(
            "uq_ai_attachment_references_org_key_live",
            "organisation_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status = 'live'"),
        ),
        # Request-scoped lifecycle operations (expire/delete all references of
        # one logical request) and the org-scoped newest-first scan.
        Index(
            "ix_ai_attachment_references_org_request",
            "organisation_id",
            "logical_request_id",
        ),
        Index(
            "ix_ai_attachment_references_organisation_id_created_at",
            "organisation_id",
            "created_at",
        ),
        CheckConstraint(
            f"transfer_mode IN ({', '.join(repr(mode) for mode in _transfer_mode_values())})",
            name="transfer_mode",
        ),
        CheckConstraint(
            "source_lifecycle IN ('transient', 'retained')",
            name="source_lifecycle",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in _reference_status_values())})",
            name="status",
        ),
        # The verified source contract: a positive size within the template
        # ceiling (the organisation/deployment ceilings are enforced at
        # selection time) and a SHA-256 digest in canonical hex form.
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 50000000",
            name="size_range",
        ),
        CheckConstraint(
            "source_digest ~ '^[0-9a-f]{64}$'",
            name="digest_format",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The caller-visible logical AI request this transfer belongs to; retries
    # of one logical request share it and may reuse a live reference (Scope
    # §2.1 retry-only reuse).
    logical_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Provider and transfer mode are plain validated configuration against the
    # registries/contract (like the organisation allowlists); the mode column
    # is additionally guarded by the CHECK above so a stale declaration can
    # never be stored.
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    transfer_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    # The opaque external id: a provider file id or ``gs://`` URI. For the
    # managed-signed-url mode there is no provider copy, so the external id is
    # the source reference itself (the immutable object identity). Never a
    # managed signed URL (Scope §2.3).
    external_id: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    source_lifecycle: Mapped[str] = mapped_column(String(16), nullable=False)
    region: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ExternalReferenceStatus.LIVE.value,
        server_default=text("'live'"),
    )
    # The derived idempotency key (SHA-256 of the reuse predicate) — the
    # durable uniqueness identity, shared with the TransferStore so the two can
    # never drift about which transfer a retry reuses.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Safe error code from the AI taxonomy when the transfer failed terminally;
    # null while live. Never a stack trace, provider response or URL (BP §28).
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Bounded safe metadata (e.g. provider-reported display name); never bytes,
    # credentials, headers, a signed URL or raw responses (Scope §2.3). The
    # mapped attribute is ``event_metadata`` because ``metadata`` is reserved
    # in the SQLAlchemy declarative API (it would shadow ``Base.metadata``);
    # the database column keeps the §2.3 name ``metadata``.
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    # Lifecycle timestamps: the provider-side expiry (None where the provider
    # has no automatic expiry), the last reuse time and the terminal deletion
    # time. Timezone-aware UTC (BP §10).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
