"""Distributed API rate limiting backed by Redis (blueprint §30)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.exceptions import RateLimitExceeded, ServiceUnavailableError


class RateLimiter(Protocol):
    """Atomically count one request in a fixed time window."""

    async def enforce(self, *, key: str, limit: int, window_seconds: int) -> None: ...


class RedisRateLimiter:
    """Redis-backed limiter safe to share across API processes."""

    _INCREMENT_SCRIPT = (
        "local current = redis.call('INCR', KEYS[1]); "
        "if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end; "
        "return current"
    )

    def __init__(self, redis_url: str) -> None:
        # redis-py's async generic annotations do not yet describe ``eval``
        # consistently under pyright's strict mode.
        self._redis: Any = cast(Any, Redis).from_url(redis_url, decode_responses=True)

    async def enforce(self, *, key: str, limit: int, window_seconds: int) -> None:
        try:
            current = int(
                await self._redis.eval(self._INCREMENT_SCRIPT, 1, key, str(window_seconds))
            )
        except RedisError as exc:
            # Failing open would silently remove the abuse control during an
            # incident, so production API traffic fails closed instead.
            raise ServiceUnavailableError(
                code="rate_limiter_unavailable",
                message="The service is temporarily unavailable. Please try again.",
            ) from exc
        if current > limit:
            raise RateLimitExceeded(headers={"Retry-After": str(window_seconds)})


class NoOpRateLimiter:
    """Test-only limiter; integration tests stay independent of Redis."""

    async def enforce(self, *, key: str, limit: int, window_seconds: int) -> None:
        return None


@lru_cache
def get_rate_limiter() -> RateLimiter:
    """Return the process-wide limiter, fail-closing outside the test profile."""
    settings = get_settings()
    if settings.app_env == "test":
        return NoOpRateLimiter()
    return RedisRateLimiter(settings.redis_url)
