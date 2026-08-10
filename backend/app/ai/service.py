"""AIService — the only application-facing entry point to the AI layer.

v0.7 Scope §6.1, ADR-0017: application code calls
``AIService.execute(request: AIRequest) -> AIResult`` and names a task, never
a provider or model. The service resolves the task → prompt → model through
the registry interfaces, renders the prompt with a safe allowlisted renderer,
dispatches through the provider boundary, validates structured output against
the task's Pydantic schema, and returns a result with usage/cost/routing
metadata. Provider SDKs never appear here (BP §33, ADR-0017).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from app.ai.errors import (
    AIError,
    AIInputValidationError,
    ModelNotAvailableError,
    OutputSchemaError,
    OutputValidationError,
    PromptNotFoundError,
    ProviderError,
    ProviderResponseError,
    TaskNotFoundError,
)
from app.ai.providers.base import LLMProvider, ProviderRequest
from app.ai.registry import (
    ModelRegistry,
    PromptDefinition,
    PromptRegistry,
    RegistryValidationError,
    TaskDefinition,
    TaskRegistry,
    estimate_tokens,
    resolve_output_schema,
)
from app.ai.schemas import AIRequest, AIResult, CostEstimate, RoutingMetadata, TokenUsage

SchemaResolver = Callable[[str], type[BaseModel]]


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

    Constructed with the registry interfaces and one provider adapter; the
    wiring is owned by the application (Scope §6.3 factory, §6.5 organisation
    settings). The fake provider is the default adapter under test.
    """

    def __init__(
        self,
        *,
        task_registry: TaskRegistry,
        prompt_registry: PromptRegistry,
        model_registry: ModelRegistry,
        provider: LLMProvider,
        schema_resolver: SchemaResolver = import_schema,
    ) -> None:
        self._task_registry = task_registry
        self._prompt_registry = prompt_registry
        self._model_registry = model_registry
        self._provider = provider
        self._schema_resolver = schema_resolver

    async def execute(
        self,
        request: AIRequest,
        *,
        allowed_providers: list[str] | None = None,
        allowed_model_ids: list[str] | None = None,
        model_override: str | None = None,
        maximum_estimated_cost: Decimal | None = None,
    ) -> AIResult:
        """Execute one task request and return a validated result.

        ``allowed_providers`` is the organisation-level provider allowlist
        enforced by the model registry/router (Scope §6.5); ``None`` means no
        organisation restriction (the default in Scope §6.1 until
        ``organisation_ai_settings`` lands).

        Raises an :class:`~app.ai.errors.AIError` subclass with a safe code on
        every failure; unvalidated structured data is never returned.
        """

        request_id = uuid4().hex
        task = self._resolve_task(request.task)
        prompt = self._resolve_prompt(task.prompt_name, task.prompt_version)
        rendered = self._render_prompt(prompt, request)
        try:
            decision = self._model_registry.route(
                task,
                allowed_providers=allowed_providers,
                allowed_model_ids=allowed_model_ids,
                model_override=model_override,
                estimated_input_tokens=estimate_tokens(rendered),
                maximum_estimated_cost=maximum_estimated_cost,
            )
        except (KeyError, ValueError) as exc:
            raise ModelNotAvailableError(f"no model satisfies task {task.name}") from exc
        model = decision.model
        if model.provider != self._provider.provider_id:
            raise ModelNotAvailableError(
                "resolved model provider is not configured for this service"
            )
        # Resolve the effective output schema exactly once: a request override
        # wins, and an empty-string override is treated as "no override" so the
        # provider request and output validation can never disagree (Scope §6.1).
        effective_output_schema = request.output_schema or task.output_schema
        configured_max_tokens = task.parameter_defaults.get("max_tokens")
        configured_temperature = task.parameter_defaults.get("temperature")
        provider_request = ProviderRequest(
            task=task.name,
            model=model.model,
            prompt=rendered,
            output_schema=effective_output_schema,
            max_tokens=configured_max_tokens if isinstance(configured_max_tokens, int) else None,
            temperature=(
                float(configured_temperature)
                if isinstance(configured_temperature, (int, float))
                else None
            ),
            metadata=request.metadata,
        )
        response = await self._call_provider(provider_request)
        if response.model != model.model:
            raise ProviderResponseError("provider response model did not match the routed model")

        output = self._validate_output(
            effective_output_schema,
            task.declares_text_result,
            response.content,
            response.structured,
        )
        return AIResult(
            request_id=request_id,
            routing=RoutingMetadata(
                task=task.name,
                provider=self._provider.provider_id,
                model=response.model,
                prompt_name=prompt.name,
                prompt_version=prompt.version,
                reason=decision.reason,
                fallback_used=decision.fallback_used,
            ),
            output=output,
            usage=response.usage,
            cost=self._estimate_cost(
                model.pricing.input_price_per_million_tokens,
                model.pricing.output_price_per_million_tokens,
                response.usage,
                currency=model.pricing.currency,
            ),
            completed_at=datetime.now(UTC),
        )

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

    def _render_prompt(self, prompt: PromptDefinition, request: AIRequest) -> str:
        """Render the prompt template with allowlisted variables only.

        Only identifiers the prompt declares are substituted; undeclared
        placeholders are left untouched (never evaluated), missing declared
        variables fail fast. This is the safe renderer Scope §6.2 validates
        against the registry — no arbitrary template execution, no secrets.
        """

        values: dict[str, str] = {}
        for variable in prompt.input_variables:
            if variable in request.metadata:
                values[variable] = request.metadata[variable]
            elif variable == "text":
                if request.text is None:
                    raise AIInputValidationError("task requires text input")
                values[variable] = request.text
            elif variable == "messages":
                if request.messages is None:
                    raise AIInputValidationError("task requires message input")
                values[variable] = "\n".join(
                    f"{message.role}: {message.content}" for message in request.messages
                )
            elif variable == "storage_reference":
                if request.storage_reference is None:
                    raise AIInputValidationError("task requires a storage reference")
                values[variable] = request.storage_reference
            else:
                raise AIInputValidationError(f"task requires input variable {variable!r}")

        try:
            rendered = prompt.render(values)
        except ValueError as exc:
            raise AIInputValidationError("prompt input failed safe rendering") from exc
        return f"Task: {request.task}\n{rendered}"

    async def _call_provider(self, provider_request: ProviderRequest) -> Any:
        try:
            return await self._provider.complete(provider_request)
        except (AIError, ProviderError):
            raise
        except Exception as exc:
            # Normalise unexpected adapter failures into the safe taxonomy.
            raise ProviderResponseError("provider returned an unexpected error") from exc

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
