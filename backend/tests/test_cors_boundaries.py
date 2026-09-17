"""Browser-origin (CORS) boundary tests for the API (plan P9, BP §30).

The browser calls the API cross-origin from the configured frontend origin, so
the explicit allowlist is a security control: an authorised origin gets a
preflight and a cross-origin response, and any other origin is refused. These
drive the real ``CORSMiddleware`` wired by ``create_app`` (the same code the
deployment runs) over ASGI, with no backend network.
"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app

ALLOWED_ORIGIN = "http://localhost:5173"  # the default test/production-shaped origin
FORBIDDEN_ORIGIN = "https://evil.example.com"


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_preflight_from_an_allowed_origin_is_permitted() -> None:
    app = create_app()
    async with _client(app) as client:
        response = await client.options(
            "/api/v1/ai/ask",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,x-org-id",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    allowed_methods = response.headers["access-control-allow-methods"]
    assert "POST" in allowed_methods
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "content-type", "x-org-id"):
        assert header in allowed_headers


async def test_preflight_from_a_forbidden_origin_is_refused() -> None:
    app = create_app()
    async with _client(app) as client:
        response = await client.options(
            "/api/v1/ai/ask",
            headers={
                "Origin": FORBIDDEN_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_simple_request_from_a_forbidden_origin_gets_no_cors_grant() -> None:
    """A non-preflighted cross-origin request is not granted the header, so the
    browser blocks the response."""
    app = create_app()
    async with _client(app) as client:
        response = await client.get(
            "/health",
            headers={"Origin": FORBIDDEN_ORIGIN},
        )
    assert "access-control-allow-origin" not in response.headers


async def test_simple_request_from_an_allowed_origin_is_granted() -> None:
    app = create_app()
    async with _client(app) as client:
        response = await client.get(
            "/health",
            headers={"Origin": ALLOWED_ORIGIN},
        )
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
