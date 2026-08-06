"""Tests for the standard API error format (blueprint §13, acceptance #4).

Domain exceptions are translated by central handlers into one structured error
format that always carries the request ID bound by the middleware. Requests go
through the full ASGI stack via ``httpx.AsyncClient`` + ``ASGITransport``.
"""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import BaseModel
from pytest import MonkeyPatch

from app.core.exceptions import ConflictError, NotFoundError, RateLimitExceeded
from app.main import create_app


def _client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def test_domain_exception_returns_standard_error_with_request_id() -> None:
    app = create_app()

    def _raise_not_found() -> None:
        raise NotFoundError(code="widget_not_found", message="The widget could not be found.")

    app.add_api_route("/_test/not-found", _raise_not_found, methods=["GET"])

    async with _client_for(app) as client:
        response: Response = await client.get("/_test/not-found")
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "widget_not_found"
        assert body["message"] == "The widget could not be found."
        assert body["details"] is None
        assert body["request_id"]
        assert response.headers["x-request-id"] == body["request_id"]


async def test_conflict_error_maps_to_409() -> None:
    app = create_app()

    def _raise_conflict() -> None:
        raise ConflictError(message="The record already exists.")

    app.add_api_route("/_test/conflict", _raise_conflict, methods=["GET"])

    async with _client_for(app) as client:
        response: Response = await client.get("/_test/conflict")
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "conflict"
        assert body["request_id"]


async def test_unexpected_exception_returns_generic_500_without_leaking_details() -> None:
    app = create_app()

    def _raise_broken() -> None:
        raise RuntimeError("internal secret detail")

    app.add_api_route("/_test/broken", _raise_broken, methods=["GET"])

    async with _client_for(app) as client:
        response: Response = await client.get("/_test/broken")
        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "internal_error"
        assert "secret detail" not in body["message"]
        assert body["request_id"]


async def test_request_validation_error_uses_standard_format() -> None:
    app = create_app()

    class Payload(BaseModel):
        name: str

    def _validate(payload: Payload) -> dict[str, str]:
        return {"ok": "yes"}

    app.add_api_route("/_test/validate", _validate, methods=["POST"])

    async with _client_for(app) as client:
        response: Response = await client.post("/_test/validate", json={"name": 123})
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "validation_error"
        assert body["details"] == [{"field": "name", "message": "Input should be a valid string"}]
        assert body["request_id"]


async def test_unknown_route_returns_standard_error_format() -> None:
    async with _client_for(create_app()) as client:
        response: Response = await client.get("/does/not/exist")
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "not_found"
        assert body["request_id"]


async def test_cors_allows_only_configured_browser_origins() -> None:
    async with _client_for(create_app()) as client:
        allowed = await client.options(
            "/api/v1/me",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        denied = await client.options(
            "/api/v1/me",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


async def test_api_security_headers_and_untrusted_hosts_are_rejected() -> None:
    async with _client_for(create_app()) as client:
        response = await client.get("/health")
        denied = await client.get("/health", headers={"Host": "evil.example"})

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["permissions-policy"] == "camera=(), geolocation=(), microphone=()"
    assert denied.status_code == 400


async def test_api_requests_are_rate_limited_before_endpoint_execution(monkeypatch: MonkeyPatch) -> None:
    calls: list[tuple[str, int, int]] = []

    class RejectingLimiter:
        async def enforce(self, *, key: str, limit: int, window_seconds: int) -> None:
            calls.append((key, limit, window_seconds))
            raise RateLimitExceeded(headers={"Retry-After": "60"})

    monkeypatch.setattr("app.main.get_rate_limiter", lambda: RejectingLimiter())
    app = create_app()
    app.add_api_route("/api/v1/_test/rate-limit", lambda: {"ok": True}, methods=["GET"])

    async with _client_for(app) as client:
        response = await client.get("/api/v1/_test/rate-limit")

    assert calls == [("api:ip:127.0.0.1", 300, 60)]
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.json()["code"] == "rate_limit_exceeded"
