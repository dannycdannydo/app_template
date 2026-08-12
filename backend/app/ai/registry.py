"""Checked-in AI task, prompt and model registries (v0.7 Scope §6.2).

Definitions are validated Pydantic records loaded with PyYAML's safe loader.
The bundle validator resolves every task → prompt → schema → model reference,
so invalid reviewed configuration fails at application startup and in CI.
"""

from __future__ import annotations

import importlib
import re
import string
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.ai.attachments import (
    ALLOWED_ATTACHMENT_MIME_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    PROVIDER_INLINE_ATTACHMENT_MIME_TYPES,
    Attachment,
)
from app.ai.errors import RegistryValidationError
from app.ai.transfer import (
    INLINE_AGGREGATE_THRESHOLD_BYTES,
    MAX_LARGE_ATTACHMENT_BYTES,
    NON_INLINE_MIME_TYPES,
    TransferContracts,
    TransferMode,
    load_transfer_contracts,
)

MAX_REGISTRY_FILE_BYTES = 256 * 1024
MAX_PROMPT_INSTRUCTIONS_LENGTH = 16 * 1024
MAX_PROMPT_TEMPLATE_LENGTH = 64 * 1024
MAX_RENDERED_PROMPT_LENGTH = 128 * 1024
CHARS_PER_ESTIMATED_TOKEN = 4
ALLOWED_SCHEMA_PREFIXES = ("app.ai.tasks.schemas.", "app.modules.")
ALLOWED_PARAMETERS = frozenset({"max_tokens", "temperature"})
_VARIABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SECRET_NAME = re.compile(
    r"(?:secret|password|passwd|token|api[_-]?key|authorization|credential)", re.IGNORECASE
)


class Capability(StrEnum):
    STRUCTURED_OUTPUT = "structured_output"
    VISION = "vision"
    TOOLS = "tools"
    REASONING = "reasoning"
    #: Bounded inline document/attachment input (v0.7 Scope §6.2 amendment).
    #: Models declaring it must also declare per-model inline attachment
    #: ceilings; models without it can never carry attachments.
    DOCUMENTS = "documents"


class QualityTier(StrEnum):
    ECONOMY = "economy"
    STANDARD = "standard"
    PREMIUM = "premium"


class LatencyTier(StrEnum):
    INTERACTIVE = "interactive"
    BALANCED = "balanced"
    BATCH = "batch"


_QUALITY_RANK = {
    QualityTier.ECONOMY: 0,
    QualityTier.STANDARD: 1,
    QualityTier.PREMIUM: 2,
}


class PricingBasis(BaseModel):
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    input_price_per_million_tokens: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    output_price_per_million_tokens: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    effective_date: date
    owner: str = Field(min_length=1, max_length=128)


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    repair_attempts: int = Field(default=1, ge=0, le=1)


class FallbackPolicy(BaseModel):
    allowed: bool = False
    prefer_same_provider: bool = True
    allow_local: bool = False


class TaskDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9._-]*$")
    prompt_name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9._-]*$")
    prompt_version: int = Field(ge=1)
    input_variables: list[str] = Field(default_factory=list, max_length=32)
    required_capabilities: list[Capability] = Field(default_factory=lambda: list[Capability]())
    parameter_defaults: dict[str, int | float] = Field(default_factory=lambda: {"max_tokens": 1024})
    output_schema: str | None = Field(default=None, max_length=512)
    declares_text_result: bool = False
    # v0.7 Scope §2 retention choice: whether this task may retain the (already
    # validated, redacted) output content in ``ai_outputs``. The default is
    # off — records store references/digests only — and even when a task opts
    # in, the organisation must configure a retention policy for content to be
    # stored (both controls are required).
    retains_output_content: bool = False
    # v0.8 Scope §2.2: the provider-neutral transfer modes this task permits.
    # ``inline`` is the only default and remains eligible through the
    # 5,000,000-byte aggregate raw threshold; a non-inline mode is eligible
    # only when the source lifecycle, this task declaration, the organisation
    # policy, the routed model/provider capability and the deployment
    # configuration all allow it. A feature never selects a mode — this
    # declaration is one of the gates the deterministic selector intersects.
    allowed_transfer_modes: list[TransferMode] = Field(
        default_factory=lambda: [TransferMode.INLINE]
    )
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)
    model_preferences: list[str] = Field(default_factory=list)
    quality_tier: QualityTier = QualityTier.STANDARD
    latency_tier: LatencyTier = LatencyTier.BALANCED
    max_input_tokens: int = Field(default=16_384, ge=1, le=2_000_000)
    max_estimated_cost: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=6)

    @field_validator("input_variables")
    @classmethod
    def _validate_variables(cls, values: list[str]) -> list[str]:
        _validate_variable_names(values, context="task")
        return values

    @field_validator("model_preferences")
    @classmethod
    def _validate_model_preferences(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("model_preferences must not contain duplicates")
        return values

    @field_validator("allowed_transfer_modes")
    @classmethod
    def _validate_allowed_transfer_modes(cls, values: list[TransferMode]) -> list[TransferMode]:
        if not values:
            raise ValueError("allowed_transfer_modes must not be empty")
        if len(values) != len(set(values)):
            raise ValueError("allowed_transfer_modes must not contain duplicates")
        return values

    @field_validator("parameter_defaults")
    @classmethod
    def _validate_parameters(cls, values: dict[str, int | float]) -> dict[str, int | float]:
        unknown = set(values) - ALLOWED_PARAMETERS
        if unknown:
            raise ValueError(f"unsupported task parameters: {sorted(unknown)}")
        max_tokens = values.get("max_tokens")
        if max_tokens is not None and (
            not isinstance(max_tokens, int) or not 1 <= max_tokens <= 128_000
        ):
            raise ValueError("max_tokens must be an integer between 1 and 128000")
        temperature = values.get("temperature")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        return values

    @model_validator(mode="after")
    def _validate_result_contract(self) -> TaskDefinition:
        if (self.output_schema is None) == (not self.declares_text_result):
            raise ValueError("task must declare exactly one of output_schema or text result")
        return self


class PromptDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9._-]*$")
    version: int = Field(ge=1)
    system_instructions: str = Field(min_length=1, max_length=MAX_PROMPT_INSTRUCTIONS_LENGTH)
    input_variables: list[str] = Field(default_factory=list, max_length=32)
    user_template: str = Field(min_length=1, max_length=MAX_PROMPT_TEMPLATE_LENGTH)
    output_contract: str = Field(min_length=1, max_length=512)

    @field_validator("input_variables")
    @classmethod
    def _validate_variables(cls, values: list[str]) -> list[str]:
        _validate_variable_names(values, context="prompt")
        return values

    @model_validator(mode="after")
    def _validate_template(self) -> PromptDefinition:
        placeholders = _template_variables(self.user_template)
        declared = set(self.input_variables)
        if placeholders != declared:
            missing = sorted(declared - placeholders)
            undeclared = sorted(placeholders - declared)
            raise ValueError(
                f"template variables do not match declarations; missing={missing}, undeclared={undeclared}"
            )
        if _template_variables(self.system_instructions):
            raise ValueError("system_instructions must be static and contain no interpolation")
        return self

    def render(self, variables: Mapping[str, str]) -> str:
        """Render only declared simple placeholders; never evaluate expressions."""
        supplied = set(variables)
        declared = set(self.input_variables)
        if supplied != declared:
            raise RegistryValidationError(
                f"prompt variables do not match declarations; missing={sorted(declared - supplied)}, "
                f"unexpected={sorted(supplied - declared)}"
            )
        rendered = self.user_template.format_map(dict(variables))
        prompt = f"{self.system_instructions}\n{rendered}"
        if len(prompt) > MAX_RENDERED_PROMPT_LENGTH:
            raise RegistryValidationError("rendered prompt exceeds the configured length limit")
        return prompt


