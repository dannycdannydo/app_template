"""Structured JSON logging with request ID context (blueprint §28).

Logging is configured once at application startup. Request IDs are bound per
request by the middleware in ``app.main`` and merged into every log line by
``merge_contextvars``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, cast

import structlog

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "set_cookie",
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "secret",
    "client_secret",
    "dsn",
    "database_url",
    "connection_string",
    "signed_url",
    "upload_url",
    "download_url",
}
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_CONNECTION_URL_RE = re.compile(
    r"(?i)\b(?:postgres(?:ql)?(?:\+[a-z0-9]+)?|mysql(?:\+[a-z0-9]+)?|redis)://[^\s]+"
)
_SIGNED_URL_RE = re.compile(
    r"(?i)https?://[^\s]+(?:x-amz-signature|x-goog-signature|signature=|token=)[^\s]*"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|client[_-]?secret)\s*[:=]\s*[^\s,;&]+"
)


def _is_sensitive_key(key: object) -> bool:
    normalised = str(key).strip().lower().replace("-", "_")
    return normalised in _SENSITIVE_KEYS or normalised.endswith(("_password", "_token", "_secret"))


def _redact_string(value: str) -> str:
    value = _BEARER_RE.sub(f"Bearer {REDACTED}", value)
    value = _CONNECTION_URL_RE.sub(REDACTED, value)
    value = _SIGNED_URL_RE.sub(REDACTED, value)
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}={REDACTED}", value)


def redact_sensitive_data(value: Any) -> Any:
    """Return a recursively redacted copy suitable for logs or error reporting."""
    if isinstance(value, Mapping):
        items = cast("Mapping[object, Any]", value)
        return {
            key: REDACTED if _is_sensitive_key(key) else redact_sensitive_data(item)
            for key, item in items.items()
        }
    if isinstance(value, list):
        items = cast("list[Any]", value)
        return [redact_sensitive_data(item) for item in items]
    if isinstance(value, tuple):
        items = cast("tuple[Any, ...]", value)
        return tuple(redact_sensitive_data(item) for item in items)
    if isinstance(value, str):
        return _redact_string(value)
    return value


def redact_log_event(
    logger: object,
    method_name: str,
    event_dict: structlog.typing.EventDict,
) -> structlog.typing.EventDict:
    """Structlog processor applying the shared recursive redaction policy."""
    return redact_sensitive_data(event_dict)


_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# The core processors every configuration must keep, in order: context
# variables (request/identity/job ids), log level, ISO timestamp, exception
# formatting. The renderer is appended after these in ``configure_logging``.
_CORE_PROCESSORS: tuple[structlog.typing.Processor, ...] = (
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
    redact_log_event,
)

# The process-wide processor chain. ``configure_logging`` mutates this one
# list in place (never replaces it) so that loggers cached by
# ``cache_logger_on_first_use`` keep referencing the same object — repeated
# configuration (every ``create_app`` in tests) and tooling that temporarily
# swaps processors (structlog's ``capture_logs`` test helper) then always see
# the current chain.
_PROCESSORS: list[structlog.typing.Processor] = [*_CORE_PROCESSORS]


def configure_logging(*, log_level: str, json_logs: bool) -> None:
    """Configure structlog once.

    Args:
        log_level: Logging level name (``DEBUG``, ``INFO``, ...). Unknown
            values fall back to ``INFO``.
        json_logs: When true, emit JSON lines (production); otherwise use a
            readable console renderer (local development).
    """
    level = _LOG_LEVELS.get(log_level.upper(), logging.INFO)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    _PROCESSORS.clear()
    _PROCESSORS.extend([*_CORE_PROCESSORS, renderer])
    structlog.configure(
        processors=_PROCESSORS,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.getLogger().setLevel(level)


def current_request_id() -> str:
    """Return the request ID bound by the request ID middleware, if any."""
    return str(structlog.contextvars.get_contextvars().get("request_id", ""))


def bind_identity_context(*, user_id: str, organisation_id: str | None = None) -> None:
    """Bind the authenticated caller's identity to the logging context.

    Called by the authentication dependencies once the caller is validated and
    enabled (blueprint §28 logging-context field set: ``user_id`` and
    ``organisation_id``). The request middleware clears the context at the
    start and end of every request, so nothing leaks across requests.
    """
    context: dict[str, str] = {"user_id": user_id}
    if organisation_id is not None:
        context["organisation_id"] = organisation_id
    structlog.contextvars.bind_contextvars(**context)


def bind_worker_context(*, job_id: str, resource_id: str | None = None) -> None:
    """Bind the durable job identity to the logging context in a worker task.

    Worker tasks call this first thing (after clearing context vars) so every
    log line an attempt emits carries the ``job_id``, and ``resource_id`` once
    the row the job operates on is known. Context vars are per-async-task, so
    a message cannot observe another message's context; the explicit clear is
    a belt-and-braces guard for threads that process messages serially.
    """
    structlog.contextvars.clear_contextvars()
    context: dict[str, str] = {"job_id": job_id}
    if resource_id is not None:
        context["resource_id"] = resource_id
    structlog.contextvars.bind_contextvars(**context)
