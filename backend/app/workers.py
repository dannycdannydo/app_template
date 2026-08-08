"""Dramatiq worker entrypoint (blueprint §18, ADR-0004, Scope §6.2/§6.4).

``uv run dramatiq app.workers`` starts the worker process: this module
configures the Redis broker, installs it as the process-wide Dramatiq broker,
and configures the same structured logging the API uses, so worker logs and
request logs are shaped identically (blueprint §28 logging context — the
worker's tasks add ``job_id`` to that context).

Scope §6.4 adds the two things the durable-job foundation needs here:

- the :func:`worker_middleware` stack: the dramatiq defaults (retries, time
  limits, callbacks, pipelines, shutdown notifications, age limits) plus
  ``AsyncIO``, which manages the event-loop thread the async tasks run on
  (it is deliberately absent from the dramatiq defaults). Tests build their
  stub brokers from the same factory so the worker and the suite can never
  drift apart.
- the task-module import in :func:`configure_worker`: importing task modules
  is what registers their actors with the broker. The first task modules
  arrive with the job infrastructure and the file-processing job (Scope
  §6.4/§6.5).

Worker concurrency is passed by the ``make worker`` / ``dev-docker`` commands
from ``WORKER_CONCURRENCY`` (default 8, matching ``settings.worker_concurrency``),
so the CLI flag stays the single knob and this module never needs it.
"""

from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import AsyncIO, Middleware, default_middleware

from app.core.config import get_settings
from app.core.logging import configure_logging

_settings = get_settings()

type MiddlewareStack = list[Middleware]


def worker_middleware() -> MiddlewareStack:
    """Return the middleware stack every worker broker must use.

    The dramatiq defaults handle retries (with per-actor options from
    ``jobs_service.retry_policy``), time limits, callbacks and pipelines;
    ``AsyncIO`` runs the async tasks on a managed event-loop thread and is
    added here because it is not part of the dramatiq default list. Passing a
    full stack to the broker constructor is required — a custom list replaces
    the defaults rather than extending them.
    """
    return [AsyncIO(), *(middleware() for middleware in default_middleware)]


def build_broker() -> RedisBroker:
    """Return the Redis broker configured for the worker process."""
    return RedisBroker(url=_settings.redis_url, middleware=worker_middleware())


def configure_worker() -> None:
    """Configure logging, the broker, and task registration for this process."""
    configure_logging(log_level=_settings.log_level, json_logs=not _settings.debug)
    dramatiq.set_broker(build_broker())
    # Task modules register their actors with the broker when imported. Import
    # them here, after the broker is set, so every actor is declared exactly
    # once per worker process. The import is intentionally side-effect-only:
    # both linters are told so (ruff needs noqa, pyright needs the ignore, the
    # same double-suppression pattern db/base.py uses for its model imports).
    import app.modules.files.tasks  # pyright: ignore[reportUnusedImport]
    import app.modules.jobs.tasks  # noqa: F401  # pyright: ignore[reportUnusedImport]


configure_worker()
