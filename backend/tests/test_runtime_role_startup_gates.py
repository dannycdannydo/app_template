"""Entrypoint wiring for the production runtime-role gates (plan P4).

Plan P4 requires that no normal API or worker path runs on the owner, a
superuser or a ``BYPASSRLS`` credential. The API enforces this from
``create_app``'s lifespan (covered by ``test_db_role_checks.py``); these tests
prove the Dramatiq worker and the outbox coordinator invoke their blocking gate
at startup, before they install the broker or register actors, so an unsafe
credential aborts the process rather than silently defeating RLS for background
work.

The gates' catalogue logic is covered by the unit and real-PostgreSQL suites;
these tests fix only the wiring, with the heavy broker/task imports stubbed.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

_WORKER_TASK_MODULES = (
    "app.ai.execution",
    "app.ai.persistence.tasks",
    "app.modules.files.tasks",
    "app.modules.jobs.tasks",
    "app.modules.notifications.tasks",
)


@pytest.fixture
def worker_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import ``app.workers`` without disturbing the suite's StubBroker.

    ``app.workers`` calls ``configure_worker`` at import, which installed the
    real Redis broker; block that swap and stub the heavy task modules so the
    entrypoint import stays cheap. Tests then drive ``configure_worker``
    themselves.
    """
    import dramatiq

    def noop_set_broker(_broker: object) -> None:
        return None

    monkeypatch.setattr(dramatiq, "set_broker", noop_set_broker)
    for name in _WORKER_TASK_MODULES:
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    import app.workers as workers

    return workers


def test_worker_enforces_the_role_before_installing_the_broker(
    monkeypatch: pytest.MonkeyPatch, worker_module: ModuleType
) -> None:
    workers = worker_module
    events: list[str] = []

    def record_logging(**_kwargs: object) -> None:
        events.append("logging")

    def record_role(_settings: object) -> None:
        events.append("role")

    def record_broker() -> object:
        events.append("broker")
        return object()

    def record_set_broker(_broker: object) -> None:
        events.append("set_broker")

    monkeypatch.setattr(workers, "configure_logging", record_logging)
    monkeypatch.setattr(workers, "enforce_production_runtime_role", record_role)
    monkeypatch.setattr(workers, "build_broker", record_broker)
    monkeypatch.setattr(workers.dramatiq, "set_broker", record_set_broker)

    workers.configure_worker()

    assert "role" in events
    assert events.index("role") < events.index("broker")


def test_worker_startup_aborts_when_the_gate_rejects_the_credential(
    monkeypatch: pytest.MonkeyPatch, worker_module: ModuleType
) -> None:
    workers = worker_module
    from app.db.role_checks import DatabaseRoleCheckError

    broker_installed: list[str] = []

    def noop_logging(**_kwargs: object) -> None:
        return None

    def failing_gate(_settings: object) -> None:
        raise DatabaseRoleCheckError("runtime", "app_owner", ["owns 1 table(s) in schema public"])

    def record_broker() -> object:
        broker_installed.append("broker")
        return object()

    def noop_set_broker(_broker: object) -> None:
        return None

    monkeypatch.setattr(workers, "configure_logging", noop_logging)
    monkeypatch.setattr(workers, "enforce_production_runtime_role", failing_gate)
    monkeypatch.setattr(workers, "build_broker", record_broker)
    monkeypatch.setattr(workers.dramatiq, "set_broker", noop_set_broker)

    with pytest.raises(DatabaseRoleCheckError):
        workers.configure_worker()
    assert broker_installed == [], "the worker must abort before installing the broker"


async def test_coordinator_enforces_the_role_before_building_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.job_coordinator.loop as loop_module

    events: list[str] = []

    class _Settings:
        app_env = "test"
        log_level = "INFO"
        debug = True
        coordinator_publication_batch_size = 10
        coordinator_idle_poll_seconds = 1.0
        coordinator_publication_lease_seconds = 60.0
        coordinator_publication_backoff_initial_seconds = 1.0
        coordinator_publication_backoff_max_seconds = 60.0
        job_reconcile_threshold_seconds = 900.0
        job_reconcile_cooldown_seconds = 900.0
        maintenance_ai_retention_interval_hours = 24.0
        maintenance_transfer_reconcile_interval_hours = 24.0
        outbox_retention_days = 7
        outbox_cleanup_batch_size = 100
        outbox_cleanup_interval_hours = 1.0

    async def fake_gate(_settings: object, *, coordinator_engine: object) -> None:
        events.append("role")

    async def fake_run_coordinator(_session_factory: object, **_kwargs: object) -> None:
        events.append("run")

    def fake_session_factory() -> object:
        return object()

    def noop_logging(**_kwargs: object) -> None:
        return None

    def record_broker() -> object:
        events.append("broker")
        return object()

    def record_registry() -> object:
        events.append("registry")
        return object()

    def noop_set_broker(_broker: object) -> None:
        return None

    monkeypatch.setattr("app.db.role_checks.verify_production_coordinator_role", fake_gate)
    monkeypatch.setattr("app.db.session.coordinator_engine", object())
    monkeypatch.setattr("app.db.session.coordinator_session_factory", fake_session_factory)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _Settings())
    monkeypatch.setattr("app.core.logging.configure_logging", noop_logging)
    monkeypatch.setattr("app.broker.build_broker", record_broker)
    monkeypatch.setattr(loop_module, "build_default_registry", record_registry)
    monkeypatch.setattr(loop_module, "run_coordinator", fake_run_coordinator)

    import dramatiq

    monkeypatch.setattr(dramatiq, "set_broker", noop_set_broker)

    await loop_module._async_main()  # type: ignore[reportPrivateUsage]

    assert events.index("role") < events.index("broker")
    assert events.index("role") < events.index("registry")
    assert events[-1] == "run"