class NonInlineModeLimit(BaseModel):
    """Per-mode non-inline limits one model can carry (v0.8 Scope §2.2).

    ``mime_types`` and ``max_bytes`` are the per-mode MIME set and byte
    ceiling for exactly one non-inline transfer mode, so a model can express
    (and the bundle validator can check) differing
    ``provider_upload``/``managed_signed_url``/``storage_reference`` limits
    against the provider's reviewed per-mode contract. The v0.8 non-inline
    path is exactly one PDF (Scope §2.1, §5.3) and the provider contract's
    lower ceiling always wins.
    """

    mime_types: list[str] = Field(min_length=1)
    max_bytes: int = Field(ge=1, le=MAX_LARGE_ATTACHMENT_BYTES)

    @field_validator("mime_types")
    @classmethod
    def _mime_types_are_reviewed(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("mime_types must not contain duplicates")
        unknown = set(values) - NON_INLINE_MIME_TYPES
        if unknown:
            raise ValueError(
                "v0.8 non-inline transfer modes carry exactly one PDF; "
                f"unsupported: {sorted(unknown)}"
            )
        return values


class ModelDefinition(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9._-]*$")
    provider: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9._-]*$")
    model: str = Field(min_length=1, max_length=256)
    capabilities: list[Capability] = Field(default_factory=lambda: list[Capability]())
    context_window: int = Field(ge=1, le=2_000_000)
    supported_parameters: list[str] = Field(default_factory=list)
    quality_tier: QualityTier = QualityTier.STANDARD
    latency_tier: LatencyTier = LatencyTier.BALANCED
    priority: int = Field(default=100, ge=0, le=10_000)
    pricing: PricingBasis
    available: bool = True
    # Per-model inline attachment ceilings (v0.7 Scope §6.2 amendment). Models
    # declaring the ``documents`` capability must declare both; models without
    # it must declare neither. Ceilings are reviewed configuration bounded by
    # the template limits (5 MB per file / 10 MB combined, ADR-0017).
    max_attachment_bytes: int | None = Field(
        default=None, ge=1, le=MAX_ATTACHMENT_BYTES, description="Per-file inline ceiling"
    )
    max_total_attachment_bytes: int | None = Field(
        default=None,
        ge=1,
        le=MAX_TOTAL_ATTACHMENT_BYTES,
        description="Combined inline ceiling for one request",
    )
    # Per-model inline MIME capability set (v0.7 Scope §6.3 attachment
    # amendment). Models declaring the ``documents`` capability must declare a
    # non-empty subset of the template allowlist that the provider's adapter
    # can actually carry in its native wire form; models without it must not
    # declare one. The router rejects an attachment whose MIME type is outside
    # the routed model's set before any provider dispatch.
    attachment_mime_types: list[str] | None = Field(
        default=None,
        description="MIME types this model can carry as native inline attachments",
    )
    # v0.8 Scope §2.2: the provider-neutral transfer modes this model's provider
    # can carry, with per-mode MIME types and byte ceilings. ``inline`` is the
    # only default; a model declaring a non-inline mode must declare per-mode
    # limits for every non-inline mode it allows, and the provider contract's
    # lower ceiling always wins (Scope §2.1). The checked-in bundle validator
    # rejects any declaration the provider's reviewed contract cannot support.
    allowed_transfer_modes: list[TransferMode] = Field(
        default_factory=lambda: [TransferMode.INLINE]
    )
    # v0.8 Scope §2.2: per-mode MIME types and byte ceilings for the non-inline
    # transfer modes this model declares (one entry per non-inline mode in
    # ``allowed_transfer_modes``). The v0.8 large path is exactly one PDF
    # (Scope §2.1, §5.3).
    transfer_mode_limits: dict[TransferMode, NonInlineModeLimit] = Field(
        default_factory=lambda: dict[TransferMode, NonInlineModeLimit]()
    )

    @field_validator("supported_parameters")
    @classmethod
    def _validate_supported_parameters(cls, values: list[str]) -> list[str]:
        unknown = set(values) - ALLOWED_PARAMETERS
        if unknown:
            raise ValueError(f"unknown supported parameters: {sorted(unknown)}")
        return values

    @field_validator("attachment_mime_types")
    @classmethod
    def _validate_attachment_mime_types(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if not values:
            raise ValueError("attachment_mime_types must be empty or absent, not an empty list")
        unknown = set(values) - ALLOWED_ATTACHMENT_MIME_TYPES
        if unknown:
            raise ValueError(f"unknown attachment MIME types: {sorted(unknown)}")
        if len(set(values)) != len(values):
            raise ValueError("attachment_mime_types must not contain duplicates")
        return values

    @field_validator("allowed_transfer_modes")
    @classmethod
    def _validate_allowed_transfer_modes(cls, values: list[TransferMode]) -> list[TransferMode]:
        if not values:
            raise ValueError("allowed_transfer_modes must not be empty")
        if len(values) != len(set(values)):
            raise ValueError("allowed_transfer_modes must not contain duplicates")
        return values

    @field_validator("transfer_mode_limits")
    @classmethod
    def _validate_transfer_mode_limits(
        cls, values: dict[TransferMode, NonInlineModeLimit]
    ) -> dict[TransferMode, NonInlineModeLimit]:
        if not values:
            return values
        if TransferMode.INLINE in values:
            raise ValueError(
                "transfer_mode_limits covers non-inline modes only; inline limits "
                "come from the v0.7 attachment ceilings"
            )
        return values

    @model_validator(mode="after")
    def _validate_attachment_contract(self) -> ModelDefinition:
        supports_documents = Capability.DOCUMENTS in self.capabilities
        has_per_file_ceiling = self.max_attachment_bytes is not None
        has_total_ceiling = self.max_total_attachment_bytes is not None
        if supports_documents and not (has_per_file_ceiling and has_total_ceiling):
            raise ValueError(
                "models declaring the documents capability must declare both "
                "inline attachment ceilings (per-file and combined)"
            )
        if not supports_documents and (has_per_file_ceiling or has_total_ceiling):
            raise ValueError("inline attachment ceilings require the documents capability")
        if (
            self.max_total_attachment_bytes is not None
            and self.max_attachment_bytes is not None
            and self.max_total_attachment_bytes < self.max_attachment_bytes
        ):
            raise ValueError("max_total_attachment_bytes must not be below max_attachment_bytes")
        if supports_documents and not self.attachment_mime_types:
            raise ValueError(
                "models declaring the documents capability must declare attachment_mime_types"
            )
        if not supports_documents and self.attachment_mime_types:
            raise ValueError("attachment_mime_types require the documents capability")
        # v0.8 Scope §2.2: non-inline transfer declarations must be complete and
        # consistent. A model that declares a non-inline mode must declare
        # per-mode limits for every non-inline mode (the fields are a contract,
        # not an optional hint); a model that declares neither may not carry
        # either, and a limit entry must name a mode the model actually allows.
        # The v0.8 large path is exactly one PDF above the 5,000,000-byte
        # aggregate inline threshold (Scope §2.1, §5.3), and non-inline input is
        # still document input, so the ``documents`` capability is required.
        declares_non_inline = any(
            mode is not TransferMode.INLINE for mode in self.allowed_transfer_modes
        )
        if declares_non_inline and not supports_documents:
            raise ValueError("non-inline transfer modes require the documents capability")
        declared_modes = set(self.transfer_mode_limits)
        if not declares_non_inline:
            if declared_modes:
                raise ValueError("transfer_mode_limits require a non-inline transfer mode")
        else:
            allowed_non_inline = {
                mode for mode in self.allowed_transfer_modes if mode is not TransferMode.INLINE
            }
            missing = allowed_non_inline - declared_modes
            if missing:
                raise ValueError(
                    "models declaring a non-inline transfer mode must declare per-mode "
                    f"limits for: {sorted(missing.value for missing in missing)}"
                )
            stray = declared_modes - allowed_non_inline
            if stray:
                raise ValueError(
                    "transfer_mode_limits name modes not in allowed_transfer_modes: "
                    f"{sorted(mode.value for mode in stray)}"
                )
            for mode, limits in self.transfer_mode_limits.items():
                if limits.max_bytes <= INLINE_AGGREGATE_THRESHOLD_BYTES:
                    raise ValueError(
                        f"mode {mode.value!r} max_bytes must be above the "
                        f"{INLINE_AGGREGATE_THRESHOLD_BYTES} byte aggregate inline threshold"
                    )
        return self


class RoutingDecision(BaseModel):
    model: ModelDefinition
    reason: str
    fallback_used: bool = False
    estimated_input_tokens: int = Field(ge=0)
    estimated_max_cost: Decimal = Field(ge=0)


class TaskRegistry(ABC):
    @abstractmethod
    def get(self, name: str) -> TaskDefinition: ...

    @abstractmethod
    def all(self) -> list[TaskDefinition]: ...


class PromptRegistry(ABC):
    @abstractmethod
    def get(self, name: str, version: int) -> PromptDefinition: ...

    @abstractmethod
    def all(self) -> list[PromptDefinition]: ...


class ModelRegistry(ABC):
    @abstractmethod
    def get(self, provider: str, model: str) -> ModelDefinition: ...

    @abstractmethod
    def all(self) -> list[ModelDefinition]: ...

    @abstractmethod
    def resolve(
        self, task: TaskDefinition, *, allowed_providers: list[str] | None = None
    ) -> ModelDefinition: ...

    def route(
        self,
        task: TaskDefinition,
        *,
        allowed_providers: list[str] | None = None,
        allowed_model_ids: list[str] | None = None,
        model_override: str | None = None,
        estimated_input_tokens: int = 0,
        maximum_estimated_cost: Decimal | None = None,
        excluded_model_ids: Iterable[str] = (),
        attachments: Iterable[Attachment] = (),
        region_of_provider: Mapping[str, str] | None = None,
    ) -> RoutingDecision:
        """Compatibility route for custom registries implementing ``resolve``.

        Production uses the checked-in capability/cost implementation below;
        this default keeps test/application registry substitutions behind the
        same interface while enforcing caller-supplied hard constraints.
        ``region_of_provider`` (provider id → configured region, Scope §6.3
        regional amendment) is accepted for interface parity; this default
        rejects fallback outright, so it can never change region.
        """
        if excluded_model_ids:
            raise ValueError(f"fallback is unsupported by registry for task {task.name}")
        if estimated_input_tokens > task.max_input_tokens:
            raise ValueError(f"input exceeds token budget for task {task.name}")
        model = self.resolve(task, allowed_providers=allowed_providers)
        attachment_list = list(attachments)
        if attachment_list and not _model_can_carry_attachments(model, attachment_list):
            raise ValueError(f"model cannot carry the supplied attachments for task {task.name}")
        if allowed_model_ids is not None and model.id not in allowed_model_ids:
            raise ValueError(f"model is not allowed for task {task.name}")
        if model_override is not None and model.id != model_override:
            raise ValueError(f"model override is unavailable for task {task.name}")
        cost = estimate_maximum_cost(task, model, estimated_input_tokens)
        cost_limit = maximum_estimated_cost
        if task.max_estimated_cost is not None:
            cost_limit = (
                min(cost_limit, task.max_estimated_cost)
                if cost_limit is not None
                else task.max_estimated_cost
            )
        if cost_limit is not None and cost > cost_limit:
            raise ValueError(f"estimated cost exceeds limit for task {task.name}")
        return RoutingDecision(
            model=model,
            reason=f"resolved via model registry for task {task.name}",
            estimated_input_tokens=estimated_input_tokens,
            estimated_max_cost=cost,
        )


class FileTaskRegistry(TaskRegistry):
    def __init__(self, tasks: Iterable[TaskDefinition]) -> None:
        self._tasks: dict[str, TaskDefinition] = {}
        for task in tasks:
            if task.name in self._tasks:
                raise RegistryValidationError(f"duplicate task name: {task.name}")
            self._tasks[task.name] = task

    @classmethod
    def from_directory(cls, directory: Path) -> FileTaskRegistry:
        tasks: list[TaskDefinition] = []
        for path in _yaml_files(directory):
            try:
                tasks.append(TaskDefinition.model_validate(_read_yaml_mapping(path)))
            except ValidationError as exc:
                raise RegistryValidationError(
                    f"invalid task definition {path}: {registry_error_message(exc)}"
                ) from exc
        return cls(tasks)

    def get(self, name: str) -> TaskDefinition:
        try:
            return self._tasks[name]
        except KeyError as exc:
            raise KeyError(f"unknown task: {name}") from exc

    def all(self) -> list[TaskDefinition]:
        return list(self._tasks.values())


class FilePromptRegistry(PromptRegistry):
    def __init__(self, prompts: Iterable[PromptDefinition]) -> None:
        self._prompts: dict[tuple[str, int], PromptDefinition] = {}
        for prompt in prompts:
            key = (prompt.name, prompt.version)
            if key in self._prompts:
                raise RegistryValidationError(
                    f"duplicate prompt version: {prompt.name} v{prompt.version}"
                )
            self._prompts[key] = prompt

    @classmethod
    def from_directory(cls, directory: Path) -> FilePromptRegistry:
        prompts: list[PromptDefinition] = []
        for path in _yaml_files(directory):
            try:
                prompt = PromptDefinition.model_validate(_read_yaml_mapping(path))
            except ValidationError as exc:
                raise RegistryValidationError(
                    f"invalid prompt definition {path}: {registry_error_message(exc)}"
                ) from exc
            name_parts = prompt.name.split(".")
            expected_path = (
                Path(*name_parts[:-1]) / f"{name_parts[-1]}_v{prompt.version}{path.suffix}"
            )
            if path.relative_to(directory) != expected_path:
                raise RegistryValidationError(
                    f"prompt filename must match its name and version ({expected_path}): {path}"
                )
            prompts.append(prompt)
        return cls(prompts)

    def get(self, name: str, version: int) -> PromptDefinition:
        try:
            return self._prompts[(name, version)]
        except KeyError as exc:
            raise KeyError(f"unknown prompt: {name} v{version}") from exc

    def all(self) -> list[PromptDefinition]:
        return list(self._prompts.values())


class CapabilityCostModelRegistry(ModelRegistry):
    """Deterministic capability, context, tier and maximum-cost router."""

    def __init__(self, models: Iterable[ModelDefinition]) -> None:
        self._models_by_id: dict[str, ModelDefinition] = {}
        self._models_by_provider_key: dict[tuple[str, str], ModelDefinition] = {}
        for model in models:
            provider_key = (model.provider, model.model)
            if model.id in self._models_by_id:
                raise RegistryValidationError(f"duplicate model id: {model.id}")
            if provider_key in self._models_by_provider_key:
                raise RegistryValidationError(
                    f"duplicate provider/model: {model.provider}/{model.model}"
                )
            self._models_by_id[model.id] = model
            self._models_by_provider_key[provider_key] = model

    @classmethod
    def from_directory(cls, directory: Path) -> CapabilityCostModelRegistry:
        models: list[ModelDefinition] = []
        for path in _yaml_files(directory):
            raw = _read_yaml(path)
            if isinstance(raw, Mapping) and "models" in raw:
                mapping = cast(Mapping[str, object], raw)
                entries_value = mapping["models"]
                if not isinstance(entries_value, list):
                    raise RegistryValidationError(f"models must be a list: {path}")
                entries = cast(list[object], entries_value)
                for entry in entries:
                    try:
                        models.append(ModelDefinition.model_validate(entry))
                    except ValidationError as exc:
                        raise RegistryValidationError(
                            f"invalid model definition {path}: {registry_error_message(exc)}"
                        ) from exc
            else:
                try:
                    models.append(ModelDefinition.model_validate(raw))
                except ValidationError as exc:
                    raise RegistryValidationError(
                        f"invalid model definition {path}: {registry_error_message(exc)}"
                    ) from exc
        return cls(models)

    def get(self, provider: str, model: str) -> ModelDefinition:
        try:
            return self._models_by_provider_key[(provider, model)]
        except KeyError as exc:
            raise KeyError(f"unknown model: {provider}/{model}") from exc

    def get_by_id(self, model_id: str) -> ModelDefinition:
        try:
            return self._models_by_id[model_id]
        except KeyError as exc:
            raise KeyError(f"unknown model id: {model_id}") from exc

    def all(self) -> list[ModelDefinition]:
        return list(self._models_by_id.values())

    def resolve(
        self, task: TaskDefinition, *, allowed_providers: list[str] | None = None
    ) -> ModelDefinition:
        return self.route(task, allowed_providers=allowed_providers).model

    def route(
        self,
        task: TaskDefinition,
        *,
        allowed_providers: list[str] | None = None,
        allowed_model_ids: list[str] | None = None,
        model_override: str | None = None,
        estimated_input_tokens: int = 0,
        maximum_estimated_cost: Decimal | None = None,
        excluded_model_ids: Iterable[str] = (),
        attachments: Iterable[Attachment] = (),
        region_of_provider: Mapping[str, str] | None = None,
    ) -> RoutingDecision:
        excluded = set(excluded_model_ids)
        if excluded and not task.fallback_policy.allowed:
            raise RegistryValidationError(f"fallback is disabled for task {task.name}")
        if estimated_input_tokens > task.max_input_tokens:
            raise RegistryValidationError(f"input exceeds token budget for task {task.name}")
        if maximum_estimated_cost is not None and maximum_estimated_cost < 0:
            raise RegistryValidationError("maximum estimated cost must not be negative")
        attachments = list(attachments)

        candidates = [model for model in self.all() if self._eligible(task, model)]
        if attachments:
            candidates = [
                model for model in candidates if _model_can_carry_attachments(model, attachments)
            ]
        if allowed_providers is not None:
            candidates = [model for model in candidates if model.provider in allowed_providers]
        if allowed_model_ids is not None:
            candidates = [model for model in candidates if model.id in allowed_model_ids]
        if model_override is not None:
            if model_override not in self._models_by_id:
                raise RegistryValidationError(f"unknown model override: {model_override}")
            candidates = [model for model in candidates if model.id == model_override]
        candidates = [model for model in candidates if model.id not in excluded]

        if excluded and task.fallback_policy.prefer_same_provider:
            providers = {
                self._models_by_id[item].provider for item in excluded if item in self._models_by_id
            }
            candidates = [model for model in candidates if model.provider in providers]
        if excluded and not task.fallback_policy.allow_local:
            candidates = [model for model in candidates if model.provider != "local"]
        if excluded:
            # Cross-region fallback prohibition (v0.7 Scope §6.3 regional
            # amendment, ADR-0017): a configured fallback may move between
            # models but never implicitly to a different pinned region, and a
            # caller must not be able to bypass the rule by omitting the
            # region map. The region of the originally selected model(s) pins
            # the fallback candidate set: when the primary selection is pinned
            # (non-empty region), only candidates demonstrably in the same
            # region qualify — a provider with an unknown or empty region is
            # *not* eligible, because dispatching to it would move the request
            # to an unverifiable processing location. When the primary
            # selection is unpinned, fallback is unrestricted. If nothing
            # survives the constraint the route fails instead of silently
            # changing region.
            if region_of_provider is None:
                raise RegistryValidationError(
                    f"fallback for task {task.name} requires region_of_provider so "
                    "routing can never implicitly move a request across regions"
                )
            excluded_providers = {
                self._models_by_id[item].provider for item in excluded if item in self._models_by_id
            }
            if not excluded_providers.issubset(region_of_provider):
                raise RegistryValidationError(
                    f"fallback for task {task.name} requires every originally selected "
                    "provider's region to be declared in region_of_provider"
                )
            primary_regions = {region_of_provider[provider] for provider in excluded_providers} - {
                ""
            }
            if primary_regions:
                candidates = [
                    model
                    for model in candidates
                    if region_of_provider.get(model.provider) in primary_regions
                ]

        preference = {model_id: index for index, model_id in enumerate(task.model_preferences)}
        candidates.sort(
            key=lambda model: (preference.get(model.id, len(preference)), model.priority, model.id)
        )
        cost_limit = maximum_estimated_cost
        if task.max_estimated_cost is not None:
            cost_limit = (
                min(cost_limit, task.max_estimated_cost)
                if cost_limit is not None
                else task.max_estimated_cost
            )
        for model in candidates:
            cost = estimate_maximum_cost(task, model, estimated_input_tokens)
            if cost_limit is not None and cost > cost_limit:
                continue
            fallback_used = bool(excluded)
            reason = (
                f"organisation override {model.id}"
                if model_override
                else f"ordered fallback to {model.id}"
                if fallback_used
                else f"first eligible configured model {model.id}"
            )
            return RoutingDecision(
                model=model,
                reason=reason,
                fallback_used=fallback_used,
                estimated_input_tokens=estimated_input_tokens,
                estimated_max_cost=cost,
            )
        if attachments:
            raise RegistryValidationError(
                f"no model can carry the supplied attachments for task {task.name}"
            )
        raise RegistryValidationError(f"no model satisfies task {task.name}")

    @staticmethod
    def _eligible(task: TaskDefinition, model: ModelDefinition) -> bool:
        output_tokens = int(task.parameter_defaults.get("max_tokens", 0))
        return (
            model.available
            and set(task.required_capabilities).issubset(model.capabilities)
            and set(task.parameter_defaults).issubset(model.supported_parameters)
            and _QUALITY_RANK[model.quality_tier] >= _QUALITY_RANK[task.quality_tier]
            and model.latency_tier == task.latency_tier
            and task.max_input_tokens + output_tokens <= model.context_window
        )


class RegistryBundle(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    tasks: FileTaskRegistry
    prompts: FilePromptRegistry
    models: CapabilityCostModelRegistry


def load_registry_bundle(root: Path | None = None) -> RegistryBundle:
    ai_root = root or Path(__file__).resolve().parent
    bundle = RegistryBundle(
        tasks=FileTaskRegistry.from_directory(ai_root / "tasks"),
        prompts=FilePromptRegistry.from_directory(ai_root / "prompts"),
        models=CapabilityCostModelRegistry.from_directory(ai_root / "models"),
    )
    validate_registry_bundle(bundle)
    return bundle


def validate_registry_bundle(bundle: RegistryBundle) -> None:
    if not bundle.tasks.all():
        raise RegistryValidationError("task registry is empty")
    if not bundle.prompts.all():
        raise RegistryValidationError("prompt registry is empty")
    if not bundle.models.all():
        raise RegistryValidationError("model registry is empty")
    for task in bundle.tasks.all():
        try:
            prompt = bundle.prompts.get(task.prompt_name, task.prompt_version)
        except KeyError as exc:
            raise RegistryValidationError(
                f"task {task.name} references missing prompt {task.prompt_name} v{task.prompt_version}"
            ) from exc
        if set(prompt.input_variables) != set(task.input_variables):
            raise RegistryValidationError(f"task/prompt variables differ for {task.name}")
        expected_contract = task.output_schema or "text"
        if expected_contract != prompt.output_contract:
            raise RegistryValidationError(f"task/prompt output contract differs for {task.name}")
        if task.output_schema is not None:
            resolve_output_schema(task.output_schema)
        for model_id in task.model_preferences:
            try:
                bundle.models.get_by_id(model_id)
            except KeyError as exc:
                raise RegistryValidationError(
                    f"task {task.name} references unknown preferred model {model_id}"
                ) from exc
            bundle.models.route(
                task,
                model_override=model_id,
                estimated_input_tokens=task.max_input_tokens,
            )
        bundle.models.route(task, estimated_input_tokens=task.max_input_tokens)
    for model in bundle.models.all():
        if Capability.DOCUMENTS not in model.capabilities:
            continue
        # Truthful provider/model MIME capabilities (v0.7 Scope §6.3
        # attachment amendment): a model's declared MIME set must be one the
        # provider's adapter can actually carry in its native wire format, or
        # the router could route a document the adapter would have to reject.
        adapter_types = PROVIDER_INLINE_ATTACHMENT_MIME_TYPES.get(model.provider)
        if adapter_types is None:
            raise RegistryValidationError(
                f"model {model.id} provider {model.provider!r} declares no inline "
                "attachment MIME capability"
            )
        declared = set(model.attachment_mime_types or ())
        if not declared <= adapter_types:
            raise RegistryValidationError(
                f"model {model.id} declares attachment MIME types its provider cannot "
                f"carry inline: {sorted(declared - adapter_types)}"
            )

    # v0.8 Scope §2.2/§6.1: registry declarations must stay consistent with the
    # re-verified provider transfer contracts. A model declaring a non-inline
    # mode must name a mode, MIME set and ceiling its provider's reviewed
    # contract actually supports; a task may only declare a non-inline mode
    # that at least one registered model can realise, so an unreachable mode
    # declaration fails fast at startup/CI instead of failing at dispatch.
    contracts = load_transfer_contracts()
    for model in bundle.models.all():
        non_inline_modes = [
            mode for mode in model.allowed_transfer_modes if mode is not TransferMode.INLINE
        ]
        if not non_inline_modes:
            continue
        provider_contract = contracts.providers.get(model.provider)
        if provider_contract is None:
            raise RegistryValidationError(
                f"model {model.id} provider {model.provider!r} declares non-inline transfer "
                "modes but has no provider transfer contract"
            )
        for mode in non_inline_modes:
            mode_contract = provider_contract.transfer_modes.get(mode)
            if mode_contract is None:
                raise RegistryValidationError(
                    f"model {model.id} declares transfer mode {mode.value!r} that provider "
                    f"{model.provider!r} does not support"
                )
            limits = model.transfer_mode_limits[mode]
            declared_mime = set(limits.mime_types)
            if not declared_mime <= set(mode_contract.mime_types):
                raise RegistryValidationError(
                    f"model {model.id} declares non-inline MIME types its provider cannot "
                    f"carry in mode {mode.value!r}: {sorted(declared_mime - set(mode_contract.mime_types))}"
                )
            if limits.max_bytes > mode_contract.max_bytes:
                raise RegistryValidationError(
                    f"model {model.id} declares max_bytes {limits.max_bytes} above the "
                    f"provider ceiling {mode_contract.max_bytes} for mode {mode.value!r}"
                )
            if mode is TransferMode.STORAGE_REFERENCE and not mode_contract.same_region_required:
                raise RegistryValidationError(
                    f"model {model.id} declares storage_reference but provider "
                    f"{model.provider!r} has no same-region staging contract"
                )
    for task in bundle.tasks.all():
        for mode in task.allowed_transfer_modes:
            if mode is TransferMode.INLINE:
                continue
            supported_by = [
                model
                for model in bundle.models.all()
                if mode in model.allowed_transfer_modes
                and _model_supports_transfer_mode(model, mode, contracts)
            ]
            if not supported_by:
                raise RegistryValidationError(
                    f"task {task.name} declares transfer mode {mode.value!r} but no "
                    "registered model supports it"
                )


def _model_supports_transfer_mode(
    model: ModelDefinition, mode: TransferMode, contracts: TransferContracts
) -> bool:
    """Whether one model can carry a non-inline transfer mode under its own
    declarations and its provider's reviewed contract (v0.8 Scope §2.2).

    Re-runs the same checks as the bundle validator so the task-level
    "realisable by at least one model" test and the model-level contract test
    can never disagree."""
    if Capability.DOCUMENTS not in model.capabilities:
        return False
    limits = model.transfer_mode_limits.get(mode)
    if limits is None:
        return False
    provider_contract = contracts.providers.get(model.provider)
    if provider_contract is None:
        return False
    mode_contract = provider_contract.transfer_modes.get(mode)
    if mode_contract is None:
        return False
    if not set(limits.mime_types) <= set(mode_contract.mime_types):
        return False
    if limits.max_bytes > mode_contract.max_bytes:
        return False
    if mode is TransferMode.STORAGE_REFERENCE:
        return mode_contract.same_region_required
    return True


def _model_can_carry_attachments(model: ModelDefinition, attachments: Sequence[Attachment]) -> bool:
    """Whether one model can carry an attachment set under its reviewed
    declarations: it must declare the ``documents`` capability, both inline
    ceilings and a MIME set covering every attachment's type, each file must
    fit the per-file ceiling and the combined size must fit the combined
    ceiling. Image attachments additionally require the ``vision`` capability,
    so an image can never route to a documents-only model before dispatch
    (v0.7 Scope §6.2/§6.3 amendment)."""
    if Capability.DOCUMENTS not in model.capabilities:
        return False
    if model.max_attachment_bytes is None or model.max_total_attachment_bytes is None:
        return False
    if any(attachment.size > model.max_attachment_bytes for attachment in attachments):
        return False
    if sum(attachment.size for attachment in attachments) > model.max_total_attachment_bytes:
        return False
    supported_mime_types = model.attachment_mime_types or ()
    if any(attachment.mime_type not in supported_mime_types for attachment in attachments):
        return False
    return not any(
        attachment.is_image and Capability.VISION not in model.capabilities
        for attachment in attachments
    )


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + CHARS_PER_ESTIMATED_TOKEN - 1) // CHARS_PER_ESTIMATED_TOKEN)


def estimate_maximum_cost(
    task: TaskDefinition, model: ModelDefinition, estimated_input_tokens: int
) -> Decimal:
    output_tokens = int(task.parameter_defaults.get("max_tokens", 0))
    return (
        model.pricing.input_price_per_million_tokens * Decimal(estimated_input_tokens)
        + model.pricing.output_price_per_million_tokens * Decimal(output_tokens)
    ) / Decimal(1_000_000)


def _validate_variable_names(values: list[str], *, context: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {context} input variable")
    for value in values:
        if not _VARIABLE_NAME.fullmatch(value):
            raise ValueError(f"unsafe {context} input variable: {value!r}")
        if _SECRET_NAME.search(value):
            raise ValueError(f"secret-like {context} input variable is forbidden: {value!r}")


def _template_variables(template: str) -> set[str]:
    variables: set[str] = set()
    try:
        parsed = string.Formatter().parse(template)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if not _VARIABLE_NAME.fullmatch(field_name) or format_spec or conversion:
                raise ValueError(f"unsafe prompt placeholder: {field_name!r}")
            if _SECRET_NAME.search(field_name):
                raise ValueError(f"secret-like prompt placeholder is forbidden: {field_name!r}")
            variables.add(field_name)
    except ValueError as exc:
        raise ValueError(f"invalid prompt template: {exc}") from exc
    return variables


def _yaml_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise RegistryValidationError(f"registry directory does not exist: {directory}")
    files = sorted((*directory.rglob("*.yaml"), *directory.rglob("*.yml")))
    if not files:
        raise RegistryValidationError(f"registry directory contains no YAML files: {directory}")
    return files


def _read_yaml(path: Path) -> object:
    if path.stat().st_size > MAX_REGISTRY_FILE_BYTES:
        raise RegistryValidationError(f"registry file is too large: {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RegistryValidationError(f"cannot read registry YAML: {path}") from exc


def _read_yaml_mapping(path: Path) -> Mapping[str, object]:
    raw = _read_yaml(path)
    if not isinstance(raw, Mapping):
        raise RegistryValidationError(f"registry document must be a mapping: {path}")
    return cast(Mapping[str, object], raw)


def resolve_output_schema(path: str) -> type[BaseModel]:
    """Resolve an allowlisted dotted path to a Pydantic output model."""
    if not path.startswith(ALLOWED_SCHEMA_PREFIXES):
        raise RegistryValidationError("output schema is outside the allowlisted AI schema package")
    module_name, _, attribute = path.rpartition(".")
    try:
        module = importlib.import_module(module_name)
        schema = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise RegistryValidationError(f"cannot resolve output schema: {path}") from exc
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise RegistryValidationError(f"output schema is not a Pydantic model: {path}")
    return schema


def registry_error_message(exc: ValidationError) -> str:
    """Keep Pydantic's location detail while avoiding registry payload echo."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )
