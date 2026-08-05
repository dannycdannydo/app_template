"""Shared FastAPI dependencies (blueprint §5, §6, §8).

Routers stay thin and receive their dependencies here: the database session,
the current request ID, and the authenticated user resolved from a validated
WorkOS session token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDenied, UnauthorizedError
from app.core.logging import current_request_id
from app.core.security import (
    InvalidSessionError,
    SessionValidator,
    UserProfileClient,
    get_session_validator,
    get_user_profile_client,
)
from app.db.session import async_session_factory
from app.modules.users.models import User
from app.modules.users.service import get_or_provision_user

logger = structlog.get_logger()


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield one database session for the duration of a request.

    The session is committed/closed by SQLAlchemy's ``async_sessionmaker``
    context manager; the service layer owns transaction boundaries.
    """
    async with async_session_factory() as session:
        yield session


def get_request_id() -> str:
    """Return the request ID bound by the request ID middleware."""
    return current_request_id()


def _bearer_token(authorization: str | None) -> str:
    """Extract and return the Bearer token; reject missing/malformed headers."""
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError(
            code="invalid_token",
            message="A valid Bearer token is required.",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise UnauthorizedError(
            code="invalid_token",
            message="A valid Bearer token is required.",
        )
    return token


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_db)],
    validator: Annotated[SessionValidator, Depends(get_session_validator)],
    profiles: Annotated[UserProfileClient, Depends(get_user_profile_client)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Resolve the Bearer token to a validated, enabled internal user (BP §8).

    Invalid tokens are rejected with 401; valid sessions map to the internal
    user, provisioning the row on first login. Disabled users are blocked with
    403 even with a valid session.
    """
    token = _bearer_token(authorization)
    try:
        validated = await validator.validate_token(token)
    except InvalidSessionError as exc:
        logger.warning("session_rejected", reason=exc.reason)
        raise UnauthorizedError(
            code="invalid_session",
            message="The session is invalid or has expired.",
        ) from exc
    user = await get_or_provision_user(session, validated, profiles)
    if not user.is_active:
        logger.warning("disabled_user_rejected", workos_user_id=validated.workos_user_id)
        raise PermissionDenied(
            code="user_disabled",
            message="Your account is disabled.",
        )
    return user
