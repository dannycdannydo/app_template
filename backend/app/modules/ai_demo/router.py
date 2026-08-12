"""``document.classify`` demonstration endpoints (v0.7 Scope §6.6, BP §12).

The router stays thin (BP §4): it resolves the caller through the shared
organisation membership dependency, gates each route with an existing
permission (no new ``ai.*`` permission until a second user-facing AI use case
proves it, rule of three — v0.7 Scope §2), and delegates to the demonstration
service. Triggering a classification is a document write/action gated by
``documents.upload`` (member and above; a read-only viewer is denied); reading
a result is gated by ``documents.read``. The organisation id always comes from
the resolved membership, never from a request body or path.

There is no generic arbitrary-prompt endpoint (v0.7 Scope §6.6): the only
exposed task is the checked-in ``document.classify`` demonstration task.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db, require_permission
from app.modules.ai_demo import service
from app.modules.ai_demo.schemas import (
    DocumentClassifyAcceptedResponse,
    DocumentClassifyRequest,
    DocumentClassifyResultResponse,
    DocumentClassifySyncResponse,
)
from app.modules.organisations.models import OrganisationMembership
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/ai/classify", tags=["ai"])


@router.post(
    "",
    response_model=DocumentClassifySyncResponse,
    responses={202: {"model": DocumentClassifyAcceptedResponse}},
)
async def classify_document(
    payload: DocumentClassifyRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[OrganisationMembership, Depends(require_permission("documents.upload"))],
    user: Annotated[User, Depends(get_current_user)],
) -> DocumentClassifySyncResponse | DocumentClassifyAcceptedResponse:
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
        response.status_code = 202
        return accepted
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
