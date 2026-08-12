"""AI persistence, policy and budget services (v0.7 Scope §6.5, BP §9-§11, §27-§29).

This module is the database-backed half of the AI platform contract:

- **Settings management** (:func:`get_ai_settings`, :func:`update_ai_settings`)
  — the platform-gated surface for one organisation's AI policy. Provider and
  model ids are validated against the checked-in registries before any row is
  written, so an unknown provider/model override can never be stored
  (acceptance §5.2). The row is created at organisation-creation time (default
  **off**, BP §27); the management functions create it defensively when
  missing, and the policy port treats a missing row as disabled (fail-safe).
- **Request-time enforcement** (:class:`AIPersistencePortImpl`) — the
  session-bound implementation of the port ``AIService`` calls. It loads the
  effective policy (enabled check), reserves budget before dispatch, settles
  the request with actuals, records the privacy-safe output, and writes the
  audit events — all in the same transactions as the records (BP §11).
- **Retention/deletion** (:func:`enforce_ai_retention`) — the privacy-safe
  retention sweep the §6.5 job runs: expired ``ai_outputs`` rows (and any
  scratch object they reference) are deleted per the organisation's retention
  policy, orphaned scratch objects older than the policy are swept from the
  organisation-scoped AI scratch namespace, stale ``running`` reservations are
  reconciled to ``failed`` (keeping their reserved cost), and one
  ``ai.retention_deleted`` audit event records the purge. Keep-flow objects
  under ``organisations/{org}/documents/…`` are never touched.

## Documented budget reservation policy (v0.7 Scope §6.5)

Monthly spend = the sum of ``cost`` over ``ai_requests`` rows for the
organisation in the current UTC calendar month whose status is ``running``,
``succeeded`` or ``failed``. The first ``running`` row carries the bounded
worst-case cost for the entire retry/repair policy; later running rows carry
zero additional reserved cost because the first row already covers them.
Each row separately retains its own routing estimate. A terminal row carries
actual usage-priced cost. Before dispatch,
:meth:`AIPersistencePortImpl.reserve` locks the organisation's settings row
``FOR UPDATE``, so concurrent reservations for one organisation serialize on
the same lock and each sees every earlier reservation in the month's sum — a
budget can never be overrun by parallel executions. The terminal tail settles
the first row last, keeping the bounded reservation durable until every later
attempt's actual cost is committed. Settlement then replaces the reservation
with actual cost and moves the row to a terminal state; a
row stuck in ``running`` (a crashed worker) is reconciled by the retention job
to ``failed`` *keeping its reserved cost*, so a crash can never silently
release budget. Actual cost remains bounded by the task's reviewed cost
ceilings (v0.7 Scope §6.2/§6.4).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.errors import AIUnavailableError, BudgetExceededError
from app.ai.persistence.models import (
    AIOutputRecord,
    AIRequestRecord,
    AIRequestStatus,
    OrganisationAISettings,
)
from app.ai.persistence.port import AIRequestReservation, OrganisationAIPolicy
from app.ai.persistence.queries import (
    ai_month_spend_statement,
    ai_request_by_request_id_statement,
    ai_request_record_statement,
    expired_ai_outputs_statement,
    organisation_ai_settings_for_update_statement,
    organisation_ai_settings_statement,
    organisations_with_retention_policy_statement,
    stale_running_requests_statement,
)
from app.ai.registry import CapabilityCostModelRegistry, load_registry_bundle
from app.ai.schemas import CostEstimate, TokenUsage
from app.core.config import AI_KNOWN_PROVIDER_IDS
from app.core.exceptions import ErrorDetail, NotFoundError, ValidationError
from app.modules.audit.service import (
    ACTION_AI_BUDGET_DENIED,
    ACTION_AI_REQUEST_COMPLETED,
    ACTION_AI_REQUEST_FAILED,
    ACTION_AI_RETENTION_DELETED,
    ACTION_AI_SETTINGS_UPDATED,
    record_event,
)
from app.modules.organisations.models import Organisation
from app.modules.users.models import User
from app.storage.base import ObjectStorage

#: The organisation-scoped AI scratch namespace (v0.7 Scope §6.5 item 4): temporary
#: analyse-only objects live here so the retention sweep can target them and
#: keep-flow objects under ``organisations/{org}/documents/…`` (feature-owned,
#: v0.7 Scope §6.3) are never touched by the AI layer.
SCRATCH_KEY_PREFIX = "organisations/{organisation_id}/ai/scratch/"

#: A reservation older than this while still ``running`` is a crashed worker
#: execution; the retention job reconciles it to ``failed`` keeping its cost.
STALE_RUNNING_THRESHOLD = timedelta(hours=24)

#: The bounded page size of the scratch-namespace listing sweep. The sweep
#: advances past every listed page, so an expired object beyond the first page
#: can never be stranded while lexicographically earlier fresh objects keep
#: filling the page (v0.7 Scope §6.5 item 4).
SCRATCH_SWEEP_PAGE_SIZE = 1000

#: The budget-denial error code used on the durable reservation path.
ERROR_CODE_BUDGET_DENIED = "budget_exceeded"
#: Error code the retention job stamps on reconciled crashed reservations.
ERROR_CODE_WORKER_CRASHED = "worker_crashed"


def ai_scratch_prefix(organisation_id: uuid.UUID) -> str:
    """Return the organisation-scoped scratch key prefix."""
    return SCRATCH_KEY_PREFIX.format(organisation_id=organisation_id)


@lru_cache(maxsize=1)
def _model_registry() -> CapabilityCostModelRegistry:
    """The checked-in model registry, cached (the registry is immutable)."""
    return load_registry_bundle().models


def _registry_error(field: str, message: str) -> ValidationError:
    return ValidationError(
        code="ai_settings_invalid",
        message="The AI settings contain invalid registry references.",
        details=[ErrorDetail(field=field, message=message)],
    )


def _validate_policy_identifiers(
    *,
    allowed_provider_ids: list[str],
    allowed_model_ids: list[str],
    provider_override: str | None,
    model_override: str | None,
) -> None:
    """Validate provider/model ids and overrides against the registries.

    Unknown ids, duplicate ids and contradictory overrides fail fast with an
    actionable message before any row is written (acceptance §5.2: unknown
    provider/model overrides must fail with actionable errors). A forced model
    must be consistent with the allowlists and the forced provider, otherwise
    the router could never satisfy it and the configuration would silently
    mis-resolve at request time.
    """
    if len(set(allowed_provider_ids)) != len(allowed_provider_ids):
        raise _registry_error("allowed_provider_ids", "provider ids must not contain duplicates")
    unknown_providers = set(allowed_provider_ids) - AI_KNOWN_PROVIDER_IDS
    if unknown_providers:
        raise _registry_error(
            "allowed_provider_ids",
            f"unknown provider ids: {sorted(unknown_providers)}",
        )
    if len(set(allowed_model_ids)) != len(allowed_model_ids):
        raise _registry_error("allowed_model_ids", "model ids must not contain duplicates")
    registry = _model_registry()
    known_model_ids = {model.id for model in registry.all()}
    unknown_models = set(allowed_model_ids) - known_model_ids
    if unknown_models:
        raise _registry_error(
            "allowed_model_ids",
            f"unknown model ids: {sorted(unknown_models)}",
        )

    if provider_override is not None:
        if provider_override not in AI_KNOWN_PROVIDER_IDS:
            raise _registry_error(
                "provider_override",
                f"unknown provider id: {provider_override!r}",
            )
        if allowed_provider_ids and provider_override not in allowed_provider_ids:
            raise _registry_error(
                "provider_override",
                "the provider override must be inside allowed_provider_ids "
                "(or the allowlist must be empty)",
            )
    if model_override is not None:
        try:
            override_model = registry.get_by_id(model_override)
        except KeyError as exc:
            raise _registry_error(
                "model_override",
                f"unknown model id: {model_override!r}",
            ) from exc
        if allowed_model_ids and model_override not in allowed_model_ids:
            raise _registry_error(
                "model_override",
                "the model override must be inside allowed_model_ids "
                "(or the allowlist must be empty)",
            )
        if provider_override is not None and override_model.provider != provider_override:
            raise _registry_error(
                "model_override",
                f"the forced model lives under provider {override_model.provider!r}, "
                f"not the forced provider {provider_override!r}",
            )


async def _get_organisation_or_404(session: AsyncSession, organisation_id: uuid.UUID) -> None:
    """Raise the standard 404 when the organisation does not exist."""
    organisation = await session.scalar(
        select(Organisation).where(Organisation.id == organisation_id)
    )
    if organisation is None:
        raise NotFoundError(
            code="organisation_not_found",
            message="The organisation could not be found.",
        )


async def create_default_settings(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
) -> OrganisationAISettings:
    """Insert the default-off policy row for a new organisation.

    Called from the organisation-creation services inside their own
    transaction (v0.7 Scope §6.5: AI is default-off for new organisations, BP §27).
    The unique ``organisation_id`` is the one-row-per-organisation invariant.
    """
    settings_row = OrganisationAISettings(organisation_id=organisation_id)
    session.add(settings_row)
    await session.flush()
    return settings_row


async def get_ai_settings(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
) -> OrganisationAISettings:
    """Return one organisation's policy row, creating the default when missing.

    An unknown organisation is a 404 (the platform surface always operates on a
    concrete organisation, matching the memberships/feature-flag listings). A
    known organisation without a row gets the default-off row — the defensive
    backstop that keeps the one-row-per-organisation invariant even if a gap in
    eager creation ever appears.
    """
    await _get_organisation_or_404(session, organisation_id)
    rows = (await session.scalars(organisation_ai_settings_statement(organisation_id))).all()
    settings_row = next(
        (row for row in rows if row.organisation_id == organisation_id),
        None,
    )
    if settings_row is None:
        settings_row = await create_default_settings(session, organisation_id=organisation_id)
        await session.commit()
        await session.refresh(settings_row)
    return settings_row


async def update_ai_settings(
    session: AsyncSession,
    *,
    actor: User,
    organisation_id: uuid.UUID,
    enabled: bool,
    allowed_provider_ids: list[str],
    allowed_model_ids: list[str],
    provider_override: str | None,
    model_override: str | None,
    monthly_budget: Decimal | None,
    retention_policy_days: int | None,
) -> OrganisationAISettings:
    """Replace one organisation's AI policy and audit the change.

    The registry validation runs before any write, so an invalid policy never
    reaches the row; the audit event commits inside the same transaction. The
    row is created when missing (the platform can enable AI for an
    organisation whose row predates this release).
    """
    await _get_organisation_or_404(session, organisation_id)
    _validate_policy_identifiers(
        allowed_provider_ids=allowed_provider_ids,
        allowed_model_ids=allowed_model_ids,
        provider_override=provider_override,
        model_override=model_override,
    )
    rows = (await session.scalars(organisation_ai_settings_statement(organisation_id))).all()
    settings_row = next(
        (row for row in rows if row.organisation_id == organisation_id),
        None,
    )
    if settings_row is None:
        settings_row = OrganisationAISettings(organisation_id=organisation_id)
        session.add(settings_row)
    settings_row.enabled = enabled
    settings_row.allowed_provider_ids = allowed_provider_ids
    settings_row.allowed_model_ids = allowed_model_ids
    settings_row.provider_override = provider_override
    settings_row.model_override = model_override
    settings_row.monthly_budget = monthly_budget
    settings_row.retention_policy_days = retention_policy_days
    settings_row.updated_by_user_id = actor.id
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor.id,
        action=ACTION_AI_SETTINGS_UPDATED,
        resource_type="organisation_ai_settings",
        resource_id=str(organisation_id),
        metadata={
            "enabled": enabled,
            "allowed_provider_ids": allowed_provider_ids,
            "allowed_model_ids": allowed_model_ids,
            "provider_override": provider_override,
            "model_override": model_override,
            "monthly_budget": str(monthly_budget) if monthly_budget is not None else None,
            "retention_policy_days": retention_policy_days,
        },
    )
    await session.commit()
    await session.refresh(settings_row)
    return settings_row


async def get_organisation_policy(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
) -> OrganisationAIPolicy:
    """Return the effective AI policy for request-time enforcement.

    A missing row resolves to default-deny (disabled, no restrictions), so an
    organisation without a settings row can never use AI — the same
    fail-safe rule as feature flags (BP §27 default off).
    """
    settings_row = await session.scalar(organisation_ai_settings_statement(organisation_id))
    if settings_row is None:
        return OrganisationAIPolicy(enabled=False)
    return OrganisationAIPolicy(
        enabled=settings_row.enabled,
        allowed_provider_ids=list(settings_row.allowed_provider_ids),
        allowed_model_ids=list(settings_row.allowed_model_ids),
        provider_override=settings_row.provider_override,
        model_override=settings_row.model_override,
        monthly_budget=settings_row.monthly_budget,
        retention_policy_days=settings_row.retention_policy_days,
    )


class AIPersistencePortImpl:
    """Session-bound :class:`AIPersistencePort` used by ``AIService``.

    Construct one per execution with the caller's session and pass it to
    :meth:`app.ai.service.AIService.execute` — the §6.6 demo service and the
    ``ai.execute`` job both follow this pattern.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_policy(self, *, organisation_id: uuid.UUID) -> OrganisationAIPolicy:
        return await get_organisation_policy(self._session, organisation_id=organisation_id)

    async def reserve(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
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

        The settings row is locked ``FOR UPDATE`` while the month's spend is
        summed and the reservation inserted, so concurrent executions for one
        organisation serialize (documented reservation policy above). The gate
        compares against ``execution_maximum_estimated_cost`` — the bounded
        worst case for the whole retry/repair policy — so a retry-heavy
        execution can never collectively overrun the budget after passing a
        per-attempt check. ``estimated_cost`` is retained as the first
        dispatch's own routing estimate while ``cost`` holds the bounded
        execution reservation.

        Idempotent on ``(organisation_id, request_id)``: a retried job
        re-using the execution id returns the existing first attempt row
        without a second reservation or a second budget charge. A denied
        reservation commits its ``ai.budget_denied`` audit event and raises
        :class:`BudgetExceededError` before any dispatch. The lookup and the
        insert are org-scoped, so a reused caller request id can never return
        or mutate another organisation's row (BP §9), and a lost race against
        a concurrent duplicate execution id falls back to the winner's row
        instead of surfacing a constraint error.
        """
        session = self._session
        existing = await session.scalar(
            ai_request_by_request_id_statement(organisation_id, request_id, 1)
        )
        # A pre-enqueued ``queued`` row is adopted (promoted to ``running`` with
        # the budget reservation) after the settings-row lock below. Any other
        # existing row is a replay: the caller re-used the execution id (v0.7
        # Scope §6.5/§6.6).
        if existing is not None and existing.status != AIRequestStatus.QUEUED:
            row_id = existing.id
            await session.commit()
            return AIRequestReservation(row_id=row_id, created=False)

        settings_row = await session.scalar(
            organisation_ai_settings_for_update_statement(organisation_id)
        )
        if settings_row is None:
            raise AIUnavailableError("AI is not enabled for this organisation")
        # The pre-lock lookup can race with another reservation. Re-check after
        # acquiring the organisation lock and before evaluating budget, so a
        # duplicate returns the winner instead of recording a false budget
        # denial when headroom is tight.
        existing = await session.scalar(
            ai_request_by_request_id_statement(organisation_id, request_id, 1)
        )
        if existing is not None and existing.status != AIRequestStatus.QUEUED:
            row_id = existing.id
            await session.commit()  # releases the settings-row lock
            return AIRequestReservation(row_id=row_id, created=False)
        if settings_row.monthly_budget is not None:
            now = datetime.now(UTC)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            spent = await session.scalar(
                ai_month_spend_statement(organisation_id, month_start)
            ) or Decimal("0")
            if spent + execution_maximum_estimated_cost > settings_row.monthly_budget:
                await record_event(
                    session,
                    organisation_id=organisation_id,
                    actor_user_id=user_id,
                    action=ACTION_AI_BUDGET_DENIED,
                    resource_type="ai_request",
                    resource_id=request_id,
                    metadata={
                        "task": task,
                        "estimated_cost": str(execution_maximum_estimated_cost),
                        "spent": str(spent),
                        "monthly_budget": str(settings_row.monthly_budget),
                    },
                )
                await session.commit()
                raise BudgetExceededError("the organisation's monthly AI budget is exhausted")

        if existing is not None and existing.status == AIRequestStatus.QUEUED:
            # Adopt the pre-enqueued row (v0.7 Scope §5.8): promote it from
            # ``queued`` to ``running``, fill the routing columns that were
            # unknown at enqueue time, and apply the execution-level budget
            # reservation. This is the first actual dispatch, not a replay.
            existing.status = AIRequestStatus.RUNNING
            existing.provider = provider
            existing.model = model
            existing.prompt_name = prompt_name
            existing.prompt_version = prompt_version
            existing.routing_reason = routing_reason
            existing.fallback_used = fallback_used
            existing.region = region
            existing.estimated_cost = estimated_cost
            existing.cost = execution_maximum_estimated_cost
            existing.input_reference = input_reference
            existing.input_digest = input_digest
            await session.commit()
            await session.refresh(existing)
            return AIRequestReservation(row_id=existing.id, created=True)

        record = AIRequestRecord(
            organisation_id=organisation_id,
            user_id=user_id,
            request_id=request_id,
            attempt_number=1,
            task=task,
            provider=provider,
            model=model,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            routing_reason=routing_reason,
            fallback_used=fallback_used,
            region=region,
            status=AIRequestStatus.RUNNING,
            estimated_cost=estimated_cost,
            # This row is the execution-level reservation while provider work
            # is in flight. It must durably carry the same bounded amount that
            # passed the budget gate; later attempt rows are already covered
            # by this reservation and start with zero cost.
            cost=execution_maximum_estimated_cost,
            input_reference=input_reference,
            input_digest=input_digest,
        )
        session.add(record)
        try:
            await session.commit()
        except IntegrityError:
            # Lost a race against a concurrent duplicate execution id: the
            # whole transaction (including the row lock) rolls back and the
            # winner's first row is the one to reuse.
            await session.rollback()
            winner = await session.scalar(
                ai_request_by_request_id_statement(organisation_id, request_id, 1)
            )
            if winner is not None:
                row_id = winner.id
                await session.commit()
                return AIRequestReservation(row_id=row_id, created=False)
            raise
        await session.refresh(record)
        return AIRequestReservation(row_id=record.id, created=True)

    async def record_attempt(
        self,
        *,
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
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
    ) -> uuid.UUID:
        """Create one further running request row for an actual dispatch.

        Called by ``AIService`` before every dispatch after the first, so the
        durable ``ai_requests`` records match v0.7 Scope §2's one-row-per-
        attempted-execution contract. No separate budget gate: the bounded
        worst case for the whole retry/repair policy was reserved by
        :meth:`reserve` before the first dispatch. Idempotent on
        ``(organisation_id, request_id, attempt_number)`` with the same
        org-scoped lookup and lost-race fallback as :meth:`reserve`.
        """
        session = self._session
        existing = await session.scalar(
            ai_request_by_request_id_statement(organisation_id, request_id, attempt_number)
        )
        if existing is not None:
            return existing.id
        record = AIRequestRecord(
            organisation_id=organisation_id,
            user_id=user_id,
            request_id=request_id,
            attempt_number=attempt_number,
            task=task,
            provider=provider,
            model=model,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            routing_reason=routing_reason,
            fallback_used=fallback_used,
            region=region,
            status=AIRequestStatus.RUNNING,
            estimated_cost=estimated_cost,
            # The first row holds the bounded execution reservation until the
            # terminal tail settles it last. Avoid double-reserving each
            # additional dispatch while still retaining its route metadata.
            cost=Decimal("0"),
            input_reference=input_reference,
            input_digest=input_digest,
        )
        session.add(record)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            winner = await session.scalar(
                ai_request_by_request_id_statement(organisation_id, request_id, attempt_number)
            )
            if winner is not None:
                return winner.id
            raise
        await session.refresh(record)
        return record.id

    async def settle(
        self,
        *,
        ai_request_id: uuid.UUID,
        organisation_id: uuid.UUID,
        task: str,
        user_id: uuid.UUID | None,
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

        The row was created at reservation; settlement writes the actual
        usage-priced cost (replacing the reservation estimate), the terminal
        status, the safe error code, and the routing decision actually used
        (a fallback may have changed provider/model after reservation). The
        lookup is org-scoped, so a caller can never settle (or audit under)
        another organisation's row (BP §9). A row that is no longer ``running``
        is an already-settled retried message: terminal states are never
        re-run, so it is a no-op (v0.7 Scope §6.5/§6.6 idempotency).

        For the successful attempt the validated output record is written in
        the **same transaction** as the row update and the audit event, so
        terminal success plus output/audit is atomic (BP §11) and a success can
        never be durable without its output. Output content is stored only
        when ``retain_content`` permits it — the safe default records
        references/digests only (v0.7 Scope §2). Any failure in this
        transaction rolls everything back, leaving the row running.
        """
        session = self._session
        record = await session.scalar(ai_request_record_statement(ai_request_id, organisation_id))
        if record is None:
            raise NotFoundError(
                code="ai_request_not_found",
                message="The AI request record could not be found.",
            )
        if record.status != AIRequestStatus.RUNNING:
            return
        record.status = AIRequestStatus(status)
        record.error_code = error_code
        record.input_tokens = usage.input_tokens
        record.output_tokens = usage.output_tokens
        record.cost = cost.amount
        record.latency_ms = latency_ms
        record.provider = routing_provider
        record.model = routing_model
        record.prompt_name = routing_prompt_name
        record.prompt_version = routing_prompt_version
        record.routing_reason = routing_reason
        record.fallback_used = fallback_used
        record.region = region
        action = (
            ACTION_AI_REQUEST_COMPLETED
            if status == AIRequestStatus.SUCCEEDED.value
            else ACTION_AI_REQUEST_FAILED
        )
        await record_event(
            session,
            organisation_id=organisation_id,
            actor_user_id=user_id,
            action=action,
            resource_type="ai_request",
            resource_id=record.request_id,
            metadata={
                "task": task,
                "provider": routing_provider,
                "model": routing_model,
                "error_code": error_code,
                "cost": str(cost.amount),
            },
        )
        if status == AIRequestStatus.SUCCEEDED.value and output is not None:
            output_json: dict[str, Any] | None = None
            if retain_content:
                if isinstance(output, BaseModel):
                    output_json = output.model_dump(mode="json")
                elif isinstance(output, str):
                    output_json = {"text": output}
                else:
                    output_json = output
            session.add(
                AIOutputRecord(
                    ai_request_id=record.id,
                    organisation_id=organisation_id,
                    output_json=output_json,
                    output_reference=output_reference,
                    output_digest=output_digest,
                    input_reference=input_reference,
                    input_digest=input_digest,
                )
            )
        await session.commit()


async def enforce_ai_retention(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run the privacy-safe retention/deletion sweep (v0.7 Scope §6.5 item 4).

    The sweep has two independent halves:

    1. **Stale-reservation reconciliation** for *every* organisation: a row
       stuck in ``running`` beyond :data:`STALE_RUNNING_THRESHOLD` is a crashed
       worker execution and is marked ``failed`` keeping its reserved cost
       (documented reservation policy: a crash never releases budget). This is
       deliberately not coupled to output-retention configuration — an
       organisation without ``retention_policy_days`` can never keep crashed
       reservations (and lost budget headroom) forever.
    2. **Output retention + scratch sweep** for every organisation with a
       ``retention_policy_days`` configured: expired ``ai_outputs`` rows are
       deleted (any scratch object they reference first), and the
       organisation's AI scratch namespace is swept page by page for orphaned
       analyse-only objects older than the policy.

    Keep-flow objects under ``organisations/{org}/documents/…`` are never
    touched — they remain owned by their feature (v0.7 Scope §6.5/§6.3). One
    ``ai.retention_deleted`` audit event per affected organisation records the
    purge with counts only; never content. Returns a summary for the job log.
    """
    now = now or datetime.now(UTC)
    # 1. Global stale reconciliation, committed up front so a crash mid-sweep
    # can never strand it; the per-organisation audit events follow below.
    stale_candidates = (
        await session.scalars(stale_running_requests_statement(now - STALE_RUNNING_THRESHOLD))
    ).all()
    stale_by_org: dict[uuid.UUID, list[AIRequestRecord]] = {}
    for record in stale_candidates:
        record.status = AIRequestStatus.FAILED
        record.error_code = ERROR_CODE_WORKER_CRASHED
        stale_by_org.setdefault(record.organisation_id, []).append(record)
    stale_reconciled = len(stale_candidates)
    await session.commit()

    # 2. Per-organisation output retention and scratch sweep.
    rows = (await session.scalars(organisations_with_retention_policy_statement())).all()
    retention_org_ids = {
        settings_row.organisation_id
        for settings_row in rows
        if settings_row.retention_policy_days is not None
    }
    organisations_purged = 0
    outputs_deleted = 0
    scratch_objects_deleted = 0
    for settings_row in rows:
        if settings_row.retention_policy_days is None:
            continue
        organisation_id = settings_row.organisation_id
        older_than = now - timedelta(days=settings_row.retention_policy_days)
        prefix = ai_scratch_prefix(organisation_id)
        org_scratch_deleted = 0

        outputs = (
            await session.scalars(expired_ai_outputs_statement(organisation_id, older_than))
        ).all()
        for output in outputs:
            reference = output.output_reference
            if reference and reference.startswith(prefix):
                try:
                    await storage.delete_object(reference)
                    org_scratch_deleted += 1
                except Exception:
                    # A storage failure must not block the record purge; the
                    # object remains in the scratch namespace for the next
                    # sweep. Never logged with the key (BP §28).
                    pass
            await session.delete(output)

        # Continuation sweep: page over the whole scratch namespace, advancing
        # past every listed key, so an expired object beyond the first page can
        # never be stranded while lexicographically earlier fresh objects keep
        # filling the page. Deleting mid-sweep is safe: the next page starts
        # strictly after the last listed key.
        start_after: str | None = None
        while True:
            page = await storage.list_objects(
                prefix, limit=SCRATCH_SWEEP_PAGE_SIZE, start_after=start_after
            )
            if not page:
                break
            for info in page:
                if info.last_modified is not None and info.last_modified < older_than:
                    await storage.delete_object(info.object_key)
                    org_scratch_deleted += 1
            start_after = page[-1].object_key

        stale = stale_by_org.get(organisation_id, [])
        if outputs or org_scratch_deleted or stale:
            await record_event(
                session,
                organisation_id=organisation_id,
                action=ACTION_AI_RETENTION_DELETED,
                resource_type="ai_output",
                resource_id=str(organisation_id),
                metadata={
                    "outputs_deleted": len(outputs),
                    "scratch_objects_deleted": org_scratch_deleted,
                    "stale_requests_reconciled": len(stale),
                    "retention_policy_days": settings_row.retention_policy_days,
                },
            )
            organisations_purged += 1
        outputs_deleted += len(outputs)
        scratch_objects_deleted += org_scratch_deleted
        await session.commit()

    # 3. Audit organisations whose only sweep action was stale reconciliation
    # (no output retention policy configured) — their event must not be tied
    # to a retention config.
    for organisation_id, stale in stale_by_org.items():
        if organisation_id in retention_org_ids:
            continue  # already audited (with counts) in the loop above
        await record_event(
            session,
            organisation_id=organisation_id,
            action=ACTION_AI_RETENTION_DELETED,
            resource_type="ai_request",
            resource_id=str(organisation_id),
            metadata={
                "outputs_deleted": 0,
                "scratch_objects_deleted": 0,
                "stale_requests_reconciled": len(stale),
                "retention_policy_days": None,
            },
        )
        organisations_purged += 1
        await session.commit()
    return {
        "organisations_purged": organisations_purged,
        "outputs_deleted": outputs_deleted,
        "scratch_objects_deleted": scratch_objects_deleted,
        "stale_requests_reconciled": stale_reconciled,
    }
