"""Release bookkeeping consistency (Plan P10, blueprint §41).

The starter is a versioned product: clone consumers must be able to infer the
implemented release from the package manifests, and the release must agree with
the scope document it was cut from. These are fast, pure checks that fail the
default suite if a release bookkeeping change drifts.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_PYPROJECT = REPOSITORY_ROOT / "backend" / "pyproject.toml"
FRONTEND_PACKAGE = REPOSITORY_ROOT / "frontend" / "package.json"
UPGRADES_DIR = REPOSITORY_ROOT / "docs" / "upgrades"

RELEASE_RE = re.compile(r"^Release:\s+v(\S+)", re.MULTILINE)
STATE_RE = re.compile(r"^State:\s+(\S+)", re.MULTILINE)


def _scope_path_for_version(version: str) -> Path:
    """Resolve the release contract named by the recorded version.

    The recorded version is authoritative. An unreleased higher scope (for
    example a planned ``TEMPLATE_V0_9_SCOPE.md``) must never be mistaken for
    the release under test, so the path is derived from the version rather
    than picking the highest-numbered file. Naming follows
    ``TEMPLATE_V<major>_<minor>_SCOPE.md``.
    """
    major, minor, _patch = version.split(".")
    return REPOSITORY_ROOT / f"TEMPLATE_V{major}_{int(minor)}_SCOPE.md"


def test_package_versions_agree_with_each_other() -> None:
    """Backend, frontend and the template-version table must all match."""
    with BACKEND_PYPROJECT.open("rb") as handle:
        backend = tomllib.load(handle)
    frontend = json.loads(FRONTEND_PACKAGE.read_text(encoding="utf-8"))

    backend_version = backend["project"]["version"]
    template_version = backend["tool"]["project-template"]["version"]
    frontend_version = frontend["version"]

    assert backend_version == frontend_version
    assert backend_version == template_version
    assert backend["tool"]["project-template"]["name"] == "internal-app-template"


def test_released_version_agrees_with_its_scope() -> None:
    """The scope named by the recorded version must match and be complete."""
    with BACKEND_PYPROJECT.open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    scope_path = _scope_path_for_version(version)
    assert scope_path.exists(), (
        f"no release contract {scope_path.name} for recorded version {version}"
    )
    scope = scope_path.read_text(encoding="utf-8")

    release = RELEASE_RE.search(scope)
    assert release is not None, f"{scope_path} has no 'Release:' status line"
    state = STATE_RE.search(scope)
    assert state is not None, f"{scope_path} has no 'State:' status line"

    assert release.group(1) == version
    assert state.group(1) == "complete"


def test_release_has_an_upgrade_guide() -> None:
    """A released version ships the upgrade guide §41 expects.

    Guides are named ``<from>-to-<major>.<minor>.md`` and the source side is
    free form (for example ``0.7`` or ``1.x`` for a major upgrade), so the
    check targets the version the guide upgrades to rather than computing a
    previous minor arithmetically.
    """
    with BACKEND_PYPROJECT.open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    major, minor, _patch = version.split(".")
    target = f"{major}.{minor}"
    guides = sorted(path.name for path in UPGRADES_DIR.glob(f"*-to-{target}.md") if path.is_file())
    assert guides, (
        f"missing upgrade guide for {target} under {UPGRADES_DIR.relative_to(REPOSITORY_ROOT)}"
    )
