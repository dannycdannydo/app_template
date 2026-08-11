"""AI error taxonomy (v0.7 Scope §6.1, ADR-0017).

Every AI failure surfaces as an :class:`AIError` with a safe, stable
``error_code`` and a generic message that never embeds provider output,
prompts or document content (BP §28 never-log list, ADR-0017). Codes are the
safe-error vocabulary the durable ``ai_requests`` record stores (Scope §6.5);
``retryable`` drives the bounded retry/repair policy (Scope §6.4) — transient
provider failures retry, permanent validation/policy failures never do.
"""

from __future__ import annotations


class AIError(Exception):
    """Base class for every error raised by the AI layer.

    ``error_code`` is a stable machine-readable identifier suitable for
    storage and API envelopes; the message is safe to show to users and must
    never contain provider output, prompts, keys or document content.
    """

    error_code = "ai_error"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class AIInputValidationError(AIError):
    """The :class:`~app.ai.schemas.AIRequest` itself is invalid.

    Raised by :class:`~app.ai.service.AIService` before any dispatch — for
    example a request that declares no usable input, or a task whose declared
    variables the request does not satisfy.
    """

    error_code = "ai_input_invalid"


class TaskNotFoundError(AIError):
    """The named task does not exist in the task registry (Scope §6.2)."""

    error_code = "task_not_found"


class PromptNotFoundError(AIError):
    """The task's prompt name/version is missing from the prompt registry."""

    error_code = "prompt_not_found"


class ModelNotAvailableError(AIError):
    """No model can satisfy the task's requirements under current policy.

    Covers an empty resolution result, an organisation-disallowed provider/
    model, and a capability the available models do not provide (Scope §6.2
    router, Scope §6.5 organisation controls).
    """

    error_code = "model_not_available"


class AIUnavailableError(AIError):
    """AI is disabled for the organisation (default-off, Scope §6.5)."""

    error_code = "ai_unavailable"


class BudgetExceededError(AIError):
    """The request would exceed the organisation's monthly AI budget."""

    error_code = "budget_exceeded"


class AIRequestReplayError(AIError):
    """The caller re-used an execution id that already has a durable row.

    Provider work is not repeated: the durable job/result owner must return
    its previously persisted outcome instead (v0.7 Scope §6.5/§6.6).
    """

    error_code = "ai_request_already_exists"


class ProviderError(AIError):
    """Base class for provider-adapter failures (normalised taxonomy).

    ``retryable`` defaults to True for the transient subclasses below;
    permanent adapter failures (bad request from our side, unsupported
    parameters) use :class:`ProviderResponseError` and never retry.
    """

    error_code = "provider_error"


class ProviderUnavailableError(ProviderError):
    """The provider endpoint is unreachable or returned a server error."""

    error_code = "provider_unavailable"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ProviderRateLimitError(ProviderError):
    """The provider asked us to slow down (429 / equivalent)."""

    error_code = "provider_rate_limited"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ProviderTimeoutError(ProviderError):
    """The provider did not answer within the configured timeout."""

    error_code = "provider_timeout"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ProviderResponseError(ProviderError):
    """The provider returned something unusable (malformed body, refused
    parameters). Retrying the identical request is not expected to help.
    """

    error_code = "provider_response_invalid"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class OutputValidationError(AIError):
    """The provider output did not validate against the requested schema.

    Terminal for the current attempt: the task's bounded repair/retry policy
    (Scope §6.4) decides whether a repair request is attempted; unvalidated
    structured data is never returned as success.
    """

    error_code = "output_validation_failed"


class OutputSchemaError(AIError):
    """The task's declared output schema could not be resolved or is not a
    Pydantic model. Registry validation (Scope §6.2) catches this at
    startup/CI; the service keeps the same check so execution fails fast.
    """

    error_code = "output_schema_invalid"
