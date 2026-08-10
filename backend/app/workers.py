"""Dramatiq worker entrypoint (blueprint §18, ADR-0004, v0.5 Scope §6.2/§6.4).

``uv run dramatiq app.workers`` starts the worker process: this module
configures the Redis broker, installs it as the process-wide Dramatiq broker,
and configures the same structured logging the API uses, so worker logs and
request logs are shaped identically (blueprint §28 logging context — the
worker's tasks add ``job_id`` to that context).

v0.5 Scope §6.4 adds the two things the durable-job foundation needs here:

- the shared ``app.broker`` factory: the Dramatiq defaults (retries, time
  limits, callbacks, pipelines, shutdown notifications, age limits) plus
  ``AsyncIO``, which manages the event-loop thread the async tasks run on
  (it is deliberately absent from the dramatiq defaults). Tests build their
  stub brokers from the same factory so the worker and the suite can never
  drift apart.
- the task-module import in :func:`configure_worker`: importing task modules
  is what registers their actors with the broker. The first task modules
  arrive with the job infrastructure and the file-processing job (v0.5 Scope
  §6.4/§6.5).

Worker concurrency is passed by the ``make worker`` / ``dev-docker`` commands
from ``WORKER_CONCURRENCY`` (default 8, matching ``settings.worker_concurrency``),
so the CLI flag stays the single knob and this module never needs it.
"""

from __future__ import annotations

import dramatiq

from app.broker import build_broker
from app.core.config import get_settings
from app.core.logging import configure_logging

_settings = get_settings()


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
    import app.modules.jobs.tasks  # pyright: ignore[reportUnusedImport]
    import app.modules.notifications.tasks  # noqa: F401  # pyright: ignore[reportUnusedImport]


configure_worker()
