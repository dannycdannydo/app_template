"""Request/response schemas for the ``document.classify`` demonstration
(v0.7 Scope §6.6, blueprint §12).

The demonstration is intentionally narrow: it exposes the one checked-in
non-product task and no generic arbitrary-prompt surface (v0.7 Scope §6.6).
Synchronous text input returns the validated result inline; a private storage
reference enqueues the durable ``ai.execute`` job and returns the ids the
caller polls. Every response carries only safe metadata — routing decisions,
token/cost summaries and the validated classification — never prompts, document
content, provider credentials or raw provider responses (BP §28, ADR-0017).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ai.tasks.schemas import DocumentClassificationResult

#: The finite set of durable request statuses a generated client can observe
#: (v0.7 Scope §6.5/§6.6): ``queued`` before the worker dispatches, ``running``
#: during dispatch, ``succeeded``/``failed`` terminal.
ClassifyStatus = Literal["queued", "running", "succeeded", "failed"]


class ClassifyRouting(BaseModel):
    """Safe routing metadata: which provider/model/prompt served the request."""

    provider: str
    model: str
    prompt_name: str
    prompt_version: int
    fallback_used: bool
    region: str = ""


class ClassifyUsage(BaseModel):
    """Provider-normalised token usage for one execution."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ClassifyCost(BaseModel):
    """Calculated cost for one execution (BP §10 NUMERIC precision in storage)."""

    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    currency: str = Field(min_length=3, max_length=3)


class DocumentClassifyRequest(BaseModel):
    """One classification submission: a private storage reference.

    ``sync=True`` resolves the reference to a bounded attachment and runs
    synchronously within the documented input/time limits; ``sync=False``
    (default) enqueues the durable ``ai.execute`` job. The worker re-reads the
    object on every attempt — the reference is never trusted as content
    (ADR-0017).
    """

    model_config = ConfigDict(extra="forbid")

    storage_reference: str = Field(max_length=1024)
    sync: bool = False


class DocumentClassifySyncResponse(BaseModel):
    """The synchronous classification result (200)."""

    request_id: str
    output: DocumentClassificationResult
    routing: ClassifyRouting
    usage: ClassifyUsage
    cost: ClassifyCost
    completed_at: datetime


class DocumentClassifyAcceptedResponse(BaseModel):
    """The durable-job acknowledgement (202) for document-scale input."""

    job_id: str
    request_id: str
    status: ClassifyStatus = "queued"


class DocumentClassifyResultResponse(BaseModel):
    """The durable record of one classification (synchronous or queued).

    ``output`` is present only when the organisation's retention policy and the
    task-level opt-in both permitted content retention (v0.7 Scope §2); by
    default the record carries status and safe routing/usage only, never
    sensitive source content (BP §28, ADR-0017).
    """

    request_id: str
    status: ClassifyStatus
    error_code: str | None = None
    output: DocumentClassificationResult | None = None
    routing: ClassifyRouting | None = None
    usage: ClassifyUsage | None = None
    cost: ClassifyCost | None = None
    completed_at: datetime | None = None


# The bounded question length mirrors AI_METADATA_MAX_VALUE_LENGTH: the
# question travels to the AI layer as a bounded metadata variable so the
# feature-facing AIRequest contract stays unchanged (v0.8 Scope §2.2).
ASK_QUESTION_MAX_LENGTH = 512


class DocumentAskRequest(BaseModel):
    """One QA submission: a private storage reference plus a bounded question.

    ``sync=true`` is the only path for the demonstration: the reference is
    resolved to a bounded attachment (or, above the inline threshold, staged
    through the Vertex private GCS path) and the answer is returned inline.
    The question is bounded to the AI metadata value limit (v0.8 Scope §2.2).
    """

    model_config = ConfigDict(extra="forbid")

    storage_reference: str = Field(max_length=1024)
    question: str = Field(min_length=1, max_length=ASK_QUESTION_MAX_LENGTH)


class DocumentAskResponse(BaseModel):
    """The synchronous answer (200) with safe routing/usage metadata."""

    request_id: str
    output: str
    routing: ClassifyRouting
    usage: ClassifyUsage
    cost: ClassifyCost
    completed_at: datetime
