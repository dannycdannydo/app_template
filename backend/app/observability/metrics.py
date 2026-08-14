"""Prometheus metrics for the API and the job pipeline (blueprint §28).

The API surface exposes ``GET /metrics`` in the Prometheus text exposition
format (public like ``/health`` and ``/ready``). Two families are maintained:

- ``http_requests_total`` and ``http_request_duration_seconds``: every request
  except the ``/metrics`` scrape itself (the scraper would otherwise double
  its own counter on every poll). Path labels are normalised so ids in URL
  segments do not explode the label cardinality.
- ``jobs_*_total``: durable job transitions, incremented by
  ``app.modules.jobs.service`` at the point the durable row changes state
  (enqueue / succeed / fail), so the counters cannot drift from the source of
  truth.
"""

from __future__ import annotations

import re
from time import perf_counter
from typing import Any, cast

import dramatiq
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import RequestResponseEndpoint

router = APIRouter(tags=["metrics"])
logger = structlog.get_logger()

# Request counters and latency. ``path`` holds the normalised route pattern
# (uuid segments collapsed to ``{id}``) so label cardinality stays bounded.
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests processed",
    ["method", "path", "status_code"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
)

# Durable job counters (blueprint §28: job pipeline visibility; the durable
# record shape is §18's). The job_type label mirrors the durable row's
# ``job_type``.
JOBS_ENQUEUED_TOTAL = Counter(
    "jobs_enqueued_total",
    "Durable jobs enqueued",
    ["job_type"],
)
JOBS_SUCCEEDED_TOTAL = Counter(
    "jobs_succeeded_total",
    "Durable jobs completed successfully",
    ["job_type"],
)
JOBS_FAILED_TOTAL = Counter(
    "jobs_failed_total",
    "Durable jobs failed",
    ["job_type"],
)
JOBS_STALE_MESSAGES_TOTAL = Counter(
    "jobs_stale_messages_total",
    "Dramatiq messages discarded because their durable job row is absent",
)

# AI execution metrics (v0.7 Scope §6.7, blueprint §28): one sample per
# provider execution, labelled only with low-cardinality registry ids — task,
# provider and model — plus a fixed status/direction label. Organisation ids,
# request ids, prompt text, attachment names and provider output must never
# become labels (BP §28 never-log list, ADR-0017); the durable
# ``ai_requests``/``ai_outputs`` rows are the per-request source of truth and
# these counters are the aggregate signal. The histogram buckets and counter
# cost units (USD, the registry's single pricing currency) are documented in
# docs/operations.md → AI observability.
AI_REQUESTS_TOTAL = Counter(
    "ai_requests_total",
    "AI provider executions by terminal outcome",
    ["task", "provider", "model", "status"],
)
AI_REQUEST_DURATION_SECONDS = Histogram(
    "ai_request_duration_seconds",
    "AI provider execution latency in seconds",
    ["task", "provider", "model"],
)
AI_TOKENS_TOTAL = Counter(
    "ai_tokens_total",
    "AI tokens consumed by direction",
    ["task", "provider", "model", "direction"],
)
AI_COST_TOTAL = Counter(
    "ai_cost_total",
    "AI spend in USD priced from the registry's reviewed rates",
    ["task", "provider", "model"],
)
AI_VALIDATION_FAILURES_TOTAL = Counter(
    "ai_validation_failures_total",
    "Structured-output validation failures (repair or task retry follows)",
    ["task", "provider", "model"],
)
AI_RETRIES_TOTAL = Counter(
    "ai_retries_total",
    "Bounded retry attempts after the first dispatch",
    ["task", "provider", "model"],
)
AI_FALLBACKS_TOTAL = Counter(
    "ai_fallbacks_total",
    "Reviewed provider/model fallbacks under the task's fallback policy",
    ["task", "provider", "model"],
)
AI_BUDGET_DENIALS_TOTAL = Counter(
    "ai_budget_denials_total",
    "Monthly organisation AI budget denials",
    ["task"],
)

# The template's Dramatiq queues (blueprint §18 example queues: default,
# documents, integrations, ai, emails): ``default`` carries the job-infra
# retries-exhausted finalizer, ``documents`` the file-processing job, ``emails``
# notification delivery, and ``ai`` the AI execution and retention jobs
# (v0.7 Scope §6.6). The depth gauge is the concrete backlog signal the AI
# runbooks and dashboard contract alert on (v0.7 Scope §6.7): without it an
# operator cannot configure the promised queue-backlog alert from
# ``GET /metrics``.
TEMPLATE_QUEUES = ("default", "documents", "emails", "ai")

DRAMATIQ_QUEUE_DEPTH = Gauge(
    "dramatiq_queue_depth",
    "Undelivered messages waiting in a Dramatiq queue",
    ["queue"],
)

#: Rate-limit the refresh-failure log to one line per outage/recovery.
_queue_depth_refresh_failed = False

