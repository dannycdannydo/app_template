"""Sentry integration tests (Scope §6.1, blueprint §28).

The SDK is only ever initialised when ``SENTRY_DSN`` is set; without a DSN the
app boots with no Sentry and nothing is captured. All access to the SDK is
funneled through ``app.observability.sentry`` so tests replace that module's
``sentry_sdk`` global with a recording fake: init arguments, the active check
that gates capture, and the captured exceptions are all asserted against the
fake. Settings are cached per process, so the DSN is changed via the
environment plus a cache clear, restored afterwards.
"""

from __future__ import annotations

from typing import Any

from httpx import ASGITransport, AsyncClient, Response
from pytest import MonkeyPatch

from app.core.config import get_settings
from app.main import create_app
from app.observability.sentry import SentryWorkerMiddleware, capture_exception


class _FakeClient:
    def __init__(self, *, active: bool) -> None:
        self._active = active

    def is_active(self) -> bool:
        return self._active


class _FakeSentrySDK:
    """Recording stand-in for ``sentry_sdk`` (init args and captured exceptions)."""

    def __init__(self, *, active: bool = True) -> None:
        self._active = active
        self.init_calls: list[dict[str, Any]] = []
        self.captured: list[BaseException] = []

    def init(self, **kwargs: Any) -> None:
        self.init_calls.append(kwargs)

    def get_client(self) -> _FakeClient:
        return _FakeClient(active=self._active)

    def capture_exception(self, exception: BaseException) -> None:
        self.captured.append(exception)


def _client_for(app: Any) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


def _with_dsn(monkeypatch: MonkeyPatch) -> None:
    """Point the settings singleton at a configured DSN (restored by the test)."""
    monkeypatch.setenv("SENTRY_DSN", "https://example@sentry.example/1")
    get_settings.cache_clear()


def _without_dsn(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    get_settings.cache_clear()


# --- create_app() initialisation ----------------------------------------------


def test_create_app_initialises_sentry_when_dsn_set(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSentrySDK()
    monkeypatch.setattr("app.observability.sentry.sentry_sdk", fake)
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.25")
    _with_dsn(monkeypatch)
    try:
        create_app()
    finally:
        get_settings.cache_clear()

    assert len(fake.init_calls) == 1
    call = fake.init_calls[0]
    assert call["dsn"] == "https://example@sentry.example/1"
    assert call["environment"] == "staging"
    assert call["traces_sample_rate"] == 0.25


def test_sentry_environment_defaults_to_app_env(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSentrySDK()
    monkeypatch.setattr("app.observability.sentry.sentry_sdk", fake)
    monkeypatch.setenv("APP_ENV", "staging")
    _with_dsn(monkeypatch)
    try:
        create_app()
    finally:
        get_settings.cache_clear()

    assert fake.init_calls[0]["environment"] == "staging"


def test_create_app_without_dsn_never_initialises_sentry(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSentrySDK()
    monkeypatch.setattr("app.observability.sentry.sentry_sdk", fake)
    _without_dsn(monkeypatch)
    try:
        create_app()
    finally:
        get_settings.cache_clear()

    assert fake.init_calls == []


# --- API capture --------------------------------------------------------------


async def test_unhandled_request_exception_is_captured(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSentrySDK()
    monkeypatch.setattr("app.observability.sentry.sentry_sdk", fake)
    _with_dsn(monkeypatch)
    try:
        app = create_app()
    finally:
        get_settings.cache_clear()

    def _raise_broken() -> None:
        raise RuntimeError("internal secret detail")

    app.add_api_route("/_test/broken", _raise_broken, methods=["GET"])
    async with _client_for(app) as client:
        response: Response = await client.get("/_test/broken")
    assert response.status_code == 500
    assert len(fake.captured) == 1
    assert isinstance(fake.captured[0], RuntimeError)


async def test_unexpected_exception_without_dsn_captures_nothing(
    monkeypatch: MonkeyPatch,
) -> None:
    fake = _FakeSentrySDK(active=False)
    monkeypatch.setattr("app.observability.sentry.sentry_sdk", fake)
    _without_dsn(monkeypatch)
    try:
        app = create_app()
    finally:
        get_settings.cache_clear()

    def _raise_broken() -> None:
        raise RuntimeError("internal secret detail")

    app.add_api_route("/_test/broken", _raise_broken, methods=["GET"])
    async with _client_for(app) as client:
        response: Response = await client.get("/_test/broken")
    assert response.status_code == 500
    assert fake.captured == []


def test_capture_exception_is_a_noop_when_sdk_inactive(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSentrySDK(active=False)
    monkeypatch.setattr("app.observability.sentry.sentry_sdk", fake)
    capture_exception(RuntimeError("boom"))
    assert fake.captured == []


# --- Worker capture ------------------------------------------------------------


def test_worker_middleware_captures_unhandled_task_exceptions(
    monkeypatch: MonkeyPatch,
) -> None:
    fake = _FakeSentrySDK()
    monkeypatch.setattr("app.observability.sentry.sentry_sdk", fake)
    SentryWorkerMiddleware().after_process_message(None, None, exception=RuntimeError("boom"))
    assert len(fake.captured) == 1
    assert isinstance(fake.captured[0], RuntimeError)


def test_worker_middleware_noop_without_active_sdk(monkeypatch: MonkeyPatch) -> None:
    fake = _FakeSentrySDK(active=False)
    monkeypatch.setattr("app.observability.sentry.sentry_sdk", fake)
    middleware = SentryWorkerMiddleware()
    middleware.after_process_message(None, None, exception=RuntimeError("boom"))
    middleware.after_process_message(None, None)
    assert fake.captured == []
