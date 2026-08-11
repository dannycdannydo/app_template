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

Scope §6.4 (structured outputs, retry and safety controls): every result is
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

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from app.ai.attachments import Attachment, validate_attachment_set
from app.ai.errors import (
    AIError,
    AIInputValidationError,
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
from app.ai.providers.base import LLMProvider, ProviderRequest, ProviderResponse
from app.ai.registry import (
    ModelDefinition,
    ModelRegistry,
    PromptDefinition,
    PromptRegistry,
    RegistryValidationError,
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
    cost ceilings (Scope §6.4/§6.5). Terminal: retrying the identical
    malformed cycle cannot shrink the repair prompt, so no bounded task retry
    is attempted. Carries a safe message that never echoes provider output.
    """

    error_code = "repair_not_possible"


#: Optional pre-dispatch redaction hook (Scope §6.4): applied to the request's
#: text and message content before the prompt is rendered, so sensitive input
#: never reaches the provider or the rendered prompt. Default is identity.
Redactor = Callable[[str], str]

#: Bounded context for one repair request (Scope §6.4): the previous invalid
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

    The task registry's ``output_schema`` (Scope §6.2) and the request's
    optional override are dotted paths; this is the default resolver. Raises
    :class:`OutputSchemaError` when the path is unknown or does not name a
    Pydantic model — fail fast, never fall back to unvalidated data.
    """

    try:
        return resolve_output_schema(path)
    except RegistryValidationError as exc:
        raise OutputSchemaError(str(exc)) from exc


class AIService:
    """Provider-neutral executor for one AI task.

    Constructed with the registry interfaces and the configured provider
    adapter(s); the wiring is owned by the application (Scope §6.3 factory,
    §6.5 organisation settings). ``provider`` is the single-provider shorthand;
    ``providers`` maps provider id → adapter for deployments that enable more
    than one provider, which is what makes the router's reviewed cross-provider
    fallback actually executable (Scope §6.2/§6.4). The fake provider is the
    default adapter under test.

    ``attachment_resolver`` (Scope §6.4, ADR-0017) resolves a request's private
    ``storage_reference`` into bounded in-memory attachments at the service
    boundary; ``None`` rejects storage-referenced requests with a clear error.
    ``redactor`` is applied to text/message content before dispatch.
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
        allowed_providers: list[str] | None = None,
        allowed_model_ids: list[str] | None = None,
        model_override: str | None = None,
        maximum_estimated_cost: Decimal | None = None,
        attachments: Sequence[Attachment] | None = None,
    ) -> AIResult:
        """Execute one task request and return a validated result.

        ``allowed_providers`` is the organisation-level provider allowlist
        enforced by the model registry/router (Scope §6.5); ``None`` means no
        organisation restriction (the default in Scope §6.1 until
        ``organisation_ai_settings`` lands).

        ``attachments`` are bounded inline attachments either passed by the
        caller (already resolved at the service/job boundary) or resolved by
        this service from the request's private ``storage_reference`` through
        the configured ``attachment_resolver`` (Scope §6.4, ADR-0017). They
        are validated against the template limits (5 MB per file / 10 MB
        combined), the router only selects models declaring the ``documents``
        capability with sufficient per-model ceilings, image attachments
        additionally require the model's ``vision`` capability, and the
        configured adapter must declare document support — every incompatible
        modality, MIME type and size combination fails before provider
        dispatch.

        Scope §6.4 safety controls: transient provider failures (unavailable,
        rate limited, timeout) retry within the task's ``retry_policy``
        ``max_attempts``, re-routing through the router's region-safe fallback
        only when the task's ``fallback_policy`` allows it; malformed provider
        output triggers at most ``repair_attempts`` (≤ 1) bounded repair
        request, then consumes one bounded task retry per malformed output,
        and a transient failure inside the repair itself consumes the same
        bounded retry budget instead of escaping; permanent validation/policy
        failures never retry. Every attempt's usage/cost is accounted, so the
        returned result prices the real traffic. Unvalidated structured data
        is never returned.

        Raises an :class:`~app.ai.errors.AIError` subclass with a safe code on
        every failure.
        """

        request_id = uuid4().hex
        task = self._resolve_task(request.task)
        prompt = self._resolve_prompt(task.prompt_name, task.prompt_version)
        # Input-form validation first (Scope §6.4): a task whose prompt
        # declares ``text`` must receive text input — a storage reference can
        # never silently satisfy it — and vice versa.
        self._validate_input_form(prompt, request)
        resolved_attachments = await self._resolve_attachments(request, attachments)
        rendered = self._render_prompt(prompt, request, resolved_attachments)
        # The effective output schema is resolved exactly once: a request
        # override wins, and an empty-string override is treated as "no
        # override" so the provider request and output validation can never
        # disagree (Scope §6.1). Its JSON Schema (Scope §6.4) is generated
        # before dispatch so a bad schema fails fast and every adapter can
        # request native structured output.
        effective_output_schema = request.output_schema or task.output_schema
        output_json_schema = self._output_json_schema(effective_output_schema)
        configured_max_tokens = task.parameter_defaults.get("max_tokens")
        configured_temperature = task.parameter_defaults.get("temperature")
        estimated_input_tokens = estimate_tokens(rendered)
        max_attempts = task.retry_policy.max_attempts
        repair_budget = task.retry_policy.repair_attempts
        excluded_model_ids: list[str] = []
        last_transient: ProviderError | None = None

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
            try:
                response = await self._call_provider(provider, provider_request)
            except (ProviderUnavailableError, ProviderRateLimitError, ProviderTimeoutError) as exc:
                # Bounded transient retry (Scope §6.4): only the retryable
                # provider taxonomy retries, and only up to max_attempts. When
                # the task's reviewed fallback policy allows it, the failed
                # model is excluded so the next route picks an eligible
                # fallback model under the same region constraints; otherwise
                # the identical model is retried. Never a retry storm.
                if attempt >= max_attempts:
                    raise
                last_transient = exc
                if task.fallback_policy.allowed:
                    excluded_model_ids.append(model.id)
                continue
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
                total_usage = response.usage
            except OutputValidationError:
                # Bounded repair then bounded malformed-output task retries
                # (Scope §6.4, ADR-0017): at most one repair request per
                # execution using the same approved routing/policy path; a
                # repair that fails — or that hits a transient error — consumes
                # one bounded task retry instead of escaping or looping. When
                # no repair budget remains, each malformed output likewise
                # consumes one bounded task retry and a failure on the final
                # attempt is terminal. Unvalidated data is never returned.
                if repair_budget > 0:
                    repair_budget -= 1
                    try:
                        output, repair_response = await self._repair_output(
                            effective_output_schema,
                            task,
                            provider,
                            provider_request,
                            response.content,
                            model,
                            maximum_estimated_cost=maximum_estimated_cost,
                        )
                    except (
                        ProviderUnavailableError,
                        ProviderRateLimitError,
                        ProviderTimeoutError,
                    ) as exc:
                        # A transient failure inside the repair consumes one
                        # bounded task retry (ADR-0017) instead of escaping.
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
                    except OutputValidationError:
                        # The repair was dispatched but its output is also
                        # invalid: consume one bounded malformed-output task
                        # retry; a failure on the final attempt is terminal.
                        if attempt >= max_attempts:
                            raise
                        continue
                    # Aggregate the actual attempt data (Scope §6.5): the
                    # original malformed response and the repair response both
                    # billed tokens, so usage/cost price the real traffic
                    # instead of under-reporting the repair.
                    total_usage = TokenUsage(
                        input_tokens=response.usage.input_tokens
                        + repair_response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens
                        + repair_response.usage.output_tokens,
                    )
                    response = repair_response
                else:
                    if attempt >= max_attempts:
                        raise
                    continue
            return AIResult(
                request_id=request_id,
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
                usage=total_usage,
                cost=self._estimate_cost(
                    model.pricing.input_price_per_million_tokens,
                    model.pricing.output_price_per_million_tokens,
                    total_usage,
                    currency=model.pricing.currency,
                ),
                completed_at=datetime.now(UTC),
            )
        # Unreachable: every loop path above returns or raises. Kept for
        # Pyright totality and as a final safe guard.
        raise (
            last_transient
            if last_transient is not None
            else ProviderUnavailableError(f"AI execution failed for task {task.name}")
        )

    async def _resolve_attachments(
        self,
        request: AIRequest,
        attachments: Sequence[Attachment] | None,
    ) -> list[Attachment]:
        """Determine the validated attachment set for one request.

        Explicit ``attachments`` (already resolved by the caller) and a
        request ``storage_reference`` are mutually exclusive; a storage
        reference is resolved through the configured resolver at the service
        boundary (Scope §6.4, ADR-0017) and validated against the template
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
        a fail-fast :class:`OutputSchemaError` (Scope §6.2/§6.4) and supplies
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

        Scope §6.4 input normalisation: a prompt that declares ``text`` needs
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
        variables fail fast. This is the safe renderer Scope §6.2 validates
        against the registry — no arbitrary template execution, no secrets.

        Scope §6.4 input normalisation: text and message content pass through
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

    async def _repair_output(
        self,
        output_schema: str | None,
        task: TaskDefinition,
        provider: LLMProvider,
        request: ProviderRequest,
        previous_content: str,
        model: ModelDefinition,
        *,
        maximum_estimated_cost: Decimal | None,
    ) -> tuple[Any, ProviderResponse]:
        """One bounded repair request after a failed Pydantic validation.

        Scope §6.4: the repair reuses the same approved routing/policy path —
        identical provider, model, schema and parameters — with a repair
        instruction and the truncated previous output appended to the prompt.
        The response is validated again; a second validation failure raises
        :class:`OutputValidationError` (terminal for this attempt) so
        unvalidated structured data is never returned.

        The appended repair context enlarges the prompt, so the task/model
        context and the request cost ceilings are re-applied to the repair
        prompt before dispatch (Scope §6.4/§6.5): a repair can never push a
        request over a reviewed bound. Exceeding a bound is terminal — retrying
        the identical malformed cycle cannot shrink the prompt. Returns the
        validated output together with the repair response so the caller can
        aggregate usage/cost across every actual attempt.
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
        repair_request = request.model_copy(
            update={
                "prompt": repair_prompt,
                "repair": True,
            }
        )
        response = await self._call_provider(provider, repair_request)
        if response.model != request.model:
            raise ProviderResponseError("provider response model did not match the routed model")
        output = self._validate_output(
            output_schema,
            task.declares_text_result,
            response.content,
            response.structured,
        )
        return output, response

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
        only when the task explicitly declares a text result (Scope §6.4).
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
        return CostEstimate(amount=input_cost + output_cost, currency=currency)
