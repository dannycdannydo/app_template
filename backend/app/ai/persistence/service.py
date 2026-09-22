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

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.errors import AIUnavailableError, BudgetExceededError
from app.ai.persistence.models import (
    AIOutputRecord,
    AIRequestRecord,
    AIRequestStatus,
    AIScratchUpload,
    AIScratchUploadStatus,
    OrganisationAISettings,
)
from app.ai.persistence.port import AIRequestReservation, OrganisationAIPolicy
from app.ai.persistence.queries import (
    ai_month_spend_statement,
    ai_request_by_request_id_statement,
    ai_request_record_statement,
    all_organisation_ids_statement,
    expired_ai_outputs_statement,
    expired_execution_metadata_statement,
    expired_scratch_uploads_statement,
    organisation_ai_settings_for_update_statement,
    organisation_ai_settings_statement,
    scratch_upload_by_object_key_statement,
    scratch_upload_by_upload_id_statement,
    stale_running_requests_statement,
)
from app.ai.registry import CapabilityCostModelRegistry, ModelDefinition, load_registry_bundle
from app.ai.schemas import CostEstimate, TokenUsage
from app.ai.transfer import (
    INLINE_AGGREGATE_THRESHOLD_BYTES,
    MAX_LARGE_ATTACHMENT_BYTES,
    SCRATCH_KEY_TEMPLATE,
    TransferMode,
)
from app.core.config import AI_KNOWN_PROVIDER_IDS
from app.core.exceptions import ConflictError, ErrorDetail, NotFoundError, ValidationError
from app.db.rls import bind_organisation_context
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
from app.observability.metrics import observe_ai_budget_denial
from app.storage.base import ObjectStorage

if TYPE_CHECKING:
    from app.modules.jobs.service import JobOwnership

#: Module logger. AI persistence log lines bind ``ai_request_id``, task and the
#: existing organisation context — never prompts, provider responses,
#: attachment bytes or retained input/output content (BP §28, ADR-0017).
logger = structlog.get_logger()

#: The organisation-scoped AI scratch namespace (v0.7 Scope §6.5 item 4): temporary
#: analyse-only objects live here so the retention sweep can target them and
#: keep-flow objects under ``organisations/{org}/documents/…`` (feature-owned,
#: v0.7 Scope §6.3) are never touched by the AI layer. The template string
#: lives in ``app.ai.transfer`` so the transfer selector's transient-source
#: classification and the retention sweep share one source of truth.
SCRATCH_KEY_PREFIX = SCRATCH_KEY_TEMPLATE

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


def list_available_models() -> list[ModelDefinition]:
    """Return reviewed registry models that may be presented as policy choices.

    Deployment credentials and endpoints deliberately stay outside this
    catalogue. Request-time routing remains authoritative: a selected model's
    provider must still be enabled in deployment configuration and allowed by
    the organisation policy.
    """

    return sorted(
        (model for model in _model_registry().all() if model.available),
        key=lambda model: (model.provider, model.id),
    )


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


