"""Demonstration feature service for ``document.classify`` (v0.7 Scope §6.6).

This module is the example *consumer* of the provider-neutral AI platform
package: it is a feature module (not part of ``app/ai/``), so it keeps its own
routes, request/response schemas and permission gate, and calls
``AIService.execute`` — never a provider SDK, model id or the persistence layer
directly (ADR-0017, BP §4/§5). It proves the end-to-end seam a derived
application follows: task name → prompt → routing → provider → validation →
tracking/audit → result/job flow (v0.7 Scope §1).

Two execution paths share one storage-reference input (v0.7 Scope §2):

- ``sync=True`` resolves the private ``storage_reference`` to a bounded
  attachment and runs **synchronously** inside the request through
  ``AIService.execute`` (within the documented input/time limits); and
- ``sync=False`` (default) enqueues the durable ``ai.execute`` job on the
  ``ai`` queue — a ``queued`` AI request row and the durable job row are
  persisted together before the broker message, and the message carries no
  bytes (v0.7 Scope §5.8).

AI failures are translated into the standard API error taxonomy here so the
router stays thin (BP §13): every :class:`~app.ai.errors.AIError` becomes a
safe :class:`~app.core.exceptions.APIError` with a stable code and a generic
message, never embedding provider output, prompts or document content.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.errors import AIError
from app.ai.execution import (
    enqueue_document_classification,
    execute_managed_ai,
    get_ai_execution_snapshot,
)
from app.ai.schemas import AIRequest
from app.ai.tasks.schemas import DocumentClassificationResult
from app.core.exceptions import (
    APIError,
    ExternalServiceError,
    NotFoundError,
    RateLimitExceeded,
    ServiceUnavailableError,
    ValidationError,
)
from app.db.conventions import uuid7
from app.modules.ai_demo.schemas import (
    ClassifyCost,
    ClassifyRouting,
    ClassifyUsage,
    DocumentAskResponse,
    DocumentClassifyAcceptedResponse,
    DocumentClassifyResultResponse,
    DocumentClassifySyncResponse,
)
from app.modules.users.models import User
from app.storage import get_storage

#: The single demonstrated task (kept in sync with ``app.ai.execution``).
DEMO_TASK = "document.classify"

#: The document QA demonstration task (v0.8 Scope §2.2, §6.4): a bounded
#: question plus a private PDF, inline at or below the 5,000,000-byte
#: threshold and through the Vertex private GCS staging path above it.
ASK_TASK = "document.ask"

#: Each AI error code maps to one HTTP-shaped API error so the router never
#: handles AI taxonomy itself. Messages stay generic and safe (BP §28) and
#: take a subject noun (classification / question) so both demonstrations
#: surface accurate wording without duplicating the taxonomy.
_AI_ERROR_MAP: dict[str, APIError] = {
    "ai_unavailable": ServiceUnavailableError(
        code="ai_unavailable", message="AI is not enabled for this organisation."
    ),
    "budget_exceeded": ValidationError(
        code="budget_exceeded", message="The organisation's AI budget is exhausted."
    ),
    "ai_input_invalid": ValidationError(
        code="ai_input_invalid", message="The {subject} request input is invalid."
    ),
    "output_validation_failed": ValidationError(
        code="output_validation_failed",
        message="The provider output could not be validated after bounded retries.",
    ),
    "model_not_available": ServiceUnavailableError(
        code="model_not_available",
        message="No model is available to serve this {subject} right now.",
    ),
    "task_not_found": NotFoundError(
        code="task_not_found", message="The {subject} task is not configured."
    ),
    "prompt_not_found": NotFoundError(
        code="prompt_not_found", message="The {subject} prompt is not configured."
    ),
    "output_schema_invalid": ServiceUnavailableError(
        code="output_schema_invalid",
        message="The {subject} output schema is misconfigured.",
    ),
    "provider_unavailable": ServiceUnavailableError(
        code="provider_unavailable", message="The AI provider is unavailable."
    ),
    "provider_rate_limited": RateLimitExceeded(
        code="provider_rate_limited", message="The AI provider rate limited the request."
    ),
    "provider_timeout": ServiceUnavailableError(
        code="provider_timeout", message="The AI provider did not respond in time."
    ),
    "provider_response_invalid": ExternalServiceError(
        code="provider_response_invalid", message="The AI provider returned an unusable response."
    ),
    "provider_error": ExternalServiceError(
        code="provider_error", message="The AI provider failed."
    ),
}


def _translate_ai_error(exc: AIError, *, subject: str = "classification") -> APIError:
    """Map one AI taxonomy error to its HTTP-shaped API error (BP §13)."""
    mapped = _AI_ERROR_MAP.get(exc.error_code)
    if mapped is not None:
        # Rebuild the mapped error with the subject-aware message; a fresh
        # instance per request so the shared map is never mutated.
        return type(mapped)(code=mapped.code, message=mapped.message.format(subject=subject))
    return ServiceUnavailableError(
        code=exc.error_code, message=f"The {subject} could not be completed."
    )


def _classification_output(output: object) -> DocumentClassificationResult:
    """Coerce the validated AI output to the demonstration schema."""
    if isinstance(output, DocumentClassificationResult):
        return output
    if isinstance(output, dict):
        return DocumentClassificationResult.model_validate(output)
    raise ServiceUnavailableError(
        code="output_validation_failed",
        message="The classification output was not in the expected shape.",
    )


def _validate_storage_reference(storage_reference: str, organisation_id: uuid.UUID) -> None:
    """Reject a storage reference outside the caller's organisation namespace."""
    if not storage_reference.startswith(f"organisations/{organisation_id}/"):
        raise ValidationError(
            code="invalid_storage_reference",
            message="The storage reference is not accessible to this organisation.",
        )


