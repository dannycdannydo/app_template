"""Shared Dramatiq broker configuration (BP §18, v0.5 Scope §6.4).

Both the API process and the Dramatiq worker install a broker from this module.
Keeping the factory separate from ``app.workers`` lets API startup publish jobs
without importing the worker entrypoint, whose task imports are intentionally
registration side effects.
"""

from __future__ import annotations

from dramatiq.broker import Broker
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker
from dramatiq.middleware import AsyncIO, Middleware, default_middleware

from app.core.config import get_settings
from app.observability.sentry import SentryWorkerMiddleware

type MiddlewareStack = list[Middleware]


def worker_middleware() -> MiddlewareStack:
    """Return the middleware stack every production broker must use.

    The Dramatiq defaults handle retries, time limits, callbacks, pipelines,
    shutdown notifications and age limits. ``AsyncIO`` supplies the managed
    event-loop thread required by async actors, and the Sentry middleware
    reports unhandled task exceptions when configured.
    """
    return [
        AsyncIO(),
        SentryWorkerMiddleware(),
        *(middleware() for middleware in default_middleware),
    ]


def build_broker() -> Broker:
    """Return the broker shared by API and worker processes.

    The test profile is deliberately network-free: actors imported during
    pytest collection bind permanently to an in-memory broker, so a unit or
    database test can never publish an orphaned message into a developer's
    Redis queues. Tests that exercise Redis construct their own uniquely
    namespaced ``RedisBroker`` explicitly.
    """
    settings = get_settings()
    middleware = worker_middleware()
    if settings.app_env == "test":
        return StubBroker(middleware=middleware)
    return RedisBroker(url=settings.redis_url, middleware=middleware)