def _validate_transfer_policy(
    *,
    allowed_transfer_modes: list[str],
    max_large_attachment_bytes: int,
) -> None:
    """Validate the organisation transfer policy before any row is written.

    v0.8 Scope §2.2/§6.2: ``allowed_transfer_modes`` is a bounded array of
    known transfer-mode ids (default ``inline`` only, so a non-inline mode is
    never enabled by accident), and ``max_large_attachment_bytes`` tightens the
    50,000,000-byte template ceiling — it can never raise it. Unknown or
    duplicate modes and out-of-range ceilings fail fast with an actionable
    error before any write, exactly like the provider/model allowlists.
    """
    if not allowed_transfer_modes:
        raise _registry_error(
            "allowed_transfer_modes", "at least one transfer mode is required (inline)"
        )
    if len(set(allowed_transfer_modes)) != len(allowed_transfer_modes):
        raise _registry_error(
            "allowed_transfer_modes", "transfer modes must not contain duplicates"
        )
    known = {mode.value for mode in TransferMode}
    unknown = set(allowed_transfer_modes) - known
    if unknown:
        raise _registry_error(
            "allowed_transfer_modes",
            f"unknown transfer modes: {sorted(unknown)}",
        )
    if TransferMode.INLINE.value not in allowed_transfer_modes:
        # Inline is the reviewed default and must remain eligible through the
        # aggregate threshold (Scope §2.2); an allowlist without it is a
        # misconfiguration that would silently block small files.
        raise _registry_error(
            "allowed_transfer_modes",
            f"the {TransferMode.INLINE.value!r} mode is mandatory",
        )
    if not 1 <= max_large_attachment_bytes <= MAX_LARGE_ATTACHMENT_BYTES:
        raise _registry_error(
            "max_large_attachment_bytes",
            f"must be between 1 and {MAX_LARGE_ATTACHMENT_BYTES} bytes",
        )
    non_inline = [mode for mode in allowed_transfer_modes if mode != TransferMode.INLINE.value]
    if non_inline and max_large_attachment_bytes <= INLINE_AGGREGATE_THRESHOLD_BYTES:
        # Non-inline modes carry exactly one PDF above the inline aggregate
        # threshold (Scope §2.1/§5.3); a ceiling at or below 5,000,000 would
        # make every non-inline mode unreachable, so the policy fails fast
        # instead of silently routing nothing.
        raise _registry_error(
            "max_large_attachment_bytes",
            "a non-inline transfer mode requires a ceiling above the "
            f"{INLINE_AGGREGATE_THRESHOLD_BYTES}-byte inline aggregate threshold",
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
    # Plan P3 group 4a: ``organisation_ai_settings`` is default-deny under RLS.
    # Bind the new organisation's transaction-local context before the insert
    # (the organisation-creation paths run with no tenant context yet), so the
    # one-row-per-organisation invariant holds from the moment the organisation
    # exists without a bypass.
    await bind_organisation_context(session, organisation_id)
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
    # Plan P3 group 4a: ``organisation_ai_settings`` is default-deny under RLS.
    # The platform surface has no ``X-Org-Id``; bind the one organisation this
    # operation targets after the platform permission dependency already
    # validated the caller (an explicit per-organisation platform path, never a
    # bypass — ADR-0022 decision 4).
    await bind_organisation_context(session, organisation_id)
    rows = (await session.scalars(organisation_ai_settings_statement(organisation_id))).all()
    settings_row = next(
        (row for row in rows if row.organisation_id == organisation_id),
        None,
    )
    if settings_row is None:
        settings_row = await create_default_settings(session, organisation_id=organisation_id)
        await session.commit()
        # The commit cleared the transaction-local context; rebind the tenant
        # before refreshing the protected row (plan P3 group 4a).
        await bind_organisation_context(session, organisation_id)
        await session.refresh(settings_row)
    return settings_row


async def update_ai_settings(
    session: AsyncSession,
    *,
    actor: User,
    organisation_id: uuid.UUID,
    expected_version: int = 1,
    enabled: bool,
    allowed_provider_ids: list[str],
    allowed_model_ids: list[str],
    provider_override: str | None,
    model_override: str | None,
    monthly_budget: Decimal | None,
    retention_policy_days: int | None,
    allowed_transfer_modes: list[str] | None = None,
    max_large_attachment_bytes: int | None = None,
) -> OrganisationAISettings:
    """Replace one organisation's AI policy and audit the change.

    The registry validation runs before any write, so an invalid policy never
    reaches the row. The settings row is locked before comparing the client's
    version; a stale full replacement returns 409 instead of silently
    overwriting another administrator's update (BP §10). The audit event
    commits in the same transaction. The row is created when missing (the
    platform can enable AI for an organisation whose row predates this
    release), and that defensive default begins at version 1.

    v0.8 Scope §2.2/§6.2: ``allowed_transfer_modes`` and
    ``max_large_attachment_bytes`` carry the organisation's transfer policy.
    ``None`` keeps the current row values (so existing callers and the
    default-off row keep working unchanged); explicit lists/values replace the
    policy. ``allowed_transfer_modes`` must always contain ``inline`` and
    ``max_large_attachment_bytes`` can only tighten the template ceiling.
    """
    await _get_organisation_or_404(session, organisation_id)
    # Plan P3 group 4a: bind the target organisation before reading/writing its
    # protected settings row (the platform plane carries no ``X-Org-Id``; the
    # platform permission dependency has already validated the caller).
    await bind_organisation_context(session, organisation_id)
    _validate_policy_identifiers(
        allowed_provider_ids=allowed_provider_ids,
        allowed_model_ids=allowed_model_ids,
        provider_override=provider_override,
        model_override=model_override,
    )
    rows = (
        await session.scalars(organisation_ai_settings_for_update_statement(organisation_id))
    ).all()
    settings_row = next(
        (row for row in rows if row.organisation_id == organisation_id),
        None,
    )
    if settings_row is None:
        settings_row = OrganisationAISettings(organisation_id=organisation_id, version=1)
        session.add(settings_row)
    if settings_row.version != expected_version:
        raise ConflictError(
            code="ai_settings_version_conflict",
            message="The AI settings were changed by another administrator.",
        )
    effective_transfer_modes = (
        allowed_transfer_modes
        if allowed_transfer_modes is not None
        else list(settings_row.allowed_transfer_modes)
    )
    effective_max_large_bytes = (
        max_large_attachment_bytes
        if max_large_attachment_bytes is not None
        else settings_row.max_large_attachment_bytes
    )
    _validate_transfer_policy(
        allowed_transfer_modes=effective_transfer_modes,
        max_large_attachment_bytes=effective_max_large_bytes,
    )
    settings_row.enabled = enabled
    settings_row.allowed_provider_ids = allowed_provider_ids
    settings_row.allowed_model_ids = allowed_model_ids
    settings_row.provider_override = provider_override
    settings_row.model_override = model_override
    settings_row.monthly_budget = monthly_budget
    settings_row.retention_policy_days = retention_policy_days
    settings_row.allowed_transfer_modes = effective_transfer_modes
    settings_row.max_large_attachment_bytes = effective_max_large_bytes
    settings_row.updated_by_user_id = actor.id
    settings_row.version += 1
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
            "allowed_transfer_modes": effective_transfer_modes,
            "max_large_attachment_bytes": effective_max_large_bytes,
            "version": settings_row.version,
        },
    )
    await session.commit()
    # The commit cleared the transaction-local context; rebind the tenant
    # before refreshing the protected row (plan P3 group 4a).
    await bind_organisation_context(session, organisation_id)
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
        allowed_transfer_modes=[TransferMode(mode) for mode in settings_row.allowed_transfer_modes],
        max_large_attachment_bytes=settings_row.max_large_attachment_bytes,
    )


