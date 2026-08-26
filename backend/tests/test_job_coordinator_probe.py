"""Focused validation for the coordinator liveness probe (plan P3).

The probe is the Compose healthcheck: it must report healthy when the
coordinator process is running and unhealthy when it is not, and it must
never report healthy because of its own command line. Review found the
previous inline probe searched every ``/proc/*/cmdline`` for
``app.job_coordinator``, which the probe's own ``python -c`` script always
contains — healthy with no coordinator at all. These tests prove the fixed
probe against simulated ``/proc`` trees.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.job_coordinator.probe import coordinator_alive, coordinator_matches

_COORDINATOR_ARGV = b"python\x00-m\x00app.job_coordinator\x00"
_PROBE_ARGV = b"python\x00-c\x00from app.job_coordinator.probe import main; main()\x00"
_WORKER_ARGV = b"dramatiq\x00app.workers\x00"


def _simulated_pid(*used: int) -> int:
    """Return a fake PID distinct from ``used`` and from this test process.

    The probe skips its own PID (``exclude_pid``), so a simulated coordinator
    PID must never equal the exclusion PID. In this environment pytest itself
    can be any PID, and the previous fixed ``9`` collided with the test
    process — making the simulated coordinator invisible and the test red.
    """
    blocked = {*used, os.getpid()}
    pid = 2
    while pid in blocked:
        pid += 1
    return pid


def _write_proc(root: Path, pid: int, argv: bytes) -> Path:
    """Create one simulated ``/proc/<pid>/cmdline`` entry."""
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cmdline").write_bytes(argv)
    return directory


def test_matcher_recognises_only_the_coordinator_invocation() -> None:
    """The argv matcher requires ``-m`` plus the module name as separate args.

    A ``-c`` script that merely mentions the module (the probe's own shape)
    never contains ``-m``, and a worker command line never names the
    coordinator module at all.
    """
    assert coordinator_matches(_COORDINATOR_ARGV.split(b"\0"))
    assert not coordinator_matches(_PROBE_ARGV.split(b"\0"))
    assert not coordinator_matches(_WORKER_ARGV.split(b"\0"))
    assert not coordinator_matches(b"".split(b"\0"))


def test_probe_fails_without_the_coordinator(tmp_path: Path) -> None:
    """No coordinator process -> unhealthy, whatever else is running."""
    root = tmp_path / "proc"
    _write_proc(root, _simulated_pid(), _WORKER_ARGV)
    _write_proc(root, _simulated_pid(), _PROBE_ARGV)
    assert coordinator_alive(root, exclude_pid=os.getpid()) is False


def test_probe_succeeds_with_the_coordinator(tmp_path: Path) -> None:
    """The coordinator process -> healthy."""
    root = tmp_path / "proc"
    _write_proc(root, _simulated_pid(), _WORKER_ARGV)
    _write_proc(root, _simulated_pid(), _COORDINATOR_ARGV)
    assert coordinator_alive(root, exclude_pid=os.getpid()) is True


def test_probe_never_reports_healthy_from_its_own_command_line(tmp_path: Path) -> None:
    """The self-matching defect: the probe's own argv mentions the module."""
    root = tmp_path / "proc"
    probe_pid = _simulated_pid()
    _write_proc(root, probe_pid, _PROBE_ARGV)
    # No coordinator anywhere, and the probe PID (its own) is excluded.
    assert coordinator_alive(root, exclude_pid=probe_pid) is False
    # Even without the exclusion the argv matcher alone rejects the -c shape.
    assert coordinator_matches(_PROBE_ARGV.split(b"\0")) is False


def test_probe_tolerates_exited_and_unreadable_processes(tmp_path: Path) -> None:
    """A vanished or unreadable /proc entry cannot crash the healthcheck."""
    root = tmp_path / "proc"
    coordinator_pid = _simulated_pid()
    directory = root / str(coordinator_pid)
    directory.mkdir(parents=True)
    (directory / "cmdline").write_bytes(_COORDINATOR_ARGV)
    # An entry that is unreadable mid-scan (no cmdline file).
    unreadable_pid = _simulated_pid(coordinator_pid)
    (root / str(unreadable_pid)).mkdir()
    # Simulate a process that exited between listing /proc and reading it.
    exited_pid = _simulated_pid(coordinator_pid, unreadable_pid)
    (root / str(exited_pid)).mkdir(parents=True)
    assert coordinator_alive(root, exclude_pid=os.getpid()) is True
    (directory / "cmdline").unlink()
    assert coordinator_alive(root, exclude_pid=os.getpid()) is False
