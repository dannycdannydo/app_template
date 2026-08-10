"""FastAPI application entry point (blueprint §5, §6, §13, §28).

The app is built by the ``create_app`` factory so tests and tooling can build
isolated instances. A module-level ``app`` is also exposed for uvicorn and for
the OpenAPI export used by the generated client pipeline.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any, cast
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.exceptions import APIError, ErrorDetail, ErrorResponse
from app.core.logging import bind_identity_context, configure_logging, current_request_id
from app.core.rate_limit import RateLimiter, get_rate_limiter
from app.modules.audit.router import router as audit_router
from app.modules.feature_flags.router import router as feature_flags_router
from app.modules.files.router import router as files_router
from app.modules.invitations.router import router as invitations_router
from app.modules.jobs.router import router as jobs_router
from app.modules.notifications.router import router as notifications_router
from app.modules.organisations.router import router as organisations_router
from app.modules.platform_admin.router import router as platform_admin_router
from app.modules.records.router import router as records_router
from app.modules.users.router import router as users_router
from app.modules.webhooks.router import router as webhooks_router
from app.observability.metrics import metrics_middleware
from app.observability.metrics import router as metrics_router
from app.observability.sentry import capture_exception, initialise_sentry

logger = structlog.get_logger()

_HTTP_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limit_exceeded",
}


def _field_path(error: Any) -> str:
    """Turn a pydantic ``loc`` tuple into a readable field path."""
    parts = [str(part) for part in error.get("loc", ()) if part != "body"]
    return ".".join(parts) or "request"


def _validation_details(errors: Sequence[Any]) -> list[ErrorDetail]:
    return [
        ErrorDetail(field=_field_path(error), message=str(error.get("msg", "Invalid value.")))
        for error in errors
    ]


async def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    api_error = cast(APIError, exc)
    return JSONResponse(
        status_code=api_error.status_code,
        content=api_error.as_error_response().model_dump(mode="json"),
        headers=api_error.headers,
    )


async def _handle_request_validation_error(request: Request, exc: Exception) -> JSONResponse:
    validation_error = cast(RequestValidationError, exc)
    response = ErrorResponse(
        code="validation_error",
        message="The request contains invalid data.",
        details=_validation_details(validation_error.errors()),
        request_id=current_request_id(),
    )
    return JSONResponse(status_code=422, content=response.model_dump(mode="json"))


async def _handle_pydantic_validation_error(request: Request, exc: Exception) -> JSONResponse:
    validation_error = cast(PydanticValidationError, exc)
    response = ErrorResponse(
        code="validation_error",
        message="The request contains invalid data.",
        details=_validation_details(validation_error.errors()),
        request_id=current_request_id(),
    )
    return JSONResponse(status_code=422, content=response.model_dump(mode="json"))


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    http_exception = cast(StarletteHTTPException, exc)
    response = ErrorResponse(
        code=_HTTP_ERROR_CODES.get(
            http_exception.status_code, f"http_{http_exception.status_code}"
        ),
        message=str(http_exception.detail),
        request_id=current_request_id(),
    )
    return JSONResponse(
        status_code=http_exception.status_code, content=response.model_dump(mode="json")
    )


async def _handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    # Blueprint §13: unexpected exceptions return a safe generic message and
    # are recorded in Sentry (blueprint §28 worker/API failure visibility).
    # capture_exception is a no-op when no DSN is configured.
    logger.exception("unhandled_exception", error=str(exc))
    capture_exception(exc)
    response = ErrorResponse(
        code="internal_error",
        message="An unexpected error occurred.",
        request_id=current_request_id(),
    )
    return JSONResponse(status_code=500, content=response.model_dump(mode="json"))


async def _request_id_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Bind a request ID to the logging context and echo it on the response."""
    request_id = request.headers.get("x-request-id") or uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request_failed", method=request.method, path=request.url.path)
        raise
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
    duration_ms = (perf_counter() - started) * 1000
    # The router runs in a child context (BaseHTTPMiddleware task group), so
    # identity contextvars bound by the auth dependencies never reach this
    # middleware's own context. The dependencies record the caller on
    # ``request.state`` (shared scope); rebind here so the ``request_finished``
    # line carries the blueprint §28 identity fields too.
    user_id = getattr(request.state, "user_id", None)
    organisation_id = getattr(request.state, "organisation_id", None)
    if user_id is not None:
        bind_identity_context(user_id=user_id, organisation_id=organisation_id)
    logger.info(
        "request_finished",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=round(duration_ms, 2),
    )
    structlog.contextvars.clear_contextvars()
    return response


