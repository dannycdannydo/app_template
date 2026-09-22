"""Structural boundary for the isolated ``app_operator`` credential (plan P4).

Plan P4's aggregate completion evidence has two claims this module pins down:

- **No normal API or worker path uses owner, superuser or ``BYPASSRLS``
  credentials.** The runtime and coordinator credentials are resolved by
  ``app.db.session`` and verified at startup by ``app.db.role_checks``. The
  ``app_operator`` credential is the *only* ``BYPASSRLS`` application role
  (ADR-0022 decision 4), so it must be reachable only through the dedicated
  operator engine, which no ordinary module imports.
- **Platform and recovery procedures work without creating a hidden tenant
  bypass.** The bootstrap grant, ``user.deleted`` deactivation and the
  recovery/teardown CLIs run on the restricted runtime credential and bind the
  narrow transaction-local service context; they must never resolve the
  operational bypass credential.

These are source-level guards. The behavioural proof is the group-6/7
real-PostgreSQL suites and ``test_runtime_role_startup_gates.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"
SCRIPTS_ROOT = BACKEND_ROOT / "scripts"


def _python_sources(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _files_containing(root: Path, needle: str) -> set[str]:
    """Return the backend-relative paths of ``root``'s sources containing ``needle``."""
    return {
        str(path.relative_to(BACKEND_ROOT))
        for path in _python_sources(root)
        if needle in path.read_text(encoding="utf-8")
    }


@pytest.mark.parametrize(
    ("needle", "allowed"),
    [
        # The operator engine/resolver is defined only in app.db.session.
        # config.py only names it in the credential's field description.
        ("build_operator_session_factory", {"app/db/session.py"}),
        ("resolve_operator_database_url", {"app/db/session.py", "app/core/config.py"}),
        # The credential setting is defined and consumed only by config/session.
        ("database_operator_url", {"app/core/config.py", "app/db/session.py"}),
    ],
)
def test_operator_credential_is_reachable_only_from_session(needle: str, allowed: set[str]) -> None:
    offenders = _files_containing(APP_ROOT, needle) - allowed
    assert offenders == set(), (
        f"{needle!r} must be confined to {sorted(allowed)}; found in {sorted(offenders)}"
    )


@pytest.mark.parametrize(
    "needle",
    [
        "build_operator_session_factory",
        "resolve_operator_database_url",
        "database_operator_url",
        # The literal env var name is a separate needle so a direct
        # ``os.environ`` read of the credential is caught too.
        "DATABASE_OPERATOR_URL",
    ],
)
def test_recovery_and_platform_scripts_do_not_load_the_bypass_credential(needle: str) -> None:
    """No script under ``backend/scripts/`` may reference the operator credential.

    Scanning the whole directory (rather than a hand-listed set of scripts) means
    a new operator CLI cannot silently start loading the bypass credential.
    """
    offenders = _files_containing(SCRIPTS_ROOT, needle)
    assert offenders == set(), (
        f"operator tooling must not reference the bypass credential ({needle!r}); "
        f"found in {sorted(offenders)}"
    )


@pytest.mark.parametrize(
    ("module", "gate"),
    [
        ("app/main.py", "verify_production_database_roles"),
        ("app/workers.py", "enforce_production_runtime_role"),
        ("app/job_coordinator/loop.py", "verify_production_coordinator_role"),
    ],
)
def test_every_normal_runtime_process_enforces_the_role_gate(module: str, gate: str) -> None:
    """Presence check only: each entrypoint names its gate.

    The behavioural proof that the gate runs at the right point is in
    ``test_runtime_role_startup_gates.py`` (worker and coordinator) and the
    lifespan test in ``test_db_role_checks.py`` (API).
    """
    source = (BACKEND_ROOT / module).read_text(encoding="utf-8")
    assert gate in source, f"{module} must invoke its production role gate {gate!r}"
