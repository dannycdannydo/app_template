"""AIService — the only application-facing entry point to the AI layer.

v0.7 Scope §6.1/§6.4, ADR-0017: application code calls
``AIService.execute(request: AIRequest) -> AIResult`` and names a task, never
a provider or model. The service resolves the task → prompt → model through
the registry interfaces, renders the prompt with a safe allowlisted renderer,
applies the optional redaction hook, resolves private storage references into
bounded attachments at the service boundary, dispatches through the provider
boundary, validates structured output against the task's Pydantic schema, and
returns a result with usage/cost/routing metadata. Provider SDKs never appear
here (BP §33, ADR-0017).

v0.7 Scope §6.4 (structured outputs, retry and safety controls): every result is
validated against the declared Pydantic model before it is returned. The
service supplies the JSON Schema it generated from that model to the adapter,
which requests native structured output where it truthfully supports it
(OpenAI ``json_schema``, Vertex ``responseJsonSchema``, Azure by pinned
api-version) and otherwise falls back to the documented JSON-mode prompt
contract. Malformed output triggers at most one bounded repair request, then
consumes bounded task retries; a transient provider failure inside the repair
consumes the same bounded retry budget. Transient provider failures retry
within the task's bounded ``max_attempts`` (using the router's region-safe
cross-provider fallback only when the task allows it); permanent
validation/policy failures never retry. Usage/cost aggregate every actual
attempt. Unvalidated structured data is never returned as success.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from app.ai.attachments import Attachment, validate_attachment_set
from app.ai.errors import (
    AIError,
    AIInputValidationError,
    AIRequestReplayError,
    AIUnavailableError,
    ModelNotAvailableError,
    OutputSchemaError,
    OutputValidationError,
    PromptNotFoundError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    TaskNotFoundError,
)
from app.ai.persistence.port import AIPersistencePort, OrganisationAIPolicy
from app.ai.providers.base import LLMProvider, ProviderRequest
from app.ai.registry import (
    ModelDefinition,
    ModelRegistry,
    PromptDefinition,
    PromptRegistry,
    RegistryValidationError,
    RoutingDecision,
    TaskDefinition,
    TaskRegistry,
    estimate_maximum_cost,
    estimate_tokens,
    resolve_output_schema,
)
from app.ai.schemas import AIRequest, AIResult, CostEstimate, RoutingMetadata, TokenUsage
from app.ai.storage_resolver import AttachmentResolutionContext, AttachmentResolver

SchemaResolver = Callable[[str], type[BaseModel]]


class RepairNotPossibleError(AIError):
    """A repair request cannot be dispatched within the task/model bounds.

    Raised when the enlarged repair prompt would exceed the task's context or
    cost ceilings (v0.7 Scope §6.4/§6.5). Terminal: retrying the identical
    malformed cycle cannot shrink the repair prompt, so no bounded task retry
    is attempted. Carries a safe message that never echoes provider output.
    """

    error_code = "repair_not_possible"


#: Optional pre-dispatch redaction hook (v0.7 Scope §6.4): applied to the request's
#: text and message content before the prompt is rendered, so sensitive input
#: never reaches the provider or the rendered prompt. Default is identity.
Redactor = Callable[[str], str]

#: Bounded context for one repair request (v0.7 Scope §6.4): the previous invalid
#: provider output is truncated before it is sent back, so a repair can never
#: amplify a response into an unbounded second request.
MAX_REPAIR_CONTEXT_LENGTH = 8 * 1024

_REPAIR_INSTRUCTION = (
    "\n\nYour previous response was not valid structured output for the declared "
    "contract. Return ONLY a single corrected JSON object that validates against "
    "that contract. Do not include explanations, markdown fences or extra text.\n"
    "Previous response:\n{previous}"
)


def import_schema(path: str) -> type[BaseModel]:
    """Resolve a dotted import path to a Pydantic model class.

    The task registry's ``output_schema`` (v0.7 Scope §6.2) and the request's
    optional override are dotted paths; this is the default resolver. Raises
    :class:`OutputSchemaError` when the path is unknown or does not name a
    Pydantic model — fail fast, never fall back to unvalidated data.
    """

    try:
        return resolve_output_schema(path)
    except RegistryValidationError as exc:
        raise OutputSchemaError(str(exc)) from exc


def _merge_restriction_list(
    caller: list[str] | None, organisation: list[str] | None
) -> list[str] | None:
    """Merge the caller's restrictions with the organisation's allowlist.

    ``None`` means "no restriction from this source"; two non-``None`` lists
    intersect, so both the feature-level and the organisation-level constraint
    must hold. An empty intersection simply means no model is eligible, which
    the router surfaces as the safe :class:`ModelNotAvailableError`.
    """
    if caller is None:
        return organisation
    if organisation is None:
        return caller
    return [item for item in caller if item in organisation]


def _merge_organisation_policy(
    policy: OrganisationAIPolicy,
    allowed_providers: list[str] | None,
    allowed_model_ids: list[str] | None,
    model_override: str | None,
) -> tuple[list[str] | None, list[str] | None, str | None]:
    """Merge the organisation policy with the caller's routing restrictions.

    The organisation's allowlists apply on top of the caller's (intersection);
    the organisation's overrides are folded into the caller's (a forced
    provider narrows the provider set, a forced model is the effective
    ``model_override``). The organisation is authoritative: its override wins
    over a caller-supplied one, and its allowlist can only ever restrict.
    """
    providers = _merge_restriction_list(allowed_providers, policy.allowed_provider_ids or None)
    if policy.provider_override is not None:
        providers = _merge_restriction_list([policy.provider_override], providers)
    models = _merge_restriction_list(allowed_model_ids, policy.allowed_model_ids or None)
    override = policy.model_override if policy.model_override is not None else model_override
    return providers, models, override


def _attachment_set_digest(attachments: Sequence[Attachment]) -> str | None:
    """SHA-256 of the resolved attachment set, or ``None`` without attachments.

    The digest is the privacy-safe identity of the source input recorded on
    the durable rows (v0.7 Scope §6.5) — the bytes themselves are never persisted
    (BP §28, ADR-0017). The combined content is bounded by the 10 MB template
    ceiling, so hashing it here is cheap and deterministic.
    """
    if not attachments:
        return None
    return hashlib.sha256(b"".join(attachment.content for attachment in attachments)).hexdigest()


def _validated_output_digest(output: Any) -> str:
    """Return a stable SHA-256 digest without retaining validated content."""

    if isinstance(output, BaseModel):
        value: Any = output.model_dump(mode="json")
    elif isinstance(output, str):
        value = {"text": output}
    else:
        value = output
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class _PendingAttempt:
    """Bookkeeping for one reserved attempt row inside :meth:`AIService.execute`.

    One instance exists per attempted provider execution: the durable row id,
    the routing decision actually used (a fallback may change model/provider
    between attempts), the adapter region it ran in, the billed usage, and the
    safe error code when that attempt failed. The terminal tail settles each
    attempt's row with its own actuals priced with its own model's rates.
    """

    row_id: UUID | None
    model: ModelDefinition
    decision: RoutingDecision
    region: str
    usage: TokenUsage
    latency_ms: int = 0
    error_code: str | None = None


class AIService:
    """Provider-neutral executor for one AI task.

    Constructed with the registry interfaces and the configured provider
    adapter(s); the wiring is owned by the application (v0.7 Scope §6.3
    factory, §6.5 organisation settings). ``provider`` is the single-provider
    shorthand; ``providers`` maps provider id → adapter for deployments that
    enable more than one provider, which is what makes the router's reviewed
    cross-provider fallback actually executable (v0.7 Scope §6.2/§6.4). The
    fake provider is the default adapter under test.

    ``attachment_resolver`` (v0.7 Scope §6.4, ADR-0017) resolves a request's
    private ``storage_reference`` into bounded in-memory attachments at the
    service boundary; ``None`` rejects storage-referenced requests with a
    clear error. ``redactor`` is applied to text/message content before
    dispatch.

    Enforcement is fail closed (v0.7 Scope §2/§6.5): the documented
    application-facing entry point ``execute`` requires the persistence/policy
    port, and execution without it raises unless the explicit test-only
    ``allow_unmanaged_execution`` seam is opted into at construction. No
    caller can accidentally dispatch with no enabled-state, allowlist, budget,
    persistence or audit enforcement.
    """

    def __init__(
        self,
        *,
        task_registry: TaskRegistry,
        prompt_registry: PromptRegistry,
        model_registry: ModelRegistry,
        provider: LLMProvider | None = None,
        providers: Mapping[str, LLMProvider] | None = None,
        schema_resolver: SchemaResolver = import_schema,
        attachment_resolver: AttachmentResolver | None = None,
        redactor: Redactor | None = None,
        allow_unmanaged_execution: bool = False,
    ) -> None:
        if provider is not None:
            if providers is not None:
                raise ValueError("AIService accepts either provider or providers, not both")
            self._providers: dict[str, LLMProvider] = {provider.provider_id: provider}
        elif providers is not None:
            self._providers = dict(providers)
        else:
            raise ValueError("AIService requires at least one configured provider")
        if not self._providers:
            raise ValueError("AIService requires at least one configured provider")
        self._task_registry = task_registry
        self._prompt_registry = prompt_registry
        self._model_registry = model_registry
        self._schema_resolver = schema_resolver
        self._attachment_resolver = attachment_resolver
        self._redactor = redactor
        # Test-only seam (v0.7 Scope §6.5): ``execute`` without a recorder
        # port is refused by default so the supported entry point can never
        # bypass organisation enforcement; deterministic service tests that
        # exercise routing/dispatch in isolation opt into unmanaged execution.
        self._allow_unmanaged_execution = allow_unmanaged_execution

    def _redact(self, text: str) -> str:
        return self._redactor(text) if self._redactor is not None else text

    def _region_of_provider(self) -> dict[str, str]:
        """Provider id → configured region for every configured adapter.

        The router needs the *complete* map so a reviewed fallback can prove
        it never implicitly moves a request across regions (v0.7 Scope §6.3
        regional amendment, ADR-0017): a fallback candidate in another region
        is excluded, and an omitted region cannot be used to bypass the rule.
        """
        return {provider_id: provider.region for provider_id, provider in self._providers.items()}

    async def execute(
        self,
        request: AIRequest,
        *,
        recorder: AIPersistencePort | None = None,
        request_id: str | None = None,
        allowed_providers: list[str] | None = None,
        allowed_model_ids: list[str] | None = None,
        model_override: str | None = None,
        maximum_estimated_cost: Decimal | None = None,
        attachments: Sequence[Attachment] | None = None,
    ) -> AIResult:
        """Execute one task request and return a validated result.

        ``recorder`` (v0.7 Scope §6.5) is the mandatory persistence/policy
        port: the organisation's policy is loaded and enforced *here* — AI
        disabled is rejected, the organisation's allowed providers/models and
        overrides are merged with the caller's restrictions, budget is gated
        before dispatch and settled with actuals afterwards, one
        ``ai_requests`` row per attempted provider execution is persisted
        (v0.7 Scope §2), the validated output record and audit events are
        written, and output content is retained only when the task-level
        opt-in and the organisation retention policy both permit it. Execution
        without a port raises unless the service was constructed with the
        explicit test-only ``allow_unmanaged_execution`` seam.

        ``request_id`` optionally pins the caller-visible AI request id so a
        durable job can re-execute idempotently (the §6.6 ``ai.execute`` job
        passes the id it created before enqueueing); ``None`` generates a new
        id per execution. It is the execution id shared by every attempt row.

        ``allowed_providers`` is the organisation-level provider allowlist
        enforced by the model registry/router (v0.7 Scope §6.5); ``None``
        means no organisation restriction.

        ``attachments`` are bounded inline attachments either passed by the
        caller (already resolved at the service/job boundary) or resolved by
        this service from the request's private ``storage_reference`` through
        the configured ``attachment_resolver`` (v0.7 Scope §6.4, ADR-0017).
        They are validated against the template limits (5 MB per file / 10 MB
        combined), the router only selects models declaring the ``documents``
        capability with sufficient per-model ceilings, image attachments
        additionally require the model's ``vision`` capability, and the
        configured adapter must declare document support — every incompatible
        modality, MIME type and size combination fails before provider
        dispatch.

        v0.7 Scope §6.4 safety controls: transient provider failures
        (unavailable, rate limited, timeout) retry within the task's
        ``retry_policy`` ``max_attempts``, re-routing through the router's
        region-safe fallback only when the task's ``fallback_policy`` allows
        it; malformed provider output triggers at most ``repair_attempts``
        (≤ 1) bounded repair request, then consumes one bounded task retry per
        malformed output, and a transient failure inside the repair itself
        consumes the same bounded retry budget instead of escaping; permanent
        validation/policy failures never retry. Every attempt's usage/cost is
        accounted and priced with that attempt's own model, so the returned
        result prices the real traffic attempt by attempt. The budget gate
        reserves the bounded worst case for the whole retry/repair policy
        before the first dispatch. Unvalidated structured data is never
        returned.

        Raises an :class:`~app.ai.errors.AIError` subclass with a safe code on
        every failure.
        """

        if recorder is None and not self._allow_unmanaged_execution:
            raise RuntimeError(
                "AIService.execute requires a persistence/policy port; refusing to "
                "dispatch without organisation enforcement (v0.7 Scope §6.5)"
            )

        execution_request_id = request_id or uuid4().hex
        task = self._resolve_task(request.task)
        prompt = self._resolve_prompt(task.prompt_name, task.prompt_version)
        # Input-form validation first (v0.7 Scope §6.4): a task whose prompt
        # declares ``text`` must receive text input — a storage reference can
        # never silently satisfy it — and vice versa.
        self._validate_input_form(prompt, request)
        resolved_attachments = await self._resolve_attachments(request, attachments)
        rendered = self._render_prompt(prompt, request, resolved_attachments)
        # The effective output schema is resolved exactly once: a request
        # override wins, and an empty-string override is treated as "no
        # override" so the provider request and output validation can never
        # disagree (v0.7 Scope §6.1). Its JSON Schema (v0.7 Scope §6.4) is
        # generated before dispatch so a bad schema fails fast and every
        # adapter can request native structured output.
        effective_output_schema = request.output_schema or task.output_schema
        output_json_schema = self._output_json_schema(effective_output_schema)
        configured_max_tokens = task.parameter_defaults.get("max_tokens")
        configured_temperature = task.parameter_defaults.get("temperature")
        estimated_input_tokens = estimate_tokens(rendered)
        max_attempts = task.retry_policy.max_attempts
        repair_budget = task.retry_policy.repair_attempts
        excluded_model_ids: list[str] = []
        last_transient: ProviderError | None = None

        # Organisation controls (v0.7 Scope §6.5): the organisation's
        # effective policy is enforced *here* — never in a router or UI
        # (BP §27) — and its restrictions are merged with the caller's before
        # routing. The task-level retention opt-in only takes effect together
        # with a configured organisation retention policy (v0.7 Scope §2).
        retain_output_content = False
        if recorder is not None:
            policy = await recorder.load_policy(organisation_id=request.organisation_id)
            if not policy.enabled:
                raise AIUnavailableError("AI is not enabled for this organisation")
            allowed_providers, allowed_model_ids, model_override = _merge_organisation_policy(
                policy, allowed_providers, allowed_model_ids, model_override
            )
            retain_output_content = (
                task.retains_output_content and policy.retention_policy_days is not None
            )

        # Per-attempt accounting state (v0.7 Scope §2/§6.5): every attempted
        # provider execution gets its own running row (the first via reserve,
        # later attempts via record_attempt) carrying that attempt's routing
        # decision and estimate; settlement prices each row with its own
        # model's rates. ``pending_attempts`` tracks the rows so the terminal
        # tail can settle every one of them with actuals and safe error codes.
        pending_attempts: list[_PendingAttempt] = []
        dispatch_count = 0
        failure: AIError | None = None
        result: AIResult | None = None
        winning_attempt: _PendingAttempt | None = None
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    decision = self._model_registry.route(
                        task,
                        allowed_providers=allowed_providers,
                        allowed_model_ids=allowed_model_ids,
                        model_override=model_override,
                        estimated_input_tokens=estimated_input_tokens,
                        maximum_estimated_cost=maximum_estimated_cost,
                        attachments=resolved_attachments,
                        excluded_model_ids=excluded_model_ids,
                        # Every configured provider's region is declared so a
                        # reviewed fallback can never implicitly change region
                        # (v0.7 Scope §6.3 regional amendment, ADR-0017).
                        region_of_provider=self._region_of_provider(),
                    )
                except (KeyError, ValueError) as exc:
                    # No eligible model at all, or — during an allowed fallback —
                    # no eligible alternative remains after excluding the failed
                    # model(s). The latter must surface the original transient
                    # failure (retryable by the caller/job) instead of converting
                    # it into a permanent ModelNotAvailableError: the in-process
                    # routing budget is exhausted, not the model itself.
                    if excluded_model_ids and last_transient is not None:
                        raise last_transient from exc
                    raise ModelNotAvailableError(f"no model satisfies task {task.name}") from exc
                model = decision.model
                try:
                    provider = self._providers[model.provider]
                except KeyError as exc:
                    raise ModelNotAvailableError(
                        "resolved model provider is not configured for this service"
                    ) from exc
                if resolved_attachments and not provider.supports_documents:
                    raise ModelNotAvailableError(
                        "resolved model provider does not support document attachments"
                    )
                provider_request = ProviderRequest(
                    task=task.name,
                    model=model.model,
                    prompt=rendered,
                    output_schema=effective_output_schema,
                    output_json_schema=output_json_schema,
                    max_tokens=(
                        configured_max_tokens if isinstance(configured_max_tokens, int) else None
                    ),
                    temperature=(
                        float(configured_temperature)
                        if isinstance(configured_temperature, (int, float))
                        else None
                    ),
                    metadata=request.metadata,
                    attachments=resolved_attachments,
                )
                # One durable row per actual dispatch (v0.7 Scope §2). The
                # first attempt gates the execution's bounded worst-case budget
                # under the settings-row lock (idempotent on the execution id);
                # every further attempt gets its own row with no separate gate
                # (the bounded worst case was already reserved). Rows are
                # created before dispatch so a crash mid-execution is
                # reconcilable; failures before the first dispatch reserve
                # nothing.
                dispatch_count += 1
                pending_attempt = _PendingAttempt(
                    row_id=None,
                    model=model,
                    decision=decision,
                    region=provider.region,
                    usage=TokenUsage(input_tokens=0, output_tokens=0),
                )
                if recorder is not None:
                    if dispatch_count == 1:
                        # The bounded worst case for the whole retry/repair
                        # policy, so a retry-heavy execution can never
                        # collectively overrun the budget after passing a
                        # per-attempt check (v0.7 Scope §6.5).
                        bounded_estimate = Decimal(max_attempts + repair_budget) * decision.estimated_max_cost
                        reservation = await recorder.reserve(
                            organisation_id=request.organisation_id,
                            user_id=request.user_id,
                            request_id=execution_request_id,
                            task=task.name,
                            provider=model.provider,
                            model=model.model,
                            prompt_name=prompt.name,
                            prompt_version=prompt.version,
                            routing_reason=decision.reason,
                            fallback_used=decision.fallback_used,
                            region=provider.region,
                            estimated_cost=decision.estimated_max_cost,
                            execution_maximum_estimated_cost=bounded_estimate,
                            input_reference=request.storage_reference,
                            input_digest=_attachment_set_digest(resolved_attachments),
                        )
                        if not reservation.created:
                            raise AIRequestReplayError(
                                "this AI request id already has a durable execution"
                            )
                        pending_attempt.row_id = reservation.row_id
                    else:
                        pending_attempt.row_id = await recorder.record_attempt(
                            organisation_id=request.organisation_id,
                            user_id=request.user_id,
                            request_id=execution_request_id,
                            attempt_number=dispatch_count,
                            task=task.name,
                            provider=model.provider,
                            model=model.model,
                            prompt_name=prompt.name,
                            prompt_version=prompt.version,
                            routing_reason=decision.reason,
                            fallback_used=decision.fallback_used,
                            region=provider.region,
                            estimated_cost=decision.estimated_max_cost,
                            input_reference=request.storage_reference,
                            input_digest=_attachment_set_digest(resolved_attachments),
                        )
                pending_attempts.append(pending_attempt)
                dispatch_started = perf_counter()
                try:
                    response = await self._call_provider(provider, provider_request)
                except (
                    ProviderUnavailableError,
                    ProviderRateLimitError,
                    ProviderTimeoutError,
                ) as exc:
                    # Bounded transient retry (v0.7 Scope §6.4): only the
                    # retryable provider taxonomy retries, and only up to
                    # max_attempts. When the task's reviewed fallback policy
                    # allows it, the failed model is excluded so the next route
                    # picks an eligible fallback model under the same region
                    # constraints; otherwise the identical model is retried.
                    # Never a retry storm.
                    pending_attempt.error_code = exc.error_code
                    if attempt >= max_attempts:
                        raise
                    last_transient = exc
                    if task.fallback_policy.allowed:
                        excluded_model_ids.append(model.id)
                    continue
                finally:
                    pending_attempt.latency_ms = round(
                        (perf_counter() - dispatch_started) * 1000
                    )
                pending_attempt.usage = TokenUsage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
                if response.model != model.model:
                    raise ProviderResponseError(
                        "provider response model did not match the routed model"
                    )
                try:
                    output = self._validate_output(
                        effective_output_schema,
                        task.declares_text_result,
                        response.content,
                        response.structured,
                    )
                except OutputValidationError as exc:
                    # Bounded repair then bounded malformed-output task retries
                    # (v0.7 Scope §6.4, ADR-0017): at most one repair request
                    # per execution using the same approved routing/policy
                    # path; a repair that fails — or that hits a transient
                    # error — consumes one bounded task retry instead of
                    # escaping or looping. When no repair budget remains, each
                    # malformed output likewise consumes one bounded task
                    # retry and a failure on the final attempt is terminal.
                    # Unvalidated data is never returned.
                    pending_attempt.error_code = exc.error_code
                    if repair_budget > 0:
                        repair_budget -= 1
                        repair_request = self._prepare_repair_request(
                            task,
                            provider_request,
                            response.content,
                            model,
                            maximum_estimated_cost=maximum_estimated_cost,
                        )
                        dispatch_count += 1
                        repair_attempt = _PendingAttempt(
                            row_id=None,
                            model=model,
                            decision=decision,
                            region=provider.region,
                            usage=TokenUsage(input_tokens=0, output_tokens=0),
                        )
                        if recorder is not None:
                            repair_attempt.row_id = await recorder.record_attempt(
                                organisation_id=request.organisation_id,
                                user_id=request.user_id,
                                request_id=execution_request_id,
                                attempt_number=dispatch_count,
                                task=task.name,
                                provider=model.provider,
                                model=model.model,
                                prompt_name=prompt.name,
                                prompt_version=prompt.version,
                                routing_reason=decision.reason,
                                fallback_used=decision.fallback_used,
                                region=provider.region,
                                estimated_cost=estimate_maximum_cost(
                                    task,
                                    model,
                                    estimate_tokens(repair_request.prompt),
                                ),
                                input_reference=request.storage_reference,
                                input_digest=_attachment_set_digest(resolved_attachments),
                            )
                        pending_attempts.append(repair_attempt)
                        repair_started = perf_counter()
                        try:
                            repair_response = await self._call_provider(provider, repair_request)
                            repair_attempt.usage = TokenUsage(
                                input_tokens=repair_response.usage.input_tokens,
                                output_tokens=repair_response.usage.output_tokens,
                            )
                            if repair_response.model != model.model:
                                raise ProviderResponseError(
                                    "provider response model did not match the routed model"
                                )
                            output = self._validate_output(
                                effective_output_schema,
                                task.declares_text_result,
                                repair_response.content,
                                repair_response.structured,
                            )
                        except (
                            ProviderUnavailableError,
                            ProviderRateLimitError,
                            ProviderTimeoutError,
                        ) as exc:
                            # A transient failure inside the repair consumes one
                            # bounded task retry (ADR-0017) instead of escaping.
                            repair_attempt.error_code = exc.error_code
                            if attempt >= max_attempts:
                                raise
                            last_transient = exc
                            if task.fallback_policy.allowed:
                                excluded_model_ids.append(model.id)
                            continue
                        except RepairNotPossibleError:
                            # The repair cannot be dispatched within the task/model
                            # bounds: terminal — retrying cannot shrink the prompt.
                            raise
                        except OutputValidationError as exc:
                            # The repair was dispatched but its output is also
                            # invalid: consume one bounded malformed-output task
                            # retry; a failure on the final attempt is terminal.
                            repair_attempt.error_code = exc.error_code
                            if attempt >= max_attempts:
                                raise
                            continue
                        finally:
                            repair_attempt.latency_ms = round(
                                (perf_counter() - repair_started) * 1000
                            )
                        repair_attempt.error_code = None
                        response = repair_response
                        pending_attempt = repair_attempt
                    else:
                        if attempt >= max_attempts:
                            raise
                        continue
                result = AIResult(
                    request_id=execution_request_id,
                    routing=RoutingMetadata(
                        task=task.name,
                        provider=provider.provider_id,
                        model=response.model,
                        prompt_name=prompt.name,
                        prompt_version=prompt.version,
                        reason=decision.reason,
                        fallback_used=decision.fallback_used,
                        region=response.region,
                    ),
                    output=output,
                    usage=TokenUsage(
                        input_tokens=sum(item.usage.input_tokens for item in pending_attempts),
                        output_tokens=sum(item.usage.output_tokens for item in pending_attempts),
                    ),
                    cost=self._aggregate_cost(pending_attempts, model.pricing.currency),
                    completed_at=datetime.now(UTC),
                )
                winning_attempt = pending_attempt
                break
            else:
                # Every attempt exhausted the bounded retry budget without a
                # result; the last transient failure (or a safe fallback) is
                # the outcome, settled below like any other failure.
                failure = (
                    last_transient
                    if last_transient is not None
                    else ProviderUnavailableError(f"AI execution failed for task {task.name}")
                )
        except AIError as exc:
            failure = exc

        if failure is not None:
            if recorder is not None and pending_attempts:
                # Settlement with actuals: every reserved attempt row receives
                # its own terminal status, its own billed usage priced with its
                # own model's rates, and its own safe error code — never
                # content (BP §28, ADR-0017). A retry-heavy execution is never
                # misaccounted against a single model's rates.
                # The first row carries the bounded execution reservation.
                # Settle it last so concurrent budget checks remain protected
                # until every later attempt's actual cost is durable.
                settlement_order = [*pending_attempts[1:], pending_attempts[0]]
                for pending_attempt in settlement_order:
                    if pending_attempt.row_id is None:
                        continue
                    await recorder.settle(
                        ai_request_id=pending_attempt.row_id,
                        organisation_id=request.organisation_id,
                        task=task.name,
                        user_id=request.user_id,
                        status="failed",
                        error_code=pending_attempt.error_code or failure.error_code,
                        usage=pending_attempt.usage,
                        cost=self._estimate_cost(
                            pending_attempt.model.pricing.input_price_per_million_tokens,
                            pending_attempt.model.pricing.output_price_per_million_tokens,
                            pending_attempt.usage,
                            currency=pending_attempt.model.pricing.currency,
                        ),
                        latency_ms=pending_attempt.latency_ms,
                        routing_provider=pending_attempt.decision.model.provider,
                        routing_model=pending_attempt.decision.model.model,
                        routing_prompt_name=prompt.name,
                        routing_prompt_version=prompt.version,
                        routing_reason=pending_attempt.decision.reason,
                        fallback_used=pending_attempt.decision.fallback_used,
                        region=pending_attempt.region,
                    )
            raise failure

        if result is None:
            # Unreachable: the loop either broke with a result or raised.
            raise ProviderUnavailableError(f"AI execution failed for task {task.name}")

        if recorder is not None:
            settlement_order = [*pending_attempts[1:], pending_attempts[0]]
            for pending_attempt in settlement_order:
                if pending_attempt.row_id is None:
                    continue
                if pending_attempt is winning_attempt:
                    # Terminal success plus the validated output record plus
                    # the audit event commit atomically in the port (BP §11).
                    # Output content is retained only when the task-level
                    # opt-in and the organisation retention policy both permit
                    # it; otherwise the record is references/digests only.
                    await recorder.settle(
                        ai_request_id=pending_attempt.row_id,
                        organisation_id=request.organisation_id,
                        task=task.name,
                        user_id=request.user_id,
                        status="succeeded",
                        error_code=None,
                        usage=pending_attempt.usage,
                        cost=self._estimate_cost(
                            pending_attempt.model.pricing.input_price_per_million_tokens,
                            pending_attempt.model.pricing.output_price_per_million_tokens,
                            pending_attempt.usage,
                            currency=pending_attempt.model.pricing.currency,
                        ),
                        latency_ms=pending_attempt.latency_ms,
                        routing_provider=pending_attempt.decision.model.provider,
                        routing_model=pending_attempt.decision.model.model,
                        routing_prompt_name=prompt.name,
                        routing_prompt_version=prompt.version,
                        routing_reason=pending_attempt.decision.reason,
                        fallback_used=pending_attempt.decision.fallback_used,
                        region=pending_attempt.region,
                        output=result.output,
                        output_reference=None,
                        output_digest=_validated_output_digest(result.output),
                        retain_content=retain_output_content,
                        input_reference=request.storage_reference,
                        input_digest=_attachment_set_digest(resolved_attachments),
                    )
                else:
                    # Earlier attempts of a successful execution failed (transient
                    # error or malformed output); each is settled with its own
                    # error code and actuals so the durable record prices the
                    # real traffic attempt by attempt (v0.7 Scope §2).
                    await recorder.settle(
                        ai_request_id=pending_attempt.row_id,
                        organisation_id=request.organisation_id,
                        task=task.name,
                        user_id=request.user_id,
                        status="failed",
                        error_code=pending_attempt.error_code or "ai_error",
                        usage=pending_attempt.usage,
                        cost=self._estimate_cost(
                            pending_attempt.model.pricing.input_price_per_million_tokens,
                            pending_attempt.model.pricing.output_price_per_million_tokens,
                            pending_attempt.usage,
                            currency=pending_attempt.model.pricing.currency,
                        ),
                        latency_ms=pending_attempt.latency_ms,
                        routing_provider=pending_attempt.decision.model.provider,
                        routing_model=pending_attempt.decision.model.model,
                        routing_prompt_name=prompt.name,
                        routing_prompt_version=prompt.version,
                        routing_reason=pending_attempt.decision.reason,
                        fallback_used=pending_attempt.decision.fallback_used,
                        region=pending_attempt.region,
                    )
        return result

    def _aggregate_cost(
        self,
        attempts: list[_PendingAttempt],
        currency: str,
    ) -> CostEstimate:
        """Sum every attempt's usage-priced cost, each priced with its own model.

        Aggregating per-attempt costs (each with its own model's rates) is what
        makes retries and fallback across differently priced models account
        correctly (v0.7 Scope §2/§6.5). ``currency`` is the winning attempt's
        pricing currency; the registry prices every model in the same currency.
        """
        total = sum(
            (
                self._estimate_cost(
                    attempt.model.pricing.input_price_per_million_tokens,
                    attempt.model.pricing.output_price_per_million_tokens,
                    attempt.usage,
                    currency=attempt.model.pricing.currency,
                ).amount
            )
            for attempt in attempts
        )
        return CostEstimate(amount=Decimal(total), currency=currency)

    async def _resolve_attachments(
        self,
        request: AIRequest,
        attachments: Sequence[Attachment] | None,
    ) -> list[Attachment]:
        """Determine the validated attachment set for one request.

        Explicit ``attachments`` (already resolved by the caller) and a
        request ``storage_reference`` are mutually exclusive; a storage
        reference is resolved through the configured resolver at the service
        boundary (v0.7 Scope §6.4, ADR-0017) and validated against the template
        limits before any routing or dispatch.
        """
        if attachments:
            if request.storage_reference is not None:
                raise AIInputValidationError(
                    "attachments and a storage_reference are mutually exclusive"
                )
            return self._validate_attachments(attachments)
        if request.storage_reference is not None:
            if self._attachment_resolver is None:
                raise AIInputValidationError(
                    "storage references require a configured attachment resolver"
                )
            resolved = await self._attachment_resolver(
                AttachmentResolutionContext(
                    reference=request.storage_reference,
                    organisation_id=request.organisation_id,
                )
            )
            try:
                return validate_attachment_set(resolved)
            except ValueError as exc:
                raise AIInputValidationError(str(exc)) from exc
        return []

    def _output_json_schema(self, output_schema: str | None) -> dict[str, Any] | None:
        """Generate the JSON Schema for the task's Pydantic output model.

        Resolving the schema here — before dispatch — makes an unknown schema
        a fail-fast :class:`OutputSchemaError` (v0.7 Scope §6.2/§6.4) and supplies
        the adapter with the shape for native structured output where the
        adapter truthfully supports it.
        """
        if output_schema is None:
            return None
        model_class = self._schema_resolver(output_schema)
        return model_class.model_json_schema()

    @staticmethod
    def _validate_attachments(
        attachments: Sequence[Attachment] | None,
    ) -> list[Attachment]:
        """Validate the resolved attachment set against the template limits.

        Each :class:`Attachment` is already validated at construction (MIME
        allowlist, per-file size, digest); this enforces the bounded count and
        combined 10 MB ceiling and translates a safe ``ValueError`` into the
        AI input-validation taxonomy before any routing or dispatch.
        """

        if not attachments:
            return []
        try:
            return validate_attachment_set(attachments)
        except ValueError as exc:
            raise AIInputValidationError(str(exc)) from exc

    def _resolve_task(self, name: str) -> TaskDefinition:
        try:
            return self._task_registry.get(name)
        except KeyError as exc:
            raise TaskNotFoundError(f"unknown task: {name}") from exc

    def _resolve_prompt(self, name: str, version: int) -> PromptDefinition:
        try:
            return self._prompt_registry.get(name, version)
        except KeyError as exc:
            raise PromptNotFoundError(f"unknown prompt: {name} v{version}") from exc

    def _validate_input_form(self, prompt: PromptDefinition, request: AIRequest) -> None:
        """Fail fast when the request's input form cannot satisfy the prompt.

        v0.7 Scope §6.4 input normalisation: a prompt that declares ``text`` needs
        text input, ``messages`` needs message input, and ``storage_reference``
        needs a storage reference; metadata variables are satisfied by the
        request's bounded metadata. This runs before attachment resolution so
        the informative error wins over a missing-resolver or missing-object
        error when both conditions exist.
        """
        for variable in prompt.input_variables:
            if variable in request.metadata:
                continue
            if variable == "text" and request.text is None:
                raise AIInputValidationError("task requires text input")
            if variable == "messages" and request.messages is None:
                raise AIInputValidationError("task requires message input")
            if variable == "storage_reference" and request.storage_reference is None:
                raise AIInputValidationError("task requires a storage reference")

    def _render_prompt(
        self, prompt: PromptDefinition, request: AIRequest, attachments: list[Attachment]
    ) -> str:
        """Render the prompt template with allowlisted variables only.

        Only identifiers the prompt declares are substituted; undeclared
        placeholders are left untouched (never evaluated), missing declared
        variables fail fast. This is the safe renderer v0.7 Scope §6.2 validates
        against the registry — no arbitrary template execution, no secrets.

        v0.7 Scope §6.4 input normalisation: text and message content pass through
        the configured redaction hook before the prompt is built, so sensitive
        input never reaches the provider. A ``storage_reference`` variable
        renders only the resolved attachments' approved display names — the
        private reference itself is never rendered as if it were document
        content (ADR-0017) and never reaches the provider.
        """

        values: dict[str, str] = {}
        for variable in prompt.input_variables:
            if variable in request.metadata:
                values[variable] = request.metadata[variable]
            elif variable == "text":
                if request.text is None:
                    raise AIInputValidationError("task requires text input")
                values[variable] = self._redact(request.text)
            elif variable == "messages":
                if request.messages is None:
                    raise AIInputValidationError("task requires message input")
                values[variable] = "\n".join(
                    f"{message.role}: {self._redact(message.content)}"
                    for message in request.messages
                )
            elif variable == "storage_reference":
                if request.storage_reference is None:
                    raise AIInputValidationError("task requires a storage reference")
                if not attachments:
                    raise AIInputValidationError(
                        "task requires a storage reference but no attachment was resolved"
                    )
                values[variable] = ", ".join(attachment.display_name for attachment in attachments)
            else:
                raise AIInputValidationError(f"task requires input variable {variable!r}")

        try:
            rendered = prompt.render(values)
        except ValueError as exc:
            raise AIInputValidationError("prompt input failed safe rendering") from exc
        return f"Task: {request.task}\n{rendered}"

    async def _call_provider(self, provider: LLMProvider, provider_request: ProviderRequest) -> Any:
        try:
            return await provider.complete(provider_request)
        except (AIError, ProviderError):
            raise
        except Exception as exc:
            # Normalise unexpected adapter failures into the safe taxonomy.
            raise ProviderResponseError("provider returned an unexpected error") from exc

    def _prepare_repair_request(
        self,
        task: TaskDefinition,
        request: ProviderRequest,
        previous_content: str,
        model: ModelDefinition,
        *,
        maximum_estimated_cost: Decimal | None,
    ) -> ProviderRequest:
        """Build one bounded repair request after failed Pydantic validation.

        v0.7 Scope §6.4: the repair reuses the same approved routing/policy path —
        identical provider, model, schema and parameters — with a repair
        instruction and the truncated previous output appended to the prompt.
        The response is validated again; a second validation failure raises
        :class:`OutputValidationError` (terminal for this attempt) so
        unvalidated structured data is never returned.

        The appended repair context enlarges the prompt, so the task/model
        context and the request cost ceilings are re-applied to the repair
        prompt before dispatch (v0.7 Scope §6.4/§6.5): a repair can never push a
        request over a reviewed bound. Exceeding a bound is terminal — retrying
        the identical malformed cycle cannot shrink the prompt. Returns the
        provider call itself happens in :meth:`execute`, after its distinct
        durable attempt row has been created.
        """
        previous = previous_content[:MAX_REPAIR_CONTEXT_LENGTH]
        repair_prompt = f"{request.prompt}{_REPAIR_INSTRUCTION.format(previous=previous)}"
        repair_input_tokens = estimate_tokens(repair_prompt)
        max_output_tokens = int(task.parameter_defaults.get("max_tokens", 0))
        if repair_input_tokens > task.max_input_tokens or (
            repair_input_tokens + max_output_tokens > model.context_window
        ):
            raise RepairNotPossibleError(
                "provider output cannot be repaired within the task context limit"
            )
        cost_limit = maximum_estimated_cost
        if task.max_estimated_cost is not None:
            cost_limit = (
                min(cost_limit, task.max_estimated_cost)
                if cost_limit is not None
                else task.max_estimated_cost
            )
        if cost_limit is not None:
            repair_cost = estimate_maximum_cost(task, model, repair_input_tokens)
            if repair_cost > cost_limit:
                raise RepairNotPossibleError(
                    "provider output cannot be repaired within the cost limit"
                )
        return request.model_copy(
            update={
                "prompt": repair_prompt,
                "repair": True,
            }
        )

    def _validate_output(
        self,
        output_schema: str | None,
        declares_text_result: bool,
        content: str,
        structured: dict[str, Any] | None,
    ) -> Any:
        """Validate the provider output against the declared contract.

        A task with an ``output_schema`` must produce data that validates
        against it — malformed JSON or schema mismatch is an
        :class:`OutputValidationError`, never success. Free text is allowed
        only when the task explicitly declares a text result (v0.7 Scope §6.4).
        """

        if output_schema is None:
            if declares_text_result:
                return content
            raise OutputValidationError("task declares neither an output schema nor a text result")
        model_class = self._schema_resolver(output_schema)
        raw = structured if structured is not None else content
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise OutputValidationError("provider returned malformed JSON") from exc
        try:
            validated = model_class.model_validate(raw)
        except ValidationError as exc:
            raise OutputValidationError("provider output failed schema validation") from exc
        return validated

    @staticmethod
    def _estimate_cost(
        input_price_per_million: Decimal,
        output_price_per_million: Decimal,
        usage: TokenUsage,
        *,
        currency: str = "USD",
    ) -> CostEstimate:
        input_cost = input_price_per_million * Decimal(usage.input_tokens) / Decimal(1_000_000)
        output_cost = output_price_per_million * Decimal(usage.output_tokens) / Decimal(1_000_000)
        # The durable records store cost as NUMERIC(18,6) (v0.7 Scope §6.5, BP
        # §10); the accounting rounds at that declared storage precision so a
        # tiny token count can never produce an unrepresentable cost.
        amount = (input_cost + output_cost).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return CostEstimate(amount=amount, currency=currency)
