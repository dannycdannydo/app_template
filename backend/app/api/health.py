"""Health and readiness endpoints (blueprint §28).

``/health`` is a liveness probe and always reports ok. ``/ready`` verifies
database reachability and returns 503 with the standard error format when the
database is unreachable, so probes and the compose healthcheck can rely on it.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.core.exceptions import ErrorResponse, ServiceUnavailableError

logger = structlog.get_logger()

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Payload returned by the health endpoints."""

    status: str = "ok"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe: the process is up and serving traffic."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse, responses={503: {"model": ErrorResponse}})
async def ready(session: Annotated[AsyncSession, Depends(get_db)]) -> HealthResponse:
    """Readiness probe: the process can reach its database."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("readiness_check_failed", error=str(exc))
        raise ServiceUnavailableError(
            code="database_unavailable",
            message="The service is not ready: the database is unreachable.",
        ) from exc
    return HealthResponse(status="ok")
