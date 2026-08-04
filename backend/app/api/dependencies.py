"""Shared FastAPI dependencies (blueprint §5, §6).

Routers stay thin and receive their dependencies here: the database session
and the current request ID.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import current_request_id
from app.db.session import async_session_factory


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
