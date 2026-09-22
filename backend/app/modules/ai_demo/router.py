"""``document.classify`` / ``document.ask`` demonstration endpoints (v0.7 Scope §6.6, BP §12).

The router stays thin (BP §4): it resolves the caller through the shared
organisation membership dependency, gates each route with an existing
permission (no new ``ai.*`` permission until a second user-facing AI use case
proves it, rule of three — v0.7 Scope §2), and delegates to the demonstration
service. Triggering a classification or question is a document write/action
gated by ``documents.upload`` (member and above; a read-only viewer is
denied); reading a result is gated by ``documents.read``. The organisation id
always comes from the resolved membership, never from a request body or path.

There is no generic arbitrary-prompt endpoint (v0.7 Scope §6.6): the only
exposed tasks are the checked-in ``document.classify`` and ``document.ask``
demonstration tasks.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db, require_permission
from app.modules.ai_demo import service
from app.modules.ai_demo.schemas import (
    DocumentAskAcceptedResponse,
    DocumentAskRequest,
    DocumentAskResponse,
    DocumentAskResultResponse,
    DocumentClassifyAcceptedResponse,
    DocumentClassifyRequest,
    DocumentClassifyResultResponse,
    DocumentClassifySyncResponse,
    ScratchUploadCompleteResponse,
    ScratchUploadIntentRequest,
    ScratchUploadIntentResponse,
)
from app.modules.organisations.models import OrganisationMembership
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/ai/classify", tags=["ai"])

#: ``document.ask`` is durable by default for document-scale work; ``sync=true``
#: retains the existing bounded synchronous path for small inputs.
ask_router = APIRouter(prefix="/api/v1/ai/ask", tags=["ai"])

#: Demo-scoped transient upload surface (v0.8 Scope §2.2/§6.5): the AI test
#: screen uploads a PDF into the organisation-scoped ``ai/scratch/`` namespace
#: so the AI layer classifies the source as transient and routes a >5 MB PDF
#: through the provider-upload mode. The platform files module stays untouched
#: (it owns retained ``documents/`` records); this surface carries no durable
#: file record — scratch objects are AI-owned throwaway inputs.
scratch_router = APIRouter(prefix="/api/v1/ai/scratch", tags=["ai"])


@router.post(
    "",
    response_model=DocumentClassifySyncResponse,
    responses={202: {"model": DocumentClassifyAcceptedResponse}},
)
async def classify_document(
    payload: DocumentClassifyRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.upload"))],
    user: Annotated[User, Depends(get_current_user)],
) -> DocumentClassifySyncResponse | JSONResponse:
    """Classify a document: synchronously (``sync=true``) or as a durable job.

    Both paths pass the private storage reference through ``AIService.execute``
    so the service resolves it to a bounded provider-neutral attachment rather
    than rendering the reference as content (v0.7 Scope §2). The synchronous
    response (200) returns the validated result inline; the accepted response
    (202) returns the job and request ids the caller polls.
    """
    if not payload.sync:
        accepted = await service.enqueue_classify(
            session,
            organisation_id=membership.organisation_id,
            user=user,
            storage_reference=payload.storage_reference,
        )
        return JSONResponse(status_code=202, content=accepted.model_dump(mode="json"))
    return await service.classify_sync(
        session,
        organisation_id=membership.organisation_id,
        user=user,
        storage_reference=payload.storage_reference,
    )


@router.get("/requests/{request_id}", response_model=DocumentClassifyResultResponse)
async def get_classify_result(
    request_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.read"))],
) -> DocumentClassifyResultResponse:
    """Return the durable classification record; a foreign id is a 404."""
    return await service.get_classify_result(
        session,
        organisation_id=membership.organisation_id,
        request_id=request_id,
    )


@ask_router.post(
    "",
    response_model=DocumentAskResponse,
    responses={202: {"model": DocumentAskAcceptedResponse}},
)
async def ask_document(
    payload: DocumentAskRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.upload"))],
    user: Annotated[User, Depends(get_current_user)],
) -> DocumentAskResponse | JSONResponse:
    """Answer synchronously or queue a durable document question.

    The private storage reference and bounded question are passed to the
    ``document.ask`` task after organisation policy and source authority are
    checked. The default path returns 202 and runs through ``ai.execute``;
    ``sync=true`` retains the existing 5,000,000-byte HTTP bound.
    """
    if not payload.sync:
        accepted = await service.enqueue_ask(
            session,
            organisation_id=membership.organisation_id,
            user=user,
            storage_reference=payload.storage_reference,
            question=payload.question,
        )
        # FastAPI's primary ``response_model`` validates ordinary return values
        # even when ``response.status_code`` is changed dynamically. Return the
        # explicitly documented 202 response directly so it is not incorrectly
        # validated as the synchronous 200 answer shape.
        return JSONResponse(status_code=202, content=accepted.model_dump(mode="json"))
    return await service.ask_sync(
        session,
        organisation_id=membership.organisation_id,
        user=user,
        storage_reference=payload.storage_reference,
        question=payload.question,
    )


@ask_router.get("/requests/{request_id}", response_model=DocumentAskResultResponse)
async def get_ask_result(
    request_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.read"))],
) -> DocumentAskResultResponse:
    """Return the durable answer status; a foreign request id is a 404."""
    return await service.get_ask_result(
        session,
        organisation_id=membership.organisation_id,
        request_id=request_id,
    )


@scratch_router.post("/uploads", response_model=ScratchUploadIntentResponse, status_code=201)
async def create_scratch_upload_intent_endpoint(
    payload: ScratchUploadIntentRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.upload"))],
) -> ScratchUploadIntentResponse:
    """Start a transient upload into the AI scratch namespace (signed PUT URL)."""
    upload_id, upload_url, expires_at = await service.create_scratch_upload_intent(
        session,
        organisation_id=membership.organisation_id,
        original_filename=payload.original_filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
    )
    return ScratchUploadIntentResponse(
        upload_id=upload_id,
        upload_url=upload_url,
        expires_at=expires_at,
    )


@scratch_router.post("/uploads/{upload_id}/complete", response_model=ScratchUploadCompleteResponse)
async def complete_scratch_upload_endpoint(
    upload_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.upload"))],
) -> ScratchUploadCompleteResponse:
    """Verify the stored transient object and return its storage reference."""
    storage_reference = await service.complete_scratch_upload(
        session,
        organisation_id=membership.organisation_id,
        upload_id=upload_id,
    )
    return ScratchUploadCompleteResponse(storage_reference=storage_reference)
