"""Async engine and session factory (blueprint §5, §10).

The engine is created once from the typed settings. ``pool_pre_ping`` makes
connections resilient to database restarts, which the ``/ready`` endpoint
relies on.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    pool_pre_ping=True,
    echo=_settings.debug,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
