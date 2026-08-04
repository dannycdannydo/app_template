"""Toolchain sanity checks for the v0.1 backend.

These verify the pytest setup works (collection, project-root path, and
pytest-asyncio auto mode) before any application code exists.
"""

import asyncio
from pathlib import Path


def test_pytest_discovers_backend_project(project_root: Path) -> None:
    assert (project_root / "pyproject.toml").is_file()


async def test_asyncio_auto_mode_runs_event_loop() -> None:
    value = await asyncio.sleep(0, result=42)
    assert value == 42