_UUID_SEGMENT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def normalise_path(path: str) -> str:
    """Collapse id-like URL segments so metric labels stay low-cardinality.

    ``/api/v1/files/<uuid>/download-url`` becomes
    ``/api/v1/files/{id}/download-url``; purely numeric segments (page
    numbers, record ids) collapse the same way.
    """
    segments: list[str] = []
    for segment in path.split("/"):
        if _UUID_SEGMENT.fullmatch(segment) or segment.isdigit():
            segments.append("{id}")
        else:
            segments.append(segment)
    return "/".join(segments)


async def metrics_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Instrument every request except the ``/metrics`` scrape itself."""
    if request.url.path == "/metrics":
        return await call_next(request)
    method = request.method
    path = normalise_path(request.url.path)
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # The exception propagates to FastAPI's unexpected-exception handler
        # (``_handle_unexpected_exception`` in app.main), which always returns
        # the safe generic 500. The request never produced a response object
        # to count, so count it here with status 500; the label stays accurate
        # only while every unhandled exception maps to 500.
        HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status_code="500").inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(
            perf_counter() - started
        )
        raise
    HTTP_REQUESTS_TOTAL.labels(
        method=method, path=path, status_code=str(response.status_code)
    ).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(perf_counter() - started)
    return response


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Return the process metrics in the Prometheus text exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- AI observability helpers (v0.7 Scope §6.7) ------------------------------
#
# ``AIService`` records one observation per settled attempt (the durable
# ``ai_requests`` rows are the per-request source of truth; these counters are
# the aggregate signal) and the persistence service records budget denials.
# Every parameter is an explicit safe value — never content, references or
# identifiers with unbounded cardinality (BP §28, ADR-0017).


def observe_ai_attempt(
    *,
    task: str,
    provider: str,
    model: str,
    status: str,
    latency_ms: int,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    """Record one settled provider execution attempt.

    ``status`` is ``succeeded`` or ``failed`` (the attempt's terminal outcome,
    mirroring the durable row). ``cost_usd`` is the usage-priced amount from
    the registry's reviewed rates.
    """
    AI_REQUESTS_TOTAL.labels(task=task, provider=provider, model=model, status=status).inc()
    AI_REQUEST_DURATION_SECONDS.labels(task=task, provider=provider, model=model).observe(
        latency_ms / 1000
    )
    AI_TOKENS_TOTAL.labels(task=task, provider=provider, model=model, direction="input").inc(
        input_tokens
    )
    AI_TOKENS_TOTAL.labels(task=task, provider=provider, model=model, direction="output").inc(
        output_tokens
    )
    if cost_usd > 0:
        AI_COST_TOTAL.labels(task=task, provider=provider, model=model).inc(cost_usd)


def observe_ai_validation_failure(*, task: str, provider: str, model: str) -> None:
    """Record one malformed/unvalidatable provider output (before repair/retry)."""
    AI_VALIDATION_FAILURES_TOTAL.labels(task=task, provider=provider, model=model).inc()


def observe_ai_retry(*, task: str, provider: str, model: str) -> None:
    """Record one bounded retry dispatch (attempts after the first)."""
    AI_RETRIES_TOTAL.labels(task=task, provider=provider, model=model).inc()


def observe_ai_fallback(*, task: str, provider: str, model: str) -> None:
    """Record one reviewed fallback dispatch under the task's fallback policy."""
    AI_FALLBACKS_TOTAL.labels(task=task, provider=provider, model=model).inc()


def observe_ai_budget_denial(*, task: str) -> None:
    """Record one monthly organisation AI budget denial before dispatch."""
    AI_BUDGET_DENIALS_TOTAL.labels(task=task).inc()


def update_queue_depths() -> None:
    """Refresh the Dramatiq queue-depth gauges from the process broker.

    Dramatiq stores each queue as a Redis list (``LLEN dramatiq:<queue>``);
    ``RedisBroker.get_queue_message_counts`` reads those lengths synchronously,
    so the API lifespan calls this through ``asyncio.to_thread`` every 30 s.
    A Redis outage leaves the gauges stale instead of failing the scrape and is
    logged once per outage/recovery (never every 30 s).
    """
    global _queue_depth_refresh_failed
    broker = dramatiq.get_broker()
    try:
        # ``get_queue_message_counts`` is a RedisBroker method, not part of the
        # base Broker contract (dramatiq's generic annotations do not describe
        # it); the API and worker always install the Redis broker.
        counts = cast(Any, broker).get_queue_message_counts(*TEMPLATE_QUEUES)
    except Exception:
        if not _queue_depth_refresh_failed:
            logger.warning("queue_depth.refresh_failed")
            _queue_depth_refresh_failed = True
        return
    if _queue_depth_refresh_failed:
        logger.info("queue_depth.refresh_recovered")
        _queue_depth_refresh_failed = False
    for queue in TEMPLATE_QUEUES:
        DRAMATIQ_QUEUE_DEPTH.labels(queue=queue).set(counts.get(queue, 0))
