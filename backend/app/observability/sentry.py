"""Sentry error tracking for the API and the worker (blueprint §28, §13).

Sentry is optional: nothing is captured and nothing is sent unless
``SENTRY_DSN`` is configured. The API initialises the SDK in ``create_app()``
(FastAPI integration) and captures unhandled request exceptions; the worker
captures unhandled task exceptions through a Dramatiq middleware. All access
to the SDK is funneled through :func:`capture_exception`, which is a safe
no-op when the SDK is not initialised, so application code never needs to
know whether Sentry is on.
"""

from __future__ import annotations

from typing import Any, cast

import sentry_sdk
from dramatiq.middleware import Middleware
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.types import Event, Hint

from app.core.logging import redact_sensitive_data


def _before_send(event: Event, hint: Hint) -> Event | None:
    """Apply the same bounded secret policy used by application logging."""
    return cast("Event", redact_sensitive_data(event))


def initialise_sentry(*, dsn: str, environment: str, traces_sample_rate: float) -> None:
    """Initialise the Sentry SDK with the FastAPI integration (idempotent).

    Called once per application startup from ``create_app()`` when a DSN is
    configured. The environment label defaults to ``APP_ENV`` at the settings
    layer; ``traces_sample_rate`` controls performance-trace sampling.
    """
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=False,
        before_send=_before_send,
        integrations=[FastApiIntegration()],
    )


def _is_active() -> bool:
    """Return whether the SDK is initialised (a DSN is configured)."""
    try:
        client = sentry_sdk.get_client()
    except Exception:
        return False
    return bool(getattr(client, "is_active", lambda: True)())


def capture_exception(exception: BaseException) -> None:
    """Capture ``exception`` in Sentry; a no-op when the SDK is not active.

    Application error handlers call this for unexpected (500) failures and the
    worker middleware for unhandled task exceptions. The SDK is guarded so an
    uninitialised SDK never raises into the error path it is supposed to
    observe; the guard also keeps the no-DSN test suite free of captures.
    """
    if not _is_active():
        return
    sentry_sdk.capture_exception(exception)


class SentryWorkerMiddleware(Middleware):
    """Capture unhandled worker exceptions in Sentry (blueprint §28).

    Mirrors the durable job failure record: the job row and audit event are
    the source of truth for business recovery, Sentry is the alerting
    surface. A no-op when the SDK is not initialised.
    """

    def after_process_message(
        self,
        broker: Any,
        message: Any,
        *,
        result: Any = None,
        exception: BaseException | None = None,
    ) -> None:
        if exception is not None:
            capture_exception(exception)