async def classify_sync(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user: User,
    storage_reference: str,
) -> DocumentClassifySyncResponse:
    """Run the classification synchronously and return the validated result.

    The private ``storage_reference`` is resolved to a bounded provider-neutral
    attachment inside ``AIService.execute`` (v0.7 Scope §2): the service never
    renders the reference as if it were document content.
    """
    _validate_storage_reference(storage_reference, organisation_id)
    try:
        result = await execute_managed_ai(
            session,
            AIRequest(
                task=DEMO_TASK,
                storage_reference=storage_reference,
                organisation_id=organisation_id,
                user_id=user.id,
                metadata={"source": "ai_demo"},
            ),
        )
    except AIError as exc:
        raise _translate_ai_error(exc) from exc
    return DocumentClassifySyncResponse(
        request_id=result.request_id,
        output=_classification_output(result.output),
        routing=ClassifyRouting(
            provider=result.routing.provider,
            model=result.routing.model,
            prompt_name=result.routing.prompt_name,
            prompt_version=result.routing.prompt_version,
            fallback_used=result.routing.fallback_used,
            region=result.routing.region,
        ),
        usage=ClassifyUsage(
            input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens
        ),
        cost=ClassifyCost(amount=result.cost.amount, currency=result.cost.currency),
        completed_at=result.completed_at,
    )


async def enqueue_classify(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user: User,
    storage_reference: str,
) -> DocumentClassifyAcceptedResponse:
    """Persist the durable job + queued AI request, then enqueue (202 path).

    The AI platform execution boundary writes a ``queued`` AI request row and
    durable job row in the same transaction before publishing the broker
    message (v0.7 Scope §5.8). The feature never imports persistence models or
    query statements. The request id is derived deterministically from the job
    id, and the result endpoint is coherent immediately after the ``202``.
    """
    _validate_storage_reference(storage_reference, organisation_id)
    queued = await enqueue_document_classification(
        session,
        organisation_id=organisation_id,
        user_id=user.id,
        storage_reference=storage_reference,
    )
    return DocumentClassifyAcceptedResponse(
        job_id=str(queued.job_id),
        request_id=queued.request_id,
    )


async def get_classify_result(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    request_id: str,
) -> DocumentClassifyResultResponse:
    """Return the durable record of one classification (org-scoped).

    The winning (succeeded) attempt is preferred; if none succeeded yet, the
    latest attempt's status is the execution-level outcome — ``queued`` before
    the worker dispatches, ``running`` during dispatch, or ``failed`` if every
    attempt failed (v0.7 Scope §6.4/§6.6). A foreign or unknown request id is a
    404 — indistinguishable from missing (BP §9).
    """
    record = await get_ai_execution_snapshot(
        session,
        organisation_id=organisation_id,
        request_id=request_id,
    )
    output: DocumentClassificationResult | None = None
    if record.status == "succeeded" and record.output is not None:
        output = DocumentClassificationResult.model_validate(record.output)
    routing: ClassifyRouting | None = None
    usage: ClassifyUsage | None = None
    cost: ClassifyCost | None = None
    if record.status == "succeeded":
        routing = ClassifyRouting(
            provider=record.provider or "",
            model=record.model or "",
            prompt_name=record.prompt_name or "",
            prompt_version=record.prompt_version or 0,
            fallback_used=record.fallback_used,
            region=record.region,
        )
        usage = ClassifyUsage(input_tokens=record.input_tokens, output_tokens=record.output_tokens)
        cost = ClassifyCost(amount=record.cost, currency="USD")
    return DocumentClassifyResultResponse(
        request_id=record.request_id,
        status=record.status,
        error_code=record.error_code,
        output=output,
        routing=routing,
        usage=usage,
        cost=cost,
        completed_at=record.completed_at,
    )


