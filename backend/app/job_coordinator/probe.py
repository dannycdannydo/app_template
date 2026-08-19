"""Coordinator liveness probe (durable delivery plan P3).

Both Compose profiles verify the coordinator process is alive by scanning
``/proc`` command lines; the slim base image has neither ``pgrep`` nor
``ps``. This module is deliberately dependency-free (standard library only):
the healthcheck container runs it via ``python -c``, and it must not import
the coordinator loop, the registry or any task module.

The probe must never match itself. The healthcheck runs as ``python -c
"from app.job_coordinator.probe import main; main()"``, whose own command
line contains the literal string ``app.job_coordinator`` — the self-matching
defect found in review. The probe therefore (a) skips its own PID and (b)
requires the coordinator's invocation shape: the ``-m`` flag and the module
name as *separate* argv entries, which a ``-c`` script never has.
"""

from __future__ import annotations

import os
from pathlib import Path


def coordinator_matches(args: list[bytes]) -> bool:
    """True when one process argv is the coordinator invocation.

    The coordinator runs as ``python -m app.job_coordinator``: the ``-m``
    flag and the module name appear as separate argv entries. A ``-c``
    script's argv never contains ``-m``, so a probe (or any other process)
    that merely mentions the module name in code cannot self-match.
    """
    return b"-m" in args and b"app.job_coordinator" in args


def coordinator_alive(
    proc_root: str | os.PathLike[str] = "/proc", *, exclude_pid: int | None = None
) -> bool:
    """True when a ``python -m app.job_coordinator`` process is running.

    ``proc_root`` is injected by tests as a simulated ``/proc``; production
    always scans the real filesystem. The probe's own PID is always skipped
    so a healthcheck can never report healthy because of itself.
    """
    skip = os.getpid() if exclude_pid is None else exclude_pid
    root = Path(proc_root)
    try:
        entries = os.listdir(root)
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == skip:
            continue
        try:
            raw = (root / entry / "cmdline").read_bytes()
        except OSError:
            # The process may have exited (or be unreadable) mid-scan; it is
            # simply not a live coordinator as far as this probe is concerned.
            continue
        if coordinator_matches(raw.split(b"\0")):
            return True
    return False


def main() -> None:
    """Healthcheck entry point: exit 0 when the coordinator is alive."""
    raise SystemExit(0 if coordinator_alive() else 1)
