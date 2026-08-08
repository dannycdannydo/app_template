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

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import RequestResponseEndpoint

router = APIRouter(tags=["metrics"])

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