class AIPersistencePortImpl:
    """Session-bound :class:`AIPersistencePort` used by ``AIService``.

    Construct one per execution with the caller's session and pass it to
    :meth:`app.ai.service.AIService.execute` — the §6.6 demo service and the
    ``ai.execute`` job both follow this pattern.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        ownership: JobOwnership | None = None,
    ) -> None:
        self._session = session
        self._ownership = ownership

    async def _verify_ownership(self) -> None:
        """Re-lock and re-verify the owning durable job before a mutation.

        The durable ``ai.execute`` worker passes its captured ownership; the
        synchronous demo path passes ``None`` and skips the guard. Imported
        locally to avoid the models import cycle (``app.db.base`` -> AI ->
        audit), the same reason other AI modules defer job imports.
        """
        if self._ownership is None:
            return
        from app.modules.jobs import service as jobs_service

        await jobs_service.verify_ownership(self._session, self._ownership)

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
        await self._verify_ownership()
        session = self._session
        # Bind the validated organisation before touching the protected
        # ``ai_requests``/``ai_outputs`` tables. The policy is default-deny and
        # transaction-local, so a worker or API path that committed earlier must
        # rebind here rather than rely on stale context (plan P3 group 3,
        # ADR-0022 decisions 4 and 9).
        await bind_organisation_context(session, organisation_id)
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
                observe_ai_budget_denial(task=task)
                logger.warning(
                    "ai.budget_denied",
                    ai_request_id=request_id,
                    task=task,
                    organisation_id=str(organisation_id),
                    estimated_cost=str(execution_maximum_estimated_cost),
                    spent=str(spent),
                    monthly_budget=str(settings_row.monthly_budget),
                )
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
            # The commit ended the transaction-local context; rebind before the
            # post-commit refresh of the protected row.
            await bind_organisation_context(session, organisation_id)
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
            await bind_organisation_context(session, organisation_id)
            winner = await session.scalar(
                ai_request_by_request_id_statement(organisation_id, request_id, 1)
            )
            if winner is not None:
                row_id = winner.id
                await session.commit()
                return AIRequestReservation(row_id=row_id, created=False)
            raise
        await bind_organisation_context(session, organisation_id)
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
        await self._verify_ownership()
        session = self._session
        await bind_organisation_context(session, organisation_id)
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
            await bind_organisation_context(session, organisation_id)
            winner = await session.scalar(
                ai_request_by_request_id_statement(organisation_id, request_id, attempt_number)
            )
            if winner is not None:
                return winner.id
            raise
        await bind_organisation_context(session, organisation_id)
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
        await self._verify_ownership()
        session = self._session
        await bind_organisation_context(session, organisation_id)
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


# --- Durable AI scratch intents (plan P6) -----------------------------------


async def is_ai_enabled(session: AsyncSession, *, organisation_id: uuid.UUID) -> bool:
    """Return whether the organisation's AI policy is enabled (fail-safe off)."""
    settings_row = await session.scalar(organisation_ai_settings_statement(organisation_id))
    return settings_row is not None and settings_row.enabled


