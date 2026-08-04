"""Structured JSON logging with request ID context (blueprint §28).

Logging is configured once at application startup. Request IDs are bound per
request by the middleware in ``app.main`` and merged into every log line by
``merge_contextvars``.
"""

from __future__ import annotations

import logging

import structlog

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def configure_logging(*, log_level: str, json_logs: bool) -> None:
    """Configure structlog once.

    Args:
        log_level: Logging level name (``DEBUG``, ``INFO``, ...). Unknown
            values fall back to ``INFO``.
        json_logs: When true, emit JSON lines (production); otherwise use a
            readable console renderer (local development).
    """
    level = _LOG_LEVELS.get(log_level.upper(), logging.INFO)
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.getLogger().setLevel(level)


def current_request_id() -> str:
    """Return the request ID bound by the request ID middleware, if any."""
    return str(structlog.contextvars.get_contextvars().get("request_id", ""))
