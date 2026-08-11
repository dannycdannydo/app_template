"""Typed AI request/result/error schemas (v0.7 Scope §6.1, ADR-0017).

``AIRequest`` and ``AIResult`` are the application-facing contract:
application code names a task and receives a validated result with safe
usage/cost/routing metadata. Neither type references a provider SDK or a
provider-specific model id directly (BP §33, ADR-0017). Organisation ids are
always the validated caller context (never client-supplied); metadata is
bounded and JSON-safe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

# Bounds for the request's JSON-safe metadata (Scope §6.1): a small fixed
# number of short keys/values so a request can carry document/feature/workflow
# identifiers without ever smuggling payloads into the AI layer.
AI_METADATA_MAX_ITEMS = 16
AI_METADATA_MAX_KEY_LENGTH = 64
AI_METADATA_MAX_VALUE_LENGTH = 512

MAX_TEXT_LENGTH = 64 * 1024  # 64 KiB of user text per synchronous request
MAX_MESSAGE_LENGTH = 8 * 1024


class ChatMessage(BaseModel):
    """One message in a conversation-style request.

    Free-form conversation is not the v0.7 focus (chat is deferred), but the
    request contract must support messages for task types that need them;
    ``role`` is restricted to the standard three so validation is strict.
    """

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


class AIRequest(BaseModel):
    """Everything a feature module may ask the AI layer to do.

    Exactly one input form must be supplied: ``text`` (small bounded input),
    ``messages`` (bounded conversation) or ``storage_reference`` (a private
    storage reference resolved by the feature, for document-scale work that
    belongs in a job — Scope §6.6). ``output_schema`` optionally overrides the
    task's declared schema (tasks carry the canonical one; see Scope §6.2).
    """

    task: str = Field(min_length=1, max_length=128)
    text: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    messages: list[ChatMessage] | None = Field(default=None, max_length=64)
    storage_reference: str | None = Field(default=None, max_length=1024)
    output_schema: str | None = Field(
        default=None,
        max_length=512,
        description="Optional dotted import path of a Pydantic model overriding the task's schema",
    )
    organisation_id: UUID
    user_id: UUID
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_input(self) -> AIRequest:
        supplied = [
            value
            for value in (self.text, self.messages, self.storage_reference)
            if value is not None
        ]
        if len(supplied) != 1:
            raise ValueError("exactly one of text, messages or storage_reference must be supplied")
        if self.messages is not None:
            total_length = sum(len(message.content) for message in self.messages)
            if total_length > MAX_TEXT_LENGTH:
                raise ValueError(f"message content must not exceed {MAX_TEXT_LENGTH} characters")
        return self

    @field_validator("output_schema")
    @classmethod
    def _normalise_empty_output_schema(cls, value: str | None) -> str | None:
        """Treat an empty-string override as no override (Scope §6.1)."""
        return value or None

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, metadata: dict[str, str]) -> dict[str, str]:
        if len(metadata) > AI_METADATA_MAX_ITEMS:
            raise ValueError(f"metadata must not exceed {AI_METADATA_MAX_ITEMS} items")
        for key, value in metadata.items():
            if len(key) > AI_METADATA_MAX_KEY_LENGTH:
                raise ValueError(f"metadata key too long: {key!r}")
            if len(value) > AI_METADATA_MAX_VALUE_LENGTH:
                raise ValueError(f"metadata value too long for key {key!r}")
        return metadata


class TokenUsage(BaseModel):
    """Provider-normalised token counts for one execution."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class CostEstimate(BaseModel):
    """Calculated cost for one execution, from the model's pricing basis
    (Scope §6.2 registry) and the actual usage. ``NUMERIC`` precision lives in
    the persisted records (Scope §6.5); the estimate uses ``Decimal``.
    """

    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class RoutingMetadata(BaseModel):
    """Which task/prompt/model produced this result and why.

    ``reason`` is the deterministic router's decision summary (Scope §6.2:
    capability match, organisation override, configured fallback); it is
    stored and audited but must never contain prompt or document content.
    ``region`` is the adapter's configured deployment region (Scope §6.3
    regional amendment) — empty where the provider has no template-controlled
    pinning — so deployments can verify a request never silently changed
    region.
    """

    task: str
    provider: str
    model: str
    prompt_name: str
    prompt_version: int
    reason: str = ""
    fallback_used: bool = False
    region: str = ""


class AIResult(BaseModel):
    """The only result type returned by :class:`~app.ai.service.AIService`.

    ``output`` is validated structured data (a ``dict``) when the task declares
    an output schema, or plain text when the task explicitly declares a text
    result (Scope §6.4). Invalid data is never returned here as success.
    """

    request_id: str
    routing: RoutingMetadata
    output: Any
    usage: TokenUsage
    cost: CostEstimate
    completed_at: datetime

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class AIErrorResult(BaseModel):
    """Safe, serialisable summary of a failed execution.

    Carries the error code for the durable ``ai_requests`` record (Scope
    §6.5); never carries content, prompts or stack traces (BP §28).
    """

    request_id: str
    task: str
    error_code: str
    retryable: bool = False
    error_message: str = ""