async def _rate_limit_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Apply a distributed coarse API limit before request work begins."""
    if request.url.path.startswith("/api/v1/"):
        client_host = request.client.host if request.client else "unknown"
        limiter: RateLimiter = get_rate_limiter()
        try:
            await limiter.enforce(key=f"api:ip:{client_host}", limit=300, window_seconds=60)
        except APIError as exc:
            # Exceptions raised by BaseHTTPMiddleware do not traverse FastAPI's
            # endpoint exception handlers, so preserve the standard envelope.
            return await _handle_api_error(request, exc)
    return await call_next(request)


def _register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(APIError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(PydanticValidationError, _handle_pydantic_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_exception)


class _TrustedHostWithPublicExemptMiddleware:
    """Host-header allowlist that exempts the public health/metrics surface.

    Starlette's ``TrustedHostMiddleware`` rejects any Host header outside the
    configured allowlist (DNS-rebinding / Host-header injection protection).
    The public, non-sensitive endpoints (``/health``, ``/ready``,
    ``/metrics``) must stay reachable from infrastructure probes — the
    compose healthcheck, load balancers and orchestrators target the
    container directly and cannot present the public Host header — so they
    skip the allowlist check; every other path keeps the strict allowlist.
    """

    _PUBLIC_EXEMPT_PATHS = frozenset({"/health", "/ready", "/metrics"})

    def __init__(self, app: Any, *, allowed_hosts: list[str]) -> None:
        self.app = app
        self._host_check = TrustedHostMiddleware(app, allowed_hosts=allowed_hosts)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("path") in self._PUBLIC_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        await self._host_check(scope, receive, send)


def _register_middleware(
    app: FastAPI, *, cors_allowed_origins: list[str], trusted_hosts: list[str]
) -> None:
    """Register request middleware, including the explicit browser-origin policy."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Org-Id", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(
        _TrustedHostWithPublicExemptMiddleware,
        allowed_hosts=trusted_hosts,
    )
    # Metrics instrument the whole request path (rate limits and auth failures
    # included) but skip the /metrics scrape itself.
    app.add_middleware(BaseHTTPMiddleware, dispatch=metrics_middleware)
    app.add_middleware(BaseHTTPMiddleware, dispatch=_rate_limit_middleware)
    app.add_middleware(BaseHTTPMiddleware, dispatch=_request_id_middleware)


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    configure_logging(log_level=settings.log_level, json_logs=not settings.debug)
    if settings.sentry_dsn:
        initialise_sentry(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        logger.info("application_started", app=settings.app_name, env=settings.app_env)
        yield
        logger.info("application_stopped", app=settings.app_name)

    is_production = settings.app_env == "production"
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )
    _register_exception_handlers(app)
    _register_middleware(
        app,
        cors_allowed_origins=settings.cors_allowed_origins,
        trusted_hosts=settings.trusted_hosts,
    )
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(users_router)
    app.include_router(organisations_router)
    app.include_router(platform_admin_router)
    app.include_router(records_router)
    app.include_router(audit_router)
    app.include_router(files_router)
    app.include_router(jobs_router)
    app.include_router(notifications_router)
    app.include_router(invitations_router)
    app.include_router(feature_flags_router)
    app.include_router(webhooks_router)
    return app


app = create_app()
