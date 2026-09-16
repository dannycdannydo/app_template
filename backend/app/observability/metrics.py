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
from datetime import UTC, datetime, timedelta
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import RequestResponseEndpoint

from app.modules.outbox.contracts import (
    EVENT_TYPE_AI_RETENTION,
    EVENT_TYPE_JOB_DISPATCH,
    EVENT_TYPE_OUTBOX_CLEANUP_COMPLETED,
    EVENT_TYPE_TRANSFER_RECONCILE,
)
from app.modules.outbox.models import OutboxEventStatus
from app.modules.outbox.queries import (
    oldest_due_event_statement,
    outbox_metric_rows_statement,
    stale_queued_job_count_statement,
    stale_running_job_count_statement,
)

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

# v0.8 large-file transfer observability (Scope §2.5/§6.7): low-cardinality
# counters for mode selection, lifecycle outcomes and the reconciliation sweep
# plus a cleanup backlog gauge. Labels are mode/provider ids and safe outcomes
# only — never object keys, external file ids, gs:// URIs, managed signed
# URLs, request ids or content (BP §28, Scope §2.3).
AI_TRANSFER_SELECTIONS_TOTAL = Counter(
    "ai_transfer_selections_total",
    "Selected large-file transfer modes by mode and provider",
    ["mode", "provider"],
)
AI_TRANSFER_RECONCILIATION_TOTAL = Counter(
    "ai_transfer_reconciliation_total",
    "Reconciliation sweep outcomes by provider and result",
    ["provider", "result"],
)
AI_TRANSFER_OUTCOMES_TOTAL = Counter(
    "ai_transfer_outcomes_total",
    "Transfer lifecycle outcomes by mode, provider and result",
    ["mode", "provider", "result"],
)
AI_TRANSFER_CLEANUP_BACKLOG = Gauge(
    "ai_transfer_cleanup_backlog",
    "Provider-file references currently waiting on the reconciliation sweep",
    ["mode"],
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

# These gauges are refreshed from PostgreSQL, the source of truth for durable
# scheduling. Labels are only the finite event/status vocabulary; ids, tenant
# values, payloads and error text remain in database rows and never metrics.
OUTBOX_EVENTS = Gauge(
    "outbox_events",
    "Durable outbox rows by lifecycle status and event type",
    ["status", "event_type"],
)
OUTBOX_OLDEST_DUE_AGE_SECONDS = Gauge(
    "outbox_oldest_due_age_seconds",
    "Age in seconds of the oldest due pending outbox event",
)
STALE_QUEUED_JOBS = Gauge(
    "stale_queued_jobs",
    "Queued jobs eligible for durable dispatch reconciliation",
)
STALE_RUNNING_JOBS = Gauge(
    "stale_running_jobs",
    "Lease-expired running jobs eligible for durable recovery",
)

#: Rate-limit the refresh-failure log to one line per outage/recovery.
_queue_depth_refresh_failed = False
_outbox_metrics_refresh_failed = False

_OUTBOX_EVENT_TYPES = (
    EVENT_TYPE_JOB_DISPATCH,
    EVENT_TYPE_AI_RETENTION,
    EVENT_TYPE_TRANSFER_RECONCILE,
    EVENT_TYPE_OUTBOX_CLEANUP_COMPLETED,
)

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


def observe_ai_transfer_selection(*, mode: str, provider: str) -> None:
    """Record one deterministic non-inline transfer-mode selection (Scope §6.7).

    ``mode`` and ``provider`` are low-cardinality registry ids; the durable
    ``ai_attachment_references`` rows are the per-request source of truth.
    """
    AI_TRANSFER_SELECTIONS_TOTAL.labels(mode=mode, provider=provider).inc()


def observe_ai_transfer_reconciliation(*, provider: str, result: str) -> None:
    """Record one reconciliation outcome: ``deleted`` or ``failed``.

    A ``failed`` outcome leaves the row stamped for the bounded backoff
    window, so the same copy is never re-attempted every sweep run (Scope
    §2.5/§6.7).
    """
    AI_TRANSFER_RECONCILIATION_TOTAL.labels(provider=provider, result=result).inc()


def observe_ai_transfer_outcome(*, mode: str, provider: str, result: str) -> None:
    """Record one transfer lifecycle outcome (Scope §6.7 checkbox 3).

    ``result`` is one of ``staged``, ``reused``, ``deleted`` or ``failed``;
    the labels are low-cardinality registry ids and a safe outcome — never
    object keys, external file ids, cloud URIs, managed signed URLs, request
    ids, exception text or content (BP §28). ``expired`` stays audit-only
    (request-scoped count with no single mode/provider).
    """
    AI_TRANSFER_OUTCOMES_TOTAL.labels(mode=mode, provider=provider, result=result).inc()


def set_ai_transfer_cleanup_backlog(*, count: int) -> None:
    """Refresh the provider-file cleanup backlog gauge (Scope §6.7).

    Counts only provider-hosted copies awaiting the reconciliation sweep;
    managed signed URLs and Vertex GCS staging objects are never counted
    (Scope §2.5). The gauge is process-local and refreshed per sweep run.
    """
    AI_TRANSFER_CLEANUP_BACKLOG.labels(mode="provider_upload").set(count)


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


async def refresh_outbox_metrics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    reconciliation_threshold_seconds: int,
    reconciliation_cooldown_seconds: int,
) -> None:
    """Refresh durable-delivery gauges without making a metrics scrape fail.

    PostgreSQL aggregation returns only safe, low-cardinality values. A database
    outage retains the last samples and emits a rate-limited generic log event;
    exceptions are never rendered because connection strings can contain
    credentials.
    """
    global _outbox_metrics_refresh_failed
    now = datetime.now(UTC)
    published_before = now - timedelta(
        seconds=max(reconciliation_threshold_seconds, reconciliation_cooldown_seconds)
    )
    try:
        async with session_factory() as session:
            rows = (await session.execute(outbox_metric_rows_statement())).all()
            oldest_due = await session.scalar(oldest_due_event_statement(now=now))
            stale_count = await session.scalar(
                stale_queued_job_count_statement(published_before=published_before)
            )
            stale_running_count = await session.scalar(
                stale_running_job_count_statement(
                    lease_expired_before=now, published_before=published_before
                )
            )
    except Exception:
        if not _outbox_metrics_refresh_failed:
            logger.warning("outbox_metrics.refresh_failed")
            _outbox_metrics_refresh_failed = True
        return
    if _outbox_metrics_refresh_failed:
        logger.info("outbox_metrics.refresh_recovered")
        _outbox_metrics_refresh_failed = False
    counts = {
        (str(status), event_type): count
        for status, event_type, count in rows
        if event_type in _OUTBOX_EVENT_TYPES
        and str(status) in {status.value for status in OutboxEventStatus}
    }
    for status in OutboxEventStatus:
        for event_type in _OUTBOX_EVENT_TYPES:
            OUTBOX_EVENTS.labels(status=status.value, event_type=event_type).set(
                counts.get((status.value, event_type), 0)
            )
    OUTBOX_OLDEST_DUE_AGE_SECONDS.set(
        max((now - oldest_due).total_seconds(), 0) if oldest_due is not None else 0
    )
    STALE_QUEUED_JOBS.set(stale_count or 0)
    STALE_RUNNING_JOBS.set(stale_running_count or 0)
