"""WorkOS webhook endpoint (Scope §6.8, blueprint §8, §13, §30).

The router stays thin: the signature dependency verifies the delivery (BP §30
— webhook signature verification, centralised with the other session/webhook
validation per BP §8), the schemas parse the payload, and the consumer service
owns the best-effort refresh transaction. This is the one route in the API
gated by a signature rather than a session token — no Bearer token, no
``X-Org-Id``, and it deliberately returns no detail about local state so the
response leaks nothing (the reply is identical whether the refresh applied a
change or was a no-op).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_workos_webhook_signature
from app.modules.webhooks import service
from app.modules.webhooks.schemas import WebhookResponse, parse_webhook_event

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


@router.post("/workos", response_model=WebhookResponse)
async def workos_webhook_endpoint(
    payload: Annotated[bytes, Depends(require_workos_webhook_signature)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookResponse:
    """Consume one signature-verified WorkOS webhook delivery (best-effort).

    A missing, malformed or stale signature is rejected with 401 before the
    payload is parsed; a valid delivery is parsed and dispatched to the
    consumer, which refreshes best-effort local state only. Login-time
    reconciliation (Scope §6.5) remains the authoritative grant path, so a
    delivery that never arrives cannot block a legitimate invitation.
    """
    event = parse_webhook_event(payload)
    await service.process_webhook_event(session, event)
    return WebhookResponse()