async def scratch_retention_days(
    session: AsyncSession, *, organisation_id: uuid.UUID
) -> int | None:
    """Return the organisation's optional scratch retention policy, if any."""
    settings_row = await session.scalar(organisation_ai_settings_statement(organisation_id))
    if settings_row is None:
        return None
    return settings_row.retention_policy_days


async def create_scratch_upload(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    upload_id: uuid.UUID,
    object_key: str,
    content_type: str,
    size_bytes: int,
    expires_at: datetime,
) -> AIScratchUpload:
    """Persist one scratch-upload intent (caller owns the commit).

    Plan P6: the intent is created only after the organisation's AI policy has
    been confirmed, so obtaining a scratch PUT capability cannot bypass
    default-deny AI enablement. The bounded ``expires_at`` is computed by the
    caller from the global ceiling and any tighter organisation policy. The
    organisation context is bound so the insert satisfies the default-deny
    ``ai_scratch_uploads`` policy (plan P3 group 3).
    """
    await bind_organisation_context(session, organisation_id)
    row = AIScratchUpload(
        organisation_id=organisation_id,
        upload_id=upload_id,
        object_key=object_key,
        content_type=content_type,
        size_bytes=size_bytes,
        status=AIScratchUploadStatus.PENDING,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def get_scratch_upload(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> AIScratchUpload | None:
    """Return the org-scoped scratch intent for one caller-visible upload id."""
    await bind_organisation_context(session, organisation_id)
    return await session.scalar(scratch_upload_by_upload_id_statement(organisation_id, upload_id))


async def complete_scratch_upload(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    upload_id: uuid.UUID,
    now: datetime | None = None,
) -> AIScratchUpload | None:
    """Mark a pending scratch intent ``ready``; idempotent on replay.

    The row is locked ``FOR UPDATE`` so a concurrent completion cannot promote
    it twice. Returns ``None`` when the intent is not ``pending`` (already
    terminal, deleted) or the bounded lifetime has passed, so a caller can
    never resurrect a terminal or expired intent. A still-live ``ready`` row
    is returned for an idempotent replay; an expired ``ready`` row is not.
    """
    completed_at = now or datetime.now(UTC)
    await bind_organisation_context(session, organisation_id)
    row = await session.scalar(
        scratch_upload_by_upload_id_statement(organisation_id, upload_id).with_for_update()
    )
    if row is None:
        return None
    if row.status == AIScratchUploadStatus.READY:
        if row.expires_at <= completed_at:
            return None
        return row
    if row.status != AIScratchUploadStatus.PENDING:
        return None
    if row.expires_at <= completed_at:
        # Completion after the bounded lifetime is refused; the global expiry
        # sweep owns the terminal transition (the bytes are already
        # unauthorised by ``authorize_scratch_object``'s expiry check).
        return None
    row.status = AIScratchUploadStatus.READY
    row.completed_at = completed_at
    await session.flush()
    return row


async def authorize_scratch_object(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    object_key: str,
    now: datetime | None = None,
) -> AIScratchUpload | None:
    """Return the live scratch intent that authorises one scratch object key.

    Plan P6: a scratch key is trusted only when a durable intent exists, is
    ``ready`` and has not expired. ``None`` means the reference must fail
    closed — an unknown, pending, expired or cross-organisation scratch key is
    never treated as an authorised AI source.
    """
    await bind_organisation_context(session, organisation_id)
    row = await session.scalar(scratch_upload_by_object_key_statement(organisation_id, object_key))
    if row is None or row.status != AIScratchUploadStatus.READY:
        return None
    if row.expires_at <= (now or datetime.now(UTC)):
        return None
    return row


#: Bounded batch size for the per-organisation expired-scratch sweep (plan P6).
SCRATCH_EXPIRY_BATCH_SIZE = 200


async def _expire_scratch_uploads_for_organisation(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    organisation_id: uuid.UUID,
    expired_before: datetime,
) -> int:
    """Expire one organisation's scratch intents and delete their objects.

    The sweep runs for every organisation; this helper handles exactly one so
    both the standalone scratch sweep and the retention sweep share the same
    per-tenant logic. It binds the organisation's transaction-local context
    before every batch, because the ``ai_scratch_uploads`` policy is
    default-deny and the batch commit ends the context (plan P3 group 3,
    ADR-0022 decision 4). Object deletion is best-effort (a provider failure
    leaves the object for the object-store lifecycle backstop) while the row
    still moves to a terminal ``expired`` state.
    """
    expired = 0
    while True:
        await bind_organisation_context(session, organisation_id)
        batch = (
            await session.scalars(
                expired_scratch_uploads_statement(
                    organisation_id,
                    expired_before=expired_before,
                    batch_size=SCRATCH_EXPIRY_BATCH_SIZE,
                )
            )
        ).all()
        if not batch:
            break
        for row in batch:
            # Best-effort: never log the key (BP §28); the object-store
            # lifecycle rule is the asynchronous backstop.
            with contextlib.suppress(Exception):
                await storage.delete_object(row.object_key)
            row.status = AIScratchUploadStatus.EXPIRED
            expired += 1
        await session.commit()
    return expired


async def expire_scratch_uploads(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    now: datetime | None = None,
) -> int:
    """Delete every expired scratch object and mark its intent expired.

    Plan P6: runs for every organisation, independent of any per-organisation
    retention policy, so the global maximum lifetime is enforced even for
    organisations with no retention policy configured. It enumerates the
    global, unprotected ``organisations`` table and binds each tenant before
    touching its protected ``ai_scratch_uploads`` rows, because that table is
    default-deny under RLS (plan P3 group 3, ADR-0022 decision 4: no universal
    bypass). Returns the number of intents expired.
    """
    expired_at = now or datetime.now(UTC)
    organisation_ids = (await session.scalars(all_organisation_ids_statement())).all()
    expired = 0
    for organisation_id in organisation_ids:
        expired += await _expire_scratch_uploads_for_organisation(
            session,
            storage,
            organisation_id=organisation_id,
            expired_before=expired_at,
        )
    return expired


async def enforce_ai_retention(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run the privacy-safe retention/deletion sweep (v0.7 Scope §6.5 item 4).

    The sweep has two independent halves:

    1. **Transient execution cleanup and stale-reservation reconciliation** for
       *every* organisation: expired durable task variables are cleared (a
       still-queued request is failed), and a row
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

    Plan P3 group 3 / ADR-0022 decision 4: the ``ai_requests``, ``ai_outputs``
    and ``ai_scratch_uploads`` policies are default-deny, so a global
    cross-tenant scan no longer works. The sweep enumerates the global,
    unprotected ``organisations`` table and binds each organisation's
    transaction-local context before touching its protected AI rows; it never
    uses a bypass. Each organisation's work commits before the next tenant is
    bound, so context never leaks across tenants.

    Keep-flow objects under ``organisations/{org}/documents/…`` are never
    touched — they remain owned by their feature (v0.7 Scope §6.5/§6.3). One
    ``ai.retention_deleted`` audit event per affected organisation records the
    purge with counts only; never content. Returns a summary for the job log.
    """
    now = now or datetime.now(UTC)
    stale_cutoff = now - STALE_RUNNING_THRESHOLD
    organisation_ids = (await session.scalars(all_organisation_ids_statement())).all()

    organisations_purged = 0
    outputs_deleted = 0
    scratch_objects_deleted = 0
    scratch_intents_expired = 0
    stale_reconciled = 0
    execution_metadata_cleared = 0

    for organisation_id in organisation_ids:
        # Plan P3 group 4a: ``organisation_ai_settings`` is now RLS-protected
        # too, so bind the tenant before this settings read exactly as for the
        # protected reads below.
        await bind_organisation_context(session, organisation_id)
        settings_row = await session.scalar(organisation_ai_settings_statement(organisation_id))
        retention_days = settings_row.retention_policy_days if settings_row is not None else None

        # 1. Stale-reservation reconciliation for this organisation, committed
        # up front so a crash mid-sweep can never strand it.
        stale_candidates = (
            await session.scalars(stale_running_requests_statement(organisation_id, stale_cutoff))
        ).all()
        for record in stale_candidates:
            record.status = AIRequestStatus.FAILED
            record.error_code = ERROR_CODE_WORKER_CRASHED
        stale_reconciled += len(stale_candidates)
        expired_metadata = (
            await session.scalars(expired_execution_metadata_statement(organisation_id, now))
        ).all()
        for record in expired_metadata:
            record.execution_metadata = None
            record.execution_metadata_expires_at = None
            if record.status == AIRequestStatus.QUEUED:
                record.status = AIRequestStatus.FAILED
                record.error_code = ERROR_CODE_WORKER_CRASHED
        execution_metadata_cleared += len(expired_metadata)
        await session.commit()

        # 1b. Scratch-intent expiry for this organisation (plan P6):
        # independent of any per-organisation retention policy, so every
        # scratch object has a bounded maximum lifetime.
        scratch_intents_expired += await _expire_scratch_uploads_for_organisation(
            session,
            storage,
            organisation_id=organisation_id,
            expired_before=now,
        )

        # 2. Output retention and scratch-namespace sweep for this organisation.
        outputs: list[AIOutputRecord] = []
        org_scratch_deleted = 0
        if retention_days is not None:
            # The scratch helper committed; rebind before the protected read.
            await bind_organisation_context(session, organisation_id)
            older_than = now - timedelta(days=retention_days)
            prefix = ai_scratch_prefix(organisation_id)

            outputs = list(
                (
                    await session.scalars(expired_ai_outputs_statement(organisation_id, older_than))
                ).all()
            )
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

            # Continuation sweep: page over the whole scratch namespace,
            # advancing past every listed key, so an expired object beyond the
            # first page can never be stranded while lexicographically earlier
            # fresh objects keep filling the page. Deleting mid-sweep is safe:
            # the next page starts strictly after the last listed key.
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

        # 3. One audit event per affected organisation; its resource type and
        # metadata reflect whether the organisation configured retention.
        if retention_days is not None:
            if outputs or org_scratch_deleted or stale_candidates or expired_metadata:
                await record_event(
                    session,
                    organisation_id=organisation_id,
                    action=ACTION_AI_RETENTION_DELETED,
                    resource_type="ai_output",
                    resource_id=str(organisation_id),
                    metadata={
                        "outputs_deleted": len(outputs),
                        "scratch_objects_deleted": org_scratch_deleted,
                        "stale_requests_reconciled": len(stale_candidates),
                        "execution_metadata_cleared": len(expired_metadata),
                        "retention_policy_days": retention_days,
                    },
                )
                organisations_purged += 1
        elif stale_candidates or expired_metadata:
            await record_event(
                session,
                organisation_id=organisation_id,
                action=ACTION_AI_RETENTION_DELETED,
                resource_type="ai_request",
                resource_id=str(organisation_id),
                metadata={
                    "outputs_deleted": 0,
                    "scratch_objects_deleted": 0,
                    "stale_requests_reconciled": len(stale_candidates),
                    "execution_metadata_cleared": len(expired_metadata),
                    "retention_policy_days": None,
                },
            )
            organisations_purged += 1
        outputs_deleted += len(outputs)
        scratch_objects_deleted += org_scratch_deleted
        await session.commit()

    return {
        "organisations_purged": organisations_purged,
        "outputs_deleted": outputs_deleted,
        "scratch_objects_deleted": scratch_objects_deleted,
        "scratch_intents_expired": scratch_intents_expired,
        "stale_requests_reconciled": stale_reconciled,
        "execution_metadata_cleared": execution_metadata_cleared,
    }
