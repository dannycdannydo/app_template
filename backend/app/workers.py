"""Dramatiq worker entrypoint (blueprint §18, ADR-0004, Scope §6.2).

``uv run dramatiq app.workers`` starts the worker process: this module
configures the Redis broker, installs it as the process-wide Dramatiq broker,
and configures the same structured logging the API uses, so worker logs and
request logs are shaped identically. Task modules are imported here so their
tasks register with the broker; the first task modules arrive with the file
processing job (Scope §6.4/§6.5). The plumbing in this file is the durable
part — the worker command is fixed by blueprint §36 and ADR-0008, and the
retry policy / concurrency are owned by the job service (Scope §6.4).
"""

from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.core.config import get_settings
from app.core.logging import configure_logging

_settings = get_settings()

configure_logging(log_level=_settings.log_level, json_logs=not _settings.debug)

_broker = RedisBroker(url=_settings.redis_url)
dramatiq.set_broker(_broker)
