"""Tests for the /health and /ready endpoints (blueprint §28).

Uses ``httpx.AsyncClient`` with an ``ASGITransport`` so the full ASGI stack
(middleware and exception handlers) is exercised without needing a live
database: the ``get_db`` dependency is overridden with a fake session.
"""

from collections.abc import AsyncIterator

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import get_db
from app.main import create_app


class _DatabaseUnavailableError(SQLAlchemyError):
    """Simulates a failed connection check without touching a real database."""


class _FakeSession:
    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    async def execute(self, statement: object) -> None:
        if self._fail:
            raise _DatabaseUnavailableError("connection refused")


async def _override_get_db_ok() -> AsyncIterator[_FakeSession]:
    yield _FakeSession(fail=False)


async def _override_get_db_failing() -> AsyncIterator[_FakeSession]:
    yield _FakeSession(fail=True)


def _client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def test_health_returns_ok() -> None:
    async with _client_for(create_app()) as client:
        response: Response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


async def test_ready_returns_ok_when_database_reachable() -> None:
    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db_ok
    async with _client_for(app) as client:
        response: Response = await client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


async def test_ready_returns_503_when_database_unreachable() -> None:
    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db_failing
    async with _client_for(app) as client:
        response: Response = await client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["code"] == "database_unavailable"
        assert body["message"]
        assert body["request_id"]


async def test_public_health_surface_ignores_host_allowlist_for_probes() -> None:
    """backup-and-recovery run B (defect D3): infrastructure probes — the
    compose healthcheck, load balancers, orchestrators — hit ``/health``,
    ``/ready`` and ``/metrics`` with a container-local Host header that is
    not in ``TRUSTED_HOSTS``. The public, non-sensitive surface must not be
    rejected by the Host allowlist; everything else keeps it."""
    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db_ok
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://probe",
    ) as client:
        ready: Response = await client.get("/ready")
        assert ready.status_code == 200
        health: Response = await client.get("/health")
        assert health.status_code == 200
        metrics: Response = await client.get("/metrics")
        assert metrics.status_code == 200


async def test_host_allowlist_still_rejects_non_public_paths() -> None:
    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db_ok
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://probe",
    ) as client:
        response: Response = await client.get("/definitely-not-a-public-path")
        assert response.status_code == 400
