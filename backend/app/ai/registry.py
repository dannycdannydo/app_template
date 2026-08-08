"""Task, prompt and model registry interfaces (v0.7 Scope §6.1, ADR-0017).

The three registries are the checked-in configuration that makes the AI layer
provider-neutral: a task names a prompt version and required capabilities, the
prompt registry resolves the prompt, and the model registry/router resolves a
model that satisfies the task's hard requirements under organisation policy
(Scope §6.2). This module defines the typed records and the abstract registry
interfaces; the checked-in YAML/JSON-backed implementations and the
deterministic capability/cost router ship in Scope §6.2. Concrete registries
must validate duplicates, versions and references so a misconfiguration fails
fast at startup and in CI (acceptance criterion §5.2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Capability(StrEnum):
    """Capabilities a model may declare and a task may require."""

    STRUCTURED_OUTPUT = "structured_output"
    VISION = "vision"
    TOOLS = "tools"
    REASONING = "reasoning"


class PricingBasis(BaseModel):
    """Reviewed pricing metadata for one model (Scope §6.2).

    Prices are configuration, never scraped at runtime; every entry records an
    owner and an effective date so a price change is a reviewed, auditable
    change (acceptance criterion §5.9). Currency is ISO-4217.
    """

    currency: str = Field(default="USD", min_length=3, max_length=3)
    input_price_per_million_tokens: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    output_price_per_million_tokens: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    effective_date: date
    owner: str = Field(min_length=1, description="Person/team accountable for this pricing basis")


class RetryPolicy(BaseModel):
    """Bounded retry policy for one task (Scope §6.4).

    Transient provider errors may retry up to ``max_attempts`` total attempts;
    malformed output may trigger at most ``repair_attempts`` repair requests;
    permanent validation/policy failures never retry. Kept deliberately small
    so a misconfigured task cannot create a retry storm or unbounded cost.
    """

    max_attempts: int = Field(default=3, ge=1, le=10)
    repair_attempts: int = Field(default=1, ge=0, le=3)


class FallbackPolicy(BaseModel):
    """Whether and how the router may fall back (Scope §6.2).

    Fallback is explicit and reviewed: ``allowed`` gates provider/model
    fallback entirely, ``prefer_same_provider`` restricts cross-provider
    fallback (the router never silently falls back across a provider when a
    task or organisation disallows it), and ``allow_local`` gates fallback to
    the local/OpenAI-compatible adapter.
    """

    allowed: bool = False
    prefer_same_provider: bool = True
    allow_local: bool = False


class TaskDefinition(BaseModel):
    """One canonical, versioned task (Scope §6.2).

    Fields follow Scope §2: canonical name, prompt name/version, input
    variables, required capabilities, parameter defaults, output schema import
    path, retry and fallback policy. ``output_schema`` is a dotted import path
    to a Pydantic model; ``declares_text_result`` opts into free text (Scope
    §6.4) — by default a task is structured-output.
    """

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9._-]*$")
    prompt_name: str = Field(min_length=1, max_length=128)
    prompt_version: int = Field(ge=1)
    input_variables: list[str] = Field(default_factory=lambda: [])
    required_capabilities: list[Capability] = Field(default_factory=lambda: [])
    parameter_defaults: dict[str, Any] = Field(default_factory=dict)
    output_schema: str | None = Field(default=None, max_length=512)
    declares_text_result: bool = False
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)


class PromptDefinition(BaseModel):
    """One immutable, versioned prompt (Scope §6.2).

    Prompt versions are append-only: correcting a prompt creates a new
    ``*_vN`` file rather than editing a released version. ``system_instructions``
    is the static system text; ``input_variables`` must be a subset of the
    task's input variables; the template engine is a safe, allowlisted
    renderer (no arbitrary template execution, Scope §6.2).
    """

    name: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    system_instructions: str = Field(min_length=1)
    input_variables: list[str] = Field(default_factory=lambda: [])
    # Optional user-facing template containing {variable} placeholders; the
    # template is rendered with allowlisted variables only.
    user_template: str = ""


class ModelDefinition(BaseModel):
    """One model in the model registry (Scope §6.2).

    ``provider`` is the adapter/provider id, ``model`` the provider's model
    identifier, ``capabilities`` the capabilities the adapter actually
    supports (never pretended), ``context_window`` in tokens, and ``pricing``
    the reviewed pricing basis. ``available`` allows a reviewed temporary
    disable without deleting the definition.
    """

    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    capabilities: list[Capability] = Field(default_factory=lambda: [])
    context_window: int = Field(ge=1)
    pricing: PricingBasis
    available: bool = True


class TaskRegistry(ABC):
    """Registry of canonical task definitions (Scope §6.2)."""

    @abstractmethod
    def get(self, name: str) -> TaskDefinition:
        """Return the task with the given name; raise KeyError when unknown."""

    @abstractmethod
    def all(self) -> list[TaskDefinition]:
        """Return every registered task definition."""


class PromptRegistry(ABC):
    """Registry of versioned prompt definitions (Scope §6.2)."""

    @abstractmethod
    def get(self, name: str, version: int) -> PromptDefinition:
        """Return the prompt version; raise KeyError when unknown."""

    @abstractmethod
    def all(self) -> list[PromptDefinition]:
        """Return every registered prompt definition."""


class ModelRegistry(ABC):
    """Registry of model definitions and capability/cost routing (Scope §6.2).

    ``resolve`` is the deterministic router entry point: it returns a model
    that satisfies the task's hard capability requirements under the
    organisation's allowed providers/models and the task's fallback policy,
    or raises when none can. It never silently crosses a provider boundary
    the task/organisation disallows and never picks a model that cannot meet
    a hard requirement.
    """

    @abstractmethod
    def get(self, provider: str, model: str) -> ModelDefinition:
        """Return one model definition; raise KeyError when unknown."""

    @abstractmethod
    def all(self) -> list[ModelDefinition]:
        """Return every registered model definition."""

    @abstractmethod
    def resolve(self, task: TaskDefinition, *, allowed_providers: list[str] | None = None) -> ModelDefinition:
        """Resolve a model for the task under the given provider allowlist."""
