"""The persistence/policy port ``AIService`` calls at request time (v0.7 Scope §6.5).

``AIService`` stays provider-neutral and persistence-neutral (ADR-0017): the
checked-in application wires a session-bound implementation of this port
(``app/ai/persistence/service.AIPersistencePortImpl``) into ``execute``, and
tests may substitute a deterministic fake. The port is mandatory — execution
without it fails closed, so the documented application-facing entry point can
never dispatch with no enabled-state, allowlist, budget, persistence or audit
enforcement (v0.7 Scope §2 and §6.5).

The port has four responsibilities (v0.7 Scope §6.5):

1. :meth:`load_policy` — the organisation's effective AI policy (enabled,
   allowed providers/models, overrides, retention policy). ``AIService``
   raises ``AIUnavailableError`` when disabled and passes the restrictions to
   the router before any dispatch.
2. :meth:`reserve` — the transaction-safe budget gate for one execution:
   locks the settings row ``FOR UPDATE``, checks the month's committed spend
   against the bounded worst-case estimate for the whole retry/repair policy,
   and inserts the first ``ai_requests`` row in ``running`` state carrying the
   bounded reservation and first routing decision. Idempotent on
   ``(organisation_id, request_id)``:
   a retried job re-using the execution id receives the existing first row
   without a second reservation or a second budget charge.
3. :meth:`record_attempt` — one additional ``ai_requests`` row (in
   ``running`` state) per further attempted provider execution, so the
   durable records match v0.7 Scope §2's one-row-per-attempt contract. Also
   idempotent on ``(organisation_id, request_id, attempt_number)``. No
   separate budget gate: the bounded worst case was reserved by
   :meth:`reserve` before the first dispatch.
4. :meth:`settle` — terminates the request rows with actuals, and for the
   successful attempt also writes the validated ``ai_outputs`` record — all
   in one transaction with the audit event, so terminal success plus
   output/audit is atomic (BP §11: the service owns transaction boundaries,
   and no transaction ever spans provider I/O). Output content is retained
   only when the task-level opt-in and the organisation retention policy both
   permit it; otherwise the record stores references/digests only.

The audit events for completion/failure/budget-denial/settings-change/
retention-deletion are written by the port's implementation in the same
transactions as the records (BP §11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from app.ai.schemas import CostEstimate, TokenUsage
from app.ai.transfer import MAX_LARGE_ATTACHMENT_BYTES, TransferMode


@dataclass(frozen=True)
class AIRequestReservation:
    """Result of the idempotent first-attempt reservation.

    ``created`` is false when this execution id already has a durable first
    row.  ``AIService`` then refuses to dispatch again; the durable jobs layer
    can reuse its previously stored outcome without incurring another provider
    charge (v0.7 Scope §6.5/§6.6).
    """

    row_id: UUID
    created: bool


@dataclass(frozen=True)
class OrganisationAIPolicy:
    """The effective AI policy for one organisation at request time.

    Empty allowlists mean "no restriction from this knob"; ``provider_override``
    and ``model_override`` force the router's selection. ``monthly_budget``
    ``None`` means no budget is configured; ``retention_policy_days`` ``None``
    means no retention deletion is scheduled (and, together with the task-level
    opt-in, no output content is retained).

    v0.8 Scope §2.2 transfer policy: ``allowed_transfer_modes`` defaults to
    ``inline`` only (default-deny for every organisation — a non-inline mode is
    never eligible until a platform administrator explicitly enables it);
    ``max_large_attachment_bytes`` ``None`` means the template ceiling
    (50,000,000 bytes) applies unchanged, and a configured value tightens it.
    ``AIService`` intersects these with the task/model declarations and the
    provider contract before any external transfer (Scope §6.2).
    """

    enabled: bool
    allowed_provider_ids: list[str] = field(default_factory=list[str])
    allowed_model_ids: list[str] = field(default_factory=list[str])
    provider_override: str | None = None
    model_override: str | None = None
    monthly_budget: Decimal | None = None
    retention_policy_days: int | None = None
    allowed_transfer_modes: list[TransferMode] = field(
        default_factory=lambda: [TransferMode.INLINE]
    )
    max_large_attachment_bytes: int | None = None

    def effective_max_large_attachment_bytes(self) -> int:
        """The organisation's large-attachment ceiling, defaulting to template."""
        if self.max_large_attachment_bytes is None:
            return MAX_LARGE_ATTACHMENT_BYTES
        return self.max_large_attachment_bytes