async def ask_sync(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user: User,
    storage_reference: str,
    question: str,
) -> DocumentAskResponse:
    """Run one document QA request synchronously and return the answer.

    The private ``storage_reference`` is resolved by ``AIService`` itself: a
    PDF at or below the inline threshold becomes a bounded inline attachment,
    and a larger PDF is streamed bounded into a non-inline staging path — the
    Vertex private GCS bucket or the OpenAI Files API upload — before dispatch
    (v0.8 Scope §2.2/§2.4/§6.5). The bounded question travels as a metadata
    variable so the feature-facing ``AIRequest`` contract stays unchanged; the
    answer is validated text, never unvalidated provider output (v0.7 Scope
    §6.4).
    """
    _validate_storage_reference(storage_reference, organisation_id)
    try:
        result = await execute_managed_ai(
            session,
            AIRequest(
                task=ASK_TASK,
                storage_reference=storage_reference,
                organisation_id=organisation_id,
                user_id=user.id,
                metadata={"question": question, "source": "ai_demo"},
            ),
        )
    except AIError as exc:
        raise _translate_ai_error(exc, subject="question") from exc
    if not isinstance(result.output, str) or not result.output:
        raise ServiceUnavailableError(
            code="output_validation_failed",
            message="The answer output was not in the expected shape.",
        )
    return DocumentAskResponse(
        request_id=result.request_id,
        output=result.output,
        routing=ClassifyRouting(
            provider=result.routing.provider,
            model=result.routing.model,
            prompt_name=result.routing.prompt_name,
            prompt_version=result.routing.prompt_version,
            fallback_used=result.routing.fallback_used,
            region=result.routing.region,
        ),
        usage=ClassifyUsage(
            input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens
        ),
        cost=ClassifyCost(amount=result.cost.amount, currency=result.cost.currency),
        completed_at=result.completed_at,
    )


#: The transient upload ceiling mirrors the AI large-file template ceiling
#: (v0.8 Scope §2.2): the demo's scratch path carries exactly one PDF of at
#: most the 50,000,000-byte large-file ceiling, so an intent never signs a
#: PUT URL for bytes the AI layer would then refuse.
#:
#: The organisation-scoped AI scratch namespace is re-declared here rather
#: than imported from ``app.ai.transfer`` (v0.8 Scope §6.1 checkbox 3 import
#: boundary — feature modules never import the transfer contract module). It
#: mirrors the AI layer's own ``SCRATCH_KEY_TEMPLATE`` classifier: objects
#: under this prefix are classified as transient sources, so a >5 MB PDF
#: uploaded here routes through the provider-upload mode. The demo test suite
#: pins this exact format against the ask flow.
SCRATCH_KEY_TEMPLATE = "organisations/{organisation_id}/ai/scratch/"


def _validate_scratch_upload(*, content_type: str, size_bytes: int) -> None:
    from app.core.config import get_settings

    if content_type != "application/pdf":
        raise ValidationError(
            code="unsupported_content_type",
            message="Only PDF documents can be uploaded to the AI scratch area.",
        )
    ceiling = get_settings().ai_max_large_attachment_bytes
    if size_bytes > ceiling:
        raise ValidationError(
            code="upload_too_large",
            message=f"The declared size exceeds the AI large-file ceiling of {ceiling} bytes.",
        )


def scratch_object_key(organisation_id: uuid.UUID, upload_id: uuid.UUID) -> str:
    """The server-generated object key for one transient scratch upload."""
    return SCRATCH_KEY_TEMPLATE.format(organisation_id=organisation_id) + f"{upload_id}.pdf"


async def create_scratch_upload_intent(
    *,
    organisation_id: uuid.UUID,
    original_filename: str,
    content_type: str,
    size_bytes: int,
) -> tuple[str, str, datetime]:
    """Start the demo's transient upload: validate, generate the key, sign a PUT URL.

    The scratch namespace carries no durable file record — the object is a
    throwaway AI input whose lifecycle the AI retention sweep owns (v0.7 Scope
    §6.5) — so only the declared PDF/ceiling contract is validated here and
    the browser PUTs the bytes directly to the signed URL. The AI layer
    re-verifies ownership, size, MIME and digest when the reference is used.
    ``original_filename`` is metadata-only for the demo contract: the server
    always generates the object key from the upload id, so the client-provided
    name never influences storage or routing.
    """
    _validate_scratch_upload(content_type=content_type, size_bytes=size_bytes)
    upload_id = uuid7()
    object_key = scratch_object_key(organisation_id, upload_id)
    signed_url = await get_storage().create_upload_url(
        file_id=upload_id,
        object_key=object_key,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    return str(upload_id), signed_url.url, signed_url.expires_at


async def complete_scratch_upload(
    *,
    organisation_id: uuid.UUID,
    upload_id: str,
) -> str:
    """Verify the browser stored the transient object and return its reference.

    The object key is server-generated from the validated ``upload_id``; the
    completion re-heads the object so a client can never claim an upload that
    was never stored, and validates the stored object against the same
    PDF/ceiling contract the intent declared — no declared metadata is
    persisted, so the stored object itself is the only honest source for the
    size/MIME contract. The AI layer performs the authoritative ownership and
    digest verification when the reference is resolved at ask time.
    """
    try:
        parsed = uuid.UUID(upload_id)
    except ValueError as exc:
        raise ValidationError(
            code="invalid_upload_id", message="The upload id is not valid."
        ) from exc
    object_key = scratch_object_key(organisation_id, parsed)
    info = await get_storage().head_object(object_key)
    if info is None:
        raise ValidationError(code="upload_not_found", message="The upload could not be verified.")
    _validate_scratch_upload(content_type=info.content_type or "", size_bytes=info.size_bytes)
    return object_key
