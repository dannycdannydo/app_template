"""In-memory registry implementations for AI tests (v0.7 Scope §6.1).

Scope §6.1 defines the registry *interfaces*; the checked-in YAML/JSON-backed
registries and the capability/cost router ship in Scope §6.2. These in-memory
implementations let the service contract tests exercise the full flow with
deterministic data, mirroring how storage/email tests use the fake adapters.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.ai.registry import (
    Capability,
    ModelDefinition,
    ModelRegistry,
    PricingBasis,
    PromptDefinition,
    PromptRegistry,
    RetryPolicy,
    TaskDefinition,
    TaskRegistry,
)

_FAKE_MODEL = "fake-model-document.classify"
_FAKE_PROMPT = "classify"
_FAKE_TASK = "document.classify"


class InMemoryTaskRegistry(TaskRegistry):
    def __init__(self, tasks: dict[str, TaskDefinition] | None = None) -> None:
        self._tasks = tasks if tasks is not None else {}

    def register(self, task: TaskDefinition) -> None:
        """Insert or replace one task (test convenience)."""
        self._tasks[task.name] = task

    def get(self, name: str) -> TaskDefinition:
        try:
            return self._tasks[name]
        except KeyError as exc:
            raise KeyError(f"unknown task: {name}") from exc

    def all(self) -> list[TaskDefinition]:
        return list(self._tasks.values())


class InMemoryPromptRegistry(PromptRegistry):
    def __init__(self, prompts: dict[tuple[str, int], PromptDefinition] | None = None) -> None:
        self._prompts = prompts if prompts is not None else {}

    def get(self, name: str, version: int) -> PromptDefinition:
        try:
            return self._prompts[(name, version)]
        except KeyError as exc:
            raise KeyError(f"unknown prompt: {name} v{version}") from exc

    def all(self) -> list[PromptDefinition]:
        return list(self._prompts.values())


class InMemoryModelRegistry(ModelRegistry):
    """Deterministic resolver: picks the task's default model when it is
    available and allowed, else the first available model that satisfies the
    task's hard capabilities."""

    def __init__(self, models: list[ModelDefinition] | None = None) -> None:
        self._models = models if models is not None else []

    def get(self, provider: str, model: str) -> ModelDefinition:
        for definition in self._models:
            if definition.provider == provider and definition.model == model:
                return definition
        raise KeyError(f"unknown model: {provider}/{model}")

    def all(self) -> list[ModelDefinition]:
        return list(self._models)

    def resolve(
        self,
        task: TaskDefinition,
        *,
        allowed_providers: list[str] | None = None,
    ) -> ModelDefinition:
        required = set(task.required_capabilities)
        for definition in self._models:
            if not definition.available:
                continue
            if allowed_providers is not None and definition.provider not in allowed_providers:
                continue
            if not required.issubset(set(definition.capabilities)):
                continue
            return definition
        raise ValueError(f"no model satisfies task {task.name}")


def default_task() -> TaskDefinition:
    return TaskDefinition(
        name=_FAKE_TASK,
        prompt_name=_FAKE_PROMPT,
        prompt_version=1,
        input_variables=["document_id"],
        required_capabilities=[Capability.STRUCTURED_OUTPUT],
        output_schema="demo.ClassificationResult",
        retry_policy=RetryPolicy(max_attempts=3, repair_attempts=1),
    )


def default_prompt() -> PromptDefinition:
    return PromptDefinition(
        name=_FAKE_PROMPT,
        version=1,
        system_instructions="You extract structured facts from documents.",
        input_variables=["document_id"],
        user_template="Classify document {document_id}.",
        output_contract="demo.ClassificationResult",
    )


def default_model() -> ModelDefinition:
    return ModelDefinition(
        id="fake.document-classifier",
        provider="fake",
        model=_FAKE_MODEL,
        capabilities=[Capability.STRUCTURED_OUTPUT, Capability.REASONING],
        context_window=128_000,
        supported_parameters=[],
        pricing=PricingBasis(
            currency="USD",
            input_price_per_million_tokens=Decimal("1.00"),
            output_price_per_million_tokens=Decimal("2.00"),
            effective_date=date(2026, 1, 1),
            owner="template tests",
        ),
    )


class InMemoryRegistries:
    """Bundle of the three registries preloaded with the demo task.

    ``models`` is typed as :class:`ModelRegistry` so tests may substitute a
    registry with custom pricing/routing behaviour.
    """

    def __init__(
        self,
        *,
        tasks: InMemoryTaskRegistry | None = None,
        prompts: InMemoryPromptRegistry | None = None,
        models: ModelRegistry | None = None,
    ) -> None:
        self.tasks = tasks or InMemoryTaskRegistry()
        self.prompts = prompts or InMemoryPromptRegistry()
        self.models = models or InMemoryModelRegistry()

    @classmethod
    def default(cls) -> InMemoryRegistries:
        return cls(
            tasks=InMemoryTaskRegistry({_FAKE_TASK: default_task()}),
            prompts=InMemoryPromptRegistry({(_FAKE_PROMPT, 1): default_prompt()}),
            models=InMemoryModelRegistry([default_model()]),
        )
