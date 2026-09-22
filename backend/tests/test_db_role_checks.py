"""Unit tests for the runtime database-role check (plan P4).

The real-PostgreSQL proof lives in ``test_db_role_checks_db.py``. These tests
cover the pure report logic and the production-only startup gate, which need no
database.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import get_settings
from app.db.role_checks import (
    DatabaseRoleCheckError,
    RoleReport,
    _runtime_can_assume_operator,  # type: ignore[reportPrivateUsage]
    enforce_production_runtime_role,
    verify_production_coordinator_role,
    verify_production_database_roles,
)


def _report(**overrides: object) -> RoleReport:
    defaults: dict[str, object] = {
        "label": "runtime",
        "role": "app_runtime",
        "rolsuper": False,
        "rolbypassrls": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "owned_table_count": 0,
        "privileged_memberships": (),
    }
    defaults.update(overrides)
    return RoleReport(**defaults)  # type: ignore[arg-type]


def test_safe_runtime_role_has_no_problems() -> None:
    assert _report().problems(expected_role="app_runtime") == ()


def test_report_flags_expected_role_mismatch() -> None:
    problems = _report(role="app_owner").problems(expected_role="app_runtime")
    assert any("expected 'app_runtime'" in problem for problem in problems)


def test_report_flags_every_privilege_violation() -> None:
    problems = _report(
        rolsuper=True,
        rolbypassrls=True,
        rolcreatedb=True,
        rolcreaterole=True,
        owned_table_count=3,
        privileged_memberships=("app_owner", "some_superuser"),
    ).problems()
    rendered = " | ".join(problems)
    assert "is a superuser" in rendered
    assert "carries BYPASSRLS" in rendered
    assert "carries CREATEDB" in rendered
    assert "carries CREATEROLE" in rendered
    assert "owns 3 table(s)" in rendered
    assert "app_owner" in rendered
    assert "some_superuser" in rendered


async def test_production_check_is_a_noop_outside_production() -> None:
    settings = get_settings().model_copy(update={"app_env": "development"})
    await verify_production_database_roles(
        settings,
        runtime_engine=cast(AsyncEngine, None),
        coordinator_engine=cast(AsyncEngine, None),
    )


class _FakeDriverError(Exception):
    """Stand-in for a DBAPI driver exception carrying a PostgreSQL SQLSTATE."""

    def __init__(self, message: str, *, sqlstate: str | None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakeProbeConnection:
    """Minimal async double for the raw ``SET ROLE`` denial probe."""

    def __init__(self, *, error: DBAPIError | None = None) -> None:
        self.error = error
        self.statements: list[str] = []

    async def execute(self, statement: object) -> None:
        self.statements.append(str(statement))
        if self.error is not None:
            raise self.error


def _probe_connection(sqlstate: str | None) -> AsyncConnection:
    error = DBAPIError(
        "SET ROLE app_operator",
        {},
        _FakeDriverError("probe failed", sqlstate=sqlstate),
    )
    return cast(AsyncConnection, _FakeProbeConnection(error=error))


async def test_assume_operator_probe_reads_insufficient_privilege_as_denial() -> None:
    assert await _runtime_can_assume_operator(_probe_connection("42501")) is False


async def test_assume_operator_probe_fails_closed_on_unrelated_dbapi_error() -> None:
    with pytest.raises(DatabaseRoleCheckError):
        await _runtime_can_assume_operator(_probe_connection("57014"))


async def test_assume_operator_probe_returns_true_when_set_role_succeeds() -> None:
    fake = _FakeProbeConnection()
    assert await _runtime_can_assume_operator(cast(AsyncConnection, fake)) is True
    assert fake.statements == ["SET ROLE app_operator", "RESET ROLE"]


async def test_create_app_lifespan_runs_role_check_before_yielding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    production_settings = get_settings().model_copy(update={"app_env": "production"})
    monkeypatch.setattr(main_module, "get_settings", lambda: production_settings)

    calls: list[str] = []

    async def fake_verify(
        settings: object, *, runtime_engine: object, coordinator_engine: object = None
    ) -> None:
        calls.append(production_settings.app_env)

    monkeypatch.setattr("app.db.role_checks.verify_production_database_roles", fake_verify)

    async def noop_refresh_loop() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(main_module, "_queue_depth_refresh_loop", noop_refresh_loop)

    app = main_module.create_app()
    async with app.router.lifespan_context(app):
        assert calls == ["production"]


async def test_create_app_lifespan_aborts_before_serving_when_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    production_settings = get_settings().model_copy(update={"app_env": "production"})
    monkeypatch.setattr(main_module, "get_settings", lambda: production_settings)

    async def failing_verify(
        settings: object, *, runtime_engine: object, coordinator_engine: object = None
    ) -> None:
        raise DatabaseRoleCheckError("runtime", "app_owner", ["owns 1 table(s) in schema public"])

    monkeypatch.setattr("app.db.role_checks.verify_production_database_roles", failing_verify)

    app = main_module.create_app()
    with pytest.raises(DatabaseRoleCheckError):
        async with app.router.lifespan_context(app):
            raise AssertionError("lifespan must not yield when the role check fails")


# --- Plan P4 aggregate evidence: the worker and coordinator gates --------------


async def test_coordinator_gate_is_a_noop_outside_production() -> None:
    settings = get_settings().model_copy(update={"app_env": "development"})
    # A non-production call must not touch the engine at all.
    await verify_production_coordinator_role(settings, coordinator_engine=cast(AsyncEngine, None))


async def test_coordinator_gate_verifies_the_coordinator_engine_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    async def fake_verify(engine: object) -> None:
        seen.append(engine)

    monkeypatch.setattr("app.db.role_checks.verify_coordinator_database_role", fake_verify)
    settings = get_settings().model_copy(update={"app_env": "production"})
    sentinel = cast(AsyncEngine, object())
    await verify_production_coordinator_role(settings, coordinator_engine=sentinel)
    assert seen == [sentinel]


def test_worker_gate_is_a_noop_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_factory(settings: object) -> object:
        raise AssertionError("a non-production worker must not build a role-check engine")

    monkeypatch.setattr("app.db.session.build_session_factory", forbidden_factory)
    settings = get_settings().model_copy(update={"app_env": "test"})
    enforce_production_runtime_role(settings)


def test_worker_gate_builds_verifies_and_disposes_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Engine:
        async def dispose(self) -> None:
            events.append("disposed")

    def fake_factory(settings: object) -> tuple[object, object]:
        events.append("built")
        return _Engine(), object()

    async def fake_verify(engine: object) -> None:
        events.append("verified")

    monkeypatch.setattr("app.db.session.build_session_factory", fake_factory)
    monkeypatch.setattr("app.db.role_checks.verify_runtime_database_role", fake_verify)
    settings = get_settings().model_copy(update={"app_env": "production"})
    enforce_production_runtime_role(settings)
    assert events == ["built", "verified", "disposed"]
