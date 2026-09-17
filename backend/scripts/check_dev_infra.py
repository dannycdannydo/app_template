"""Fail fast when host-native development cannot reach Redis.

Compose health checks run inside their containers. They can therefore report
Redis healthy even when Docker failed to publish the configured host port or
attach the container to its network. ``make dev`` invokes this probe before
migrations or application processes so the worker never starts a connection
error loop against an unreachable broker.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from redis import Redis
from redis.exceptions import RedisError


class DevInfraError(Exception):
    """An actionable local-infrastructure connectivity failure."""


class RedisProbe(Protocol):
    """Small redis-py surface used by the probe and its unit tests."""

    def ping(self) -> bool: ...

    def close(self) -> None: ...


RedisProbeFactory = Callable[[str], RedisProbe]


def _redis_probe(url: str) -> RedisProbe:
    return cast(
        RedisProbe,
        cast(Any, Redis).from_url(url, socket_connect_timeout=2, socket_timeout=2),
    )


def redis_endpoint_label(url: str) -> str:
    """Return a credential-free host:port label for operator messages."""
    parsed = urlsplit(url)
    host = parsed.hostname or "configured host"
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parsed.port or 6379}"


def check_redis(
    url: str,
    *,
    setting_name: str = "REDIS_URL",
    probe_factory: RedisProbeFactory = _redis_probe,
) -> str:
    """Ping Redis through ``url`` and return its safe endpoint label."""
    try:
        endpoint = redis_endpoint_label(url)
    except ValueError as exc:
        raise DevInfraError(
            f"{setting_name} does not contain a valid Redis host and port. "
            "Check the repo-root .env."
        ) from exc
    probe: RedisProbe | None = None
    try:
        probe = probe_factory(url)
        if not probe.ping():
            raise DevInfraError(f"Redis at {endpoint} returned an unsuccessful PING response.")
    except DevInfraError:
        raise
    except (RedisError, OSError, ValueError) as exc:
        raise DevInfraError(
            f"Redis is not reachable through {setting_name} at {endpoint}. "
            "Container health checks do not verify the published host port. "
            "Run `make dev-down` and retry; use "
            "`CONFIRM_RESET=1 make dev-reset` only when local data is disposable."
        ) from exc
    finally:
        if probe is not None:
            with suppress(RedisError, OSError):
                probe.close()
    return endpoint


def main() -> int:
    """Load typed settings, probe Redis, and emit a concise result."""
    from app.core.config import get_settings

    try:
        settings = get_settings()
        broker_endpoint = check_redis(settings.broker_redis_url, setting_name="BROKER_REDIS_URL")
        rate_limit_endpoint = check_redis(
            settings.rate_limit_redis_url, setting_name="RATE_LIMIT_REDIS_URL"
        )
    except DevInfraError as exc:
        print(f"dev-infra check failed: {exc}", file=sys.stderr)
        return 1
    print(
        "dev-infra check passed: Redis endpoints reachable at "
        f"{broker_endpoint} and {rate_limit_endpoint}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
