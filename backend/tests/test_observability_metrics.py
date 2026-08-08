"""Prometheus metrics tests (Scope §6.1, blueprint §28).

``GET /metrics`` is public (like ``/health`` and ``/ready``) and returns the
Prometheus text exposition format. The request middleware records counters and
latency histograms (path labels normalised so ids do not explode cardinality),
and the durable job service increments the job counters at the point the
durable row changes state (enqueue / succeed / fail). Counters are
process-wide, so every assertion is a before/after comparison against the
``/metrics`` output rather than an absolute value.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import dramatiq
from dramatiq.brokers.stub import StubBroker
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession
from tests.context_helpers import ContextState, FakeSession, make_job

from app.main import create_app
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import JobStatus
from app.observability.metrics import normalise_path

_ALL_METRICS = (
    "http_requests_total",
    "http_request_duration_seconds",
    "jobs_enqueued_total",
    "jobs_succeeded_total",
    "jobs_failed_total",
)


def _client_for(app: Any) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


def _metric_value(body: str, metric: str, labels: dict[str, str]) -> float:
    """Extract one sample's value from Prometheus text output (0.0 if absent)."""
    label_part = "{" + ",".join(f'{key}="{value}"' for key, value in labels.items()) + "}"
    prefix = metric + label_part
    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line.split()[-1])
    return 0.0


async def _fetch_metrics() -> str:
    async with _client_for(create_app()) as client:
        response: Response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return response.text


# --- /metrics endpoint -------------------------------------------------------


async def test_metrics_endpoint_returns_prometheus_text_format() -> None:
    body = await _fetch_metrics()
    for metric in _ALL_METRICS:
        assert f"# TYPE {metric} " in body
        assert f"# HELP {metric} " in body


async def test_metrics_does_not_count_its_own_scrapes() -> None:
    """The scraper would otherwise double its own counter on every poll."""
    body = await _fetch_metrics()
    labels = {"method": "GET", "path": "/metrics", "status_code": "200"}
    assert _metric_value(body, "http_requests_total", labels) == 0.0


# --- Request instrumentation --------------------------------------------------


async def test_request_counter_and_latency_histogram_record_requests() -> None:
    before = await _fetch_metrics()
    async with _client_for(create_app()) as client:
        response: Response = await client.get("/health")
    assert response.status_code == 200
    after = await _fetch_metrics()

    request_labels = {"method": "GET", "path": "/health", "status_code": "200"}
    assert (
        _metric_value(after, "http_requests_total", request_labels)
        == _metric_value(before, "http_requests_total", request_labels) + 1
    )
    histogram_labels = {"method": "GET", "path": "/health"}
    assert (
        _metric_value(after, "http_request_duration_seconds_count", histogram_labels)
        == _metric_value(before, "http_request_duration_seconds_count", histogram_labels) + 1
    )


async def test_path_labels_collapse_ids() -> None:
    """URL segments that are uuids or numbers collapse to ``{id}``."""
    before = await _fetch_metrics()
    record_id = str(uuid.uuid4())
    async with _client_for(create_app()) as client:
        response: Response = await client.get(f"/api/v1/nope/{record_id}")
    assert response.status_code == 404  # unmatched route, but still counted
    after = await _fetch_metrics()

    labels = {
        "method": "GET",
        "path": "/api/v1/nope/{id}",
        "status_code": "404",
    }
    assert (
        _metric_value(after, "http_requests_total", labels)
        == _metric_value(before, "http_requests_total", labels) + 1
    )


def test_normalise_path_collapses_ids() -> None:
    assert normalise_path(f"/api/v1/files/{uuid.uuid4()}/download-url") == (
        "/api/v1/files/{id}/download-url"
    )
    assert normalise_path("/api/v1/records") == "/api/v1/records"
    assert normalise_path("/users/42") == "/users/{id}"
    assert normalise_path("/health") == "/health"


# --- Job counters ------------------------------------------------------------


async def test_job_counters_increment_through_the_durable_job_service() -> None:
    """Enqueue/succeed/fail drive the counters via the real service functions."""
    dramatiq.set_broker(StubBroker())
    organisation_id = uuid.uuid4()
    task = dramatiq.actor(lambda: None)
    labels = {"job_type": "file.processing"}

    before = await _fetch_metrics()

    # Enqueue: the durable row is written and the task enqueued.
    enqueue_state = ContextState()
    await jobs_service.create_and_enqueue(
        cast(AsyncSession, FakeSession(enqueue_state)),
        organisation_id=organisation_id,
        job_type="file.processing",
        input_reference="file-1",
        actor_user_id=None,
        task=task,
    )

    # Succeed: the staged queued row transitions to succeeded.
    succeed_state = ContextState()
    succeeded_job = make_job(organisation_id, status=JobStatus.QUEUED)
    succeed_state.jobs.append(succeeded_job)
    succeed_state.lookup_queue = [succeeded_job]
    await jobs_service.succeed(
        cast(AsyncSession, FakeSession(succeed_state)), job_id=succeeded_job.id
    )

    # Fail: the staged queued row transitions to failed with the error surface.
    failed_state = ContextState()
    failed_job = make_job(organisation_id, status=JobStatus.QUEUED)
    failed_state.jobs.append(failed_job)
    failed_state.lookup_queue = [failed_job]
    await jobs_service.fail(
        cast(AsyncSession, FakeSession(failed_state)),
        job_id=failed_job.id,
        error_code="file_verification_failed",
        error_message="verification failed",
    )

    after = await _fetch_metrics()
    assert (
        _metric_value(after, "jobs_enqueued_total", labels)
        == _metric_value(before, "jobs_enqueued_total", labels) + 1
    )
    assert (
        _metric_value(after, "jobs_succeeded_total", labels)
        == _metric_value(before, "jobs_succeeded_total", labels) + 1
    )
    assert (
        _metric_value(after, "jobs_failed_total", labels)
        == _metric_value(before, "jobs_failed_total", labels) + 1
    )