@runtime_checkable
class AIPersistencePort(Protocol):
    """The session-bound policy/reservation/recording seam used by AIService."""

    async def load_policy(self, *, organisation_id: UUID) -> OrganisationAIPolicy: ...

    async def reserve(
        self,
        *,
        organisation_id: UUID,
        user_id: UUID,
        request_id: str,
        task: str,
        provider: str,
        model: str,
        prompt_name: str,
        prompt_version: int,
        routing_reason: str,
        fallback_used: bool,
        region: str,
        estimated_cost: Decimal,
        execution_maximum_estimated_cost: Decimal,
        input_reference: str | None,
        input_digest: str | None,
    ) -> AIRequestReservation:
        """Gate the execution budget and create the first running request row.

        ``estimated_cost`` is the first attempt's own route estimate;
        ``execution_maximum_estimated_cost`` is the bounded worst case for the
        whole retry/repair policy and is both stored as the running reservation
        and compared against monthly spend. Both are retained so route evidence
        stays accurate without weakening the budget gate.

        Idempotent on ``(organisation_id, request_id)``: a retried job
        re-using the execution id receives the existing first row with
        ``created=False``, allowing the caller to refuse a second provider
        dispatch. Raises
        ``app.ai.errors.BudgetExceededError`` when the reservation would
        overrun the organisation's monthly budget.
        """
        ...

    async def record_attempt(
        self,
        *,
        organisation_id: UUID,
        user_id: UUID,
        request_id: str,
        attempt_number: int,
        task: str,
        provider: str,
        model: str,
        prompt_name: str,
        prompt_version: int,
        routing_reason: str,
        fallback_used: bool,
        region: str,
        estimated_cost: Decimal,
        input_reference: str | None,
        input_digest: str | None,
    ) -> UUID:
        """Create one further running request row for an actual dispatch.

        Called for every attempted provider execution after the first, so the
        durable records price the real traffic attempt by attempt (v0.7 Scope
        §2). Idempotent on ``(organisation_id, request_id, attempt_number)``:
        a redelivered job re-uses the existing row. No budget gate — the
        bounded worst case was reserved by :meth:`reserve`.
        """
        ...

    async def settle(
        self,
        *,
        ai_request_id: UUID,
        organisation_id: UUID,
        task: str,
        user_id: UUID | None,
        status: str,
        error_code: str | None,
        usage: TokenUsage,
        cost: CostEstimate,
        latency_ms: int,
        routing_provider: str,
        routing_model: str,
        routing_prompt_name: str,
        routing_prompt_version: int,
        routing_reason: str,
        fallback_used: bool,
        region: str,
        output: Any | None = None,
        output_reference: str | None = None,
        output_digest: str | None = None,
        retain_content: bool = False,
        input_reference: str | None = None,
        input_digest: str | None = None,
    ) -> None:
        """Terminate one request row with actuals, output and audit atomically.

        ``output`` (with its reference/digest) is written for the successful
        attempt in the same transaction as the row update and the audit event,
        so a success can never be durable without its output record. When
        ``retain_content`` is false the ``output_json`` cell stays ``NULL`` —
        the record carries references/digests only (v0.7 Scope §2 retention
        choice). A row that is no longer ``running`` is an already-settled
        retried message: terminal states are never re-run, so it is a no-op.
        """
        ...
