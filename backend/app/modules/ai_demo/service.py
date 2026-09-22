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
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import scratch as ai_scratch
from app.ai.errors import AIError
from app.ai.execution import (
    ASK_TASK,
    enqueue_document_classification,
    enqueue_document_execution,
    execute_managed_ai,
    get_ai_execution_snapshot,
)
from app.ai.schemas import AIRequest
from app.ai.tasks.schemas import DocumentClassificationResult
from app.core.config import get_settings
from app.core.exceptions import (
    APIError,
    ExternalServiceError,
    NotFoundError,
    RateLimitExceeded,
    ServiceUnavailableError,
    ValidationError,
)
from app.modules.ai_demo.schemas import (
    ClassifyCost,
    ClassifyRouting,
    ClassifyUsage,
    DocumentAskAcceptedResponse,
    DocumentAskResponse,
    DocumentAskResultResponse,
    DocumentClassifyAcceptedResponse,
    DocumentClassifyResultResponse,
    DocumentClassifySyncResponse,
)
from app.modules.users.models import User
from app.scanning import ScannerUnavailableError, ScanVerdict, get_scanner
from app.storage import get_storage

#: The single demonstrated task (kept in sync with ``app.ai.execution``).
DEMO_TASK = "document.classify"

#: Each AI error code maps to one HTTP-shaped API error so the router never
#: handles AI taxonomy itself. Messages stay generic and safe (BP §28) and
#: take a subject noun (classification / question) so both demonstrations
#: surface accurate wording without duplicating the taxonomy.
_AI_ERROR_MAP: dict[str, APIError] = {
    "ai_unavailable": ServiceUnavailableError(
        code="ai_unavailable", message="AI is not enabled for this organisation."
    ),
    # Plan P9 synchronous bound. Callers that explicitly select the inline
    # path can retry through the default durable operation for larger inputs.
    "ai_ask_attachment_too_large": ValidationError(
        code="ai_ask_attachment_too_large",
        message=(
            "The document is larger than the maximum size that can be processed "
            "synchronously. Submit it as a background question instead."
        ),
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
    try:
        queued = await enqueue_document_classification(
            session,
            organisation_id=organisation_id,
            user_id=user.id,
            storage_reference=storage_reference,
        )
    except AIError as exc:
        raise _translate_ai_error(exc) from exc
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
    PDF at or below the inline threshold becomes a bounded inline attachment.
    The bounded question travels as a metadata variable so the feature-facing
    ``AIRequest`` contract stays unchanged; the answer is validated text, never
    unvalidated provider output (v0.7 Scope §6.4).

    Plan P9 synchronous bound: when explicitly selected, the synchronous path
    checks the source against ``AI_ASK_MAX_SYNCHRONOUS_BYTES`` and rejects a
    larger source instead of running a long transfer in HTTP (BP §18). The
    default durable operation handles document-scale work. The bound is enforced
    inside the common ``AIService.execute`` boundary, *after* the organisation
    AI-enabled policy and the P6 source authority and *before* any attachment
    bytes are read, so a disabled organisation or an unauthorised key keeps its
    own error and never triggers pre-authorisation storage I/O.
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
            max_synchronous_source_bytes=get_settings().ai_ask_max_synchronous_bytes,
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


async def enqueue_ask(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    user: User,
    storage_reference: str,
    question: str,
) -> DocumentAskAcceptedResponse:
    """Persist a durable document question and return its polling ids."""
    _validate_storage_reference(storage_reference, organisation_id)
    try:
        queued = await enqueue_document_execution(
            session,
            organisation_id=organisation_id,
            user_id=user.id,
            storage_reference=storage_reference,
            task=ASK_TASK,
            metadata={"question": question},
        )
    except AIError as exc:
        raise _translate_ai_error(exc, subject="question") from exc
    return DocumentAskAcceptedResponse(job_id=str(queued.job_id), request_id=queued.request_id)


async def get_ask_result(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    request_id: str,
) -> DocumentAskResultResponse:
    """Return one organisation-scoped durable question result."""
    record = await get_ai_execution_snapshot(
        session,
        organisation_id=organisation_id,
        request_id=request_id,
    )
    output: str | None = None
    if record.status == "succeeded" and record.output is not None:
        candidate = record.output.get("text")
        if isinstance(candidate, str) and candidate:
            output = candidate
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
    return DocumentAskResultResponse(
        request_id=record.request_id,
        status=record.status,
        error_code=record.error_code,
        output=output,
        routing=routing,
        usage=usage,
        cost=cost,
        completed_at=record.completed_at,
    )


#: The transient upload ceiling mirrors the AI large-file template ceiling
#: (v0.8 Scope §2.2): the demo's scratch path carries exactly one PDF of at
#: most the 50,000,000-byte large-file ceiling, so an intent never signs a
#: PUT URL for bytes the AI layer would then refuse. The organisation-scoped
#: AI scratch namespace and its durable intent lifecycle live in
#: ``app.ai.scratch`` (plan P6): a feature module never names the transfer
#: contract module (v0.8 Scope §6.1 checkbox 3 import boundary).
SCRATCH_KEY_TEMPLATE = ai_scratch.SCRATCH_KEY_PREFIX


def _validate_scratch_upload(*, content_type: str, size_bytes: int) -> None:
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
    return ai_scratch.scratch_object_key(organisation_id, upload_id)


async def create_scratch_upload_intent(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    original_filename: str,
    content_type: str,
    size_bytes: int,
) -> tuple[str, str, datetime]:
    """Start the demo's transient upload: validate, persist an intent, sign a PUT.

    Plan P6: the capability is issued only when the organisation's AI policy is
    enabled, and the durable scratch intent (with its bounded global lifetime)
    is persisted before the URL is signed. ``original_filename`` is
    metadata-only for the demo contract: the server always generates the object
    key from the upload id, so the client-provided name never influences
    storage or routing.
    """
    _validate_scratch_upload(content_type=content_type, size_bytes=size_bytes)
    try:
        intent = await ai_scratch.create_scratch_intent(
            session,
            organisation_id=organisation_id,
            content_type=content_type,
            size_bytes=size_bytes,
        )
    except AIError as exc:
        raise _translate_ai_error(exc, subject="scratch upload") from exc
    signed_url = await get_storage().create_upload_url(
        file_id=intent.upload_id,
        object_key=intent.object_key,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    await session.commit()
    return str(intent.upload_id), signed_url.url, signed_url.expires_at


async def complete_scratch_upload(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    upload_id: str,
) -> str:
    """Verify the stored transient object and mark its durable intent ``ready``.

    Plan P6 (AC19): the durable, org-scoped intent is loaded first and must be
    ``pending`` (or a still-live ``ready`` replay) and unexpired. The stored
    object's content type and size must exactly match the intent's declared
    contract, and the configured scanner must return a non-quarantine verdict
    before the intent is promoted, so a mismatch, an expired intent or
    untrusted bytes can never become AI-readable. A scanner outage (or a
    quarantine verdict) fails closed and leaves the intent pending.
    """
    try:
        parsed = uuid.UUID(upload_id)
    except ValueError as exc:
        raise ValidationError(
            code="invalid_upload_id", message="The upload id is not valid."
        ) from exc
    intent = await ai_scratch.get_scratch_intent(
        session,
        organisation_id=organisation_id,
        upload_id=parsed,
    )
    if intent is None:
        raise ValidationError(code="upload_not_found", message="The upload could not be verified.")
    now = datetime.now(UTC)
    if intent.status == "ready":
        # Idempotent replay: a still-live completed intent returns its key.
        if intent.expires_at <= now:
            raise ValidationError(code="upload_expired", message="The upload has expired.")
        return intent.object_key
    if intent.status != "pending":
        raise ValidationError(code="upload_not_found", message="The upload could not be verified.")
    if intent.expires_at <= now:
        raise ValidationError(code="upload_expired", message="The upload has expired.")
    info = await get_storage().head_object(intent.object_key)
    if info is None:
        raise ValidationError(code="upload_not_found", message="The upload could not be verified.")
    if (info.content_type or "") != intent.content_type or info.size_bytes != intent.size_bytes:
        raise ValidationError(
            code="upload_contract_mismatch",
            message="The stored object does not match the declared upload contract.",
        )
    try:
        verdict = await get_scanner().scan(
            storage=get_storage(),
            object_key=intent.object_key,
            content_type=intent.content_type,
            max_bytes=intent.size_bytes,
        )
    except ScannerUnavailableError as exc:
        raise ServiceUnavailableError(
            code="upload_scan_unavailable",
            message="The upload scanner is unavailable; the upload was not accepted.",
        ) from exc
    if verdict is ScanVerdict.QUARANTINED:
        raise ValidationError(
            code="upload_quarantined",
            message="The uploaded object was rejected by the upload scanner.",
        )
    ready_key = await ai_scratch.complete_scratch_intent(
        session,
        organisation_id=organisation_id,
        upload_id=parsed,
    )
    if ready_key is None:
        raise ValidationError(code="upload_not_found", message="The upload could not be verified.")
    await session.commit()
    return ready_key
