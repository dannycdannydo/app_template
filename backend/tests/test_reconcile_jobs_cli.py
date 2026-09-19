"""Operator reconciliation command guard tests (durable delivery plan P5)."""

from __future__ import annotations

import sys
import uuid
from types import ModuleType
from typing import Any, cast

import pytest

from scripts import reconcile_jobs


async def test_apply_requires_explicit_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mutation guard exits before configuration or database access."""
    monkeypatch.delenv("CONFIRM_RECONCILE", raising=False)

    assert await reconcile_jobs.run(apply=True) == 2


async def test_default_inspection_is_read_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inspection prints opaque candidate ids and never calls the apply service."""
    candidate_id = uuid.uuid4()

    class _Settings:
        job_reconcile_threshold_seconds = 900
        job_reconcile_cooldown_seconds = 900
        coordinator_publication_batch_size = 50

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    config = cast(Any, ModuleType("app.core.config"))
    config.get_settings = lambda: _Settings()
    session = cast(Any, ModuleType("app.db.session"))
    session.coordinator_session_factory = lambda: _Session()

    def _forbidden_runtime_factory() -> None:
        raise AssertionError("the CLI must use the coordinator credential")

    session.async_session_factory = _forbidden_runtime_factory
    reconciliation = cast(Any, ModuleType("app.job_coordinator.reconciliation"))

    async def _candidates(*args: object, **kwargs: object) -> list[uuid.UUID]:
        return [candidate_id]

    async def _apply(*args: object, **kwargs: object) -> list[uuid.UUID]:
        raise AssertionError("read-only inspection must not reconcile")

    reconciliation.reconciliation_candidates = _candidates
    reconciliation.reconcile_queued_jobs = _apply
    monkeypatch.setitem(sys.modules, "app.core.config", config)
    monkeypatch.setitem(sys.modules, "app.db.session", session)
    monkeypatch.setitem(sys.modules, "app.job_coordinator.reconciliation", reconciliation)

    assert await reconcile_jobs.run(apply=False) == 0
    assert capsys.readouterr().out.splitlines() == [
        "reconciliation candidates: 1",
        str(candidate_id),
    ]
