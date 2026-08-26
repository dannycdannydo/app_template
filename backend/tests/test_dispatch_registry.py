"""Structural and unit tests for the allow-listed dispatch registry (plan P3).

Two guarantees are proven here, both structural:

- **No producer publishes directly**: the durable job producers (files,
  notifications and AI services) and the job service never call an actor's
  ``send()``. Only the coordinator's registry publishes, and the registry
  functions are the only ``send()`` call sites in the production code.
- **Registry coverage**: every durable job type the plan owns and every
  durable actor declared with the shared retry policy is registered, so a
  producer or actor can never silently drift out of the allow-list. The
  startup completeness validation (``DispatchRegistry.validate``) is also
  proven, including its failure modes, and the entrypoint order is proven in
  a subprocess: importing the package registers nothing, and building the
  registry after the broker is installed binds every actor to it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import dramatiq
import pytest
from dramatiq.brokers.stub import StubBroker

from app.ai.execution import JOB_TYPE_AI_EXECUTE
from app.job_coordinator.registry import (
    DURABLE_JOB_TYPES,
    MAINTENANCE_EVENT_TYPES,
    DispatchRegistry,
    RegistryCompletenessError,
    RegistryError,
    build_default_registry,
)
from app.modules.files.tasks import JOB_TYPE_FILE_PROCESSING
from app.modules.jobs import service as jobs_service
from app.modules.notifications.tasks import JOB_TYPE_NOTIFICATION_EMAIL
from app.modules.outbox.contracts import (
    EVENT_TYPE_AI_RETENTION,
    EVENT_TYPE_TRANSFER_RECONCILE,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"

# The producer modules that schedule durable jobs. The coordinator registry is
# the only sanctioned publisher; these modules must never contain an actor
# ``send`` call.
PRODUCER_MODULES = (
    "modules/files/service.py",
    "modules/notifications/service.py",
    "ai/execution.py",
    "modules/jobs/service.py",
)


def _lines_with_send(path: Path) -> list[tuple[int, str]]:
    """Return (line_number, stripped_line) for every ``.send(`` occurrence."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    hits: list[tuple[int, str]] = []
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if ".send(" in stripped:
            hits.append((index, stripped))
    return hits


def test_no_producer_calls_an_actor_send() -> None:
    """Only the coordinator registry publishes broker messages (plan P3, AC2).

    The durable producer services must schedule through ``schedule_job`` and
    never touch an actor; the job service itself must no longer offer the
    old record-then-enqueue path.
    """
    violations: list[str] = []
    for relative in PRODUCER_MODULES:
        path = APP_ROOT / relative
        for line_number, line in _lines_with_send(path):
            violations.append(f"{relative}:{line_number}: {line}")
    assert violations == [], (
        "a durable producer calls an actor's send(); the coordinator registry "
        "is the only sanctioned publisher:\n" + "\n".join(violations)
    )


def test_job_service_no_longer_offers_record_then_enqueue() -> None:
    """The transaction-owned scheduling boundary replaced create_and_enqueue."""
    source = (APP_ROOT / "modules/jobs/service.py").read_text(encoding="utf-8")
    assert "def create_and_enqueue" not in source
    assert "def schedule_job" in source


def test_default_registry_is_complete() -> None:
    """Startup completeness: the allow-list covers the plan's five targets."""
    registry = build_default_registry()
    registry.validate()
    assert set(DURABLE_JOB_TYPES) == {
        JOB_TYPE_FILE_PROCESSING,
        JOB_TYPE_NOTIFICATION_EMAIL,
        JOB_TYPE_AI_EXECUTE,
    }
    assert set(MAINTENANCE_EVENT_TYPES) == {
        EVENT_TYPE_AI_RETENTION,
        EVENT_TYPE_TRANSFER_RECONCILE,
    }
    assert registry.job_types() == set(DURABLE_JOB_TYPES)
    assert registry.maintenance_event_types() == set(MAINTENANCE_EVENT_TYPES)


def test_known_task_module_job_types_are_registered() -> None:
    """The task-module constants the current producers use are registered.

    This proves the three durable job types the plan owns are in the map; it
    does not claim to discover unknown future producers (the registry is a
    closed, allow-listed catalogue, so a new durable producer additionally
    needs an explicit entry here and in ``DURABLE_JOB_TYPES``).
    """
    registry = build_default_registry()
    for constant in (JOB_TYPE_FILE_PROCESSING, JOB_TYPE_NOTIFICATION_EMAIL, JOB_TYPE_AI_EXECUTE):
        assert constant in registry.job_types(), (
            f"durable job type {constant!r} is not registered with "
            "the coordinator dispatch registry"
        )


def test_registered_durable_actors_use_the_shared_retry_policy() -> None:
    """Every registered durable actor is a retry-policy actor (plan P3).

    A maintenance actor accidentally registered as a durable job producer
    (or a durable actor that lost its retry policy) fails completeness.
    """
    for job_type, actor in build_default_registry().job_actors.items():
        assert (
            getattr(actor, "options", {}).get("on_retry_exhausted")
            == jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR
        ), f"registered job type {job_type!r} is not a durable retry-policy actor"


def test_actor_lookup_rejects_unknown_job_types() -> None:
    """Unknown job types are permanent registry errors, never resolved strings."""
    registry = build_default_registry()
    with pytest.raises(RegistryError):
        registry.actor_for_job_type("not.a.real.job.type")
    with pytest.raises(RegistryError):
        registry.actor_for_maintenance_event("not.a.real.event")


def test_registry_completeness_validation_failure_modes() -> None:
    """Missing, unknown and non-durable entries are rejected at startup."""
    production = build_default_registry()

    empty = DispatchRegistry()
    with pytest.raises(RegistryCompletenessError):
        empty.validate()

    missing = DispatchRegistry(
        job_actors={JOB_TYPE_FILE_PROCESSING: production.job_actors[JOB_TYPE_FILE_PROCESSING]},
        maintenance_actors={},
    )
    with pytest.raises(RegistryCompletenessError):
        missing.validate()

    unknown = DispatchRegistry(
        job_actors={
            **production.job_actors,
            "surprise.job": production.job_actors[JOB_TYPE_FILE_PROCESSING],
        },
        maintenance_actors=production.maintenance_actors,
    )
    with pytest.raises(RegistryCompletenessError):
        unknown.validate()

    # A non-durable (no retry policy) actor registered as a durable producer.
    @dramatiq.actor()
    def _plain() -> None:
        return None

    non_durable = DispatchRegistry(
        job_actors={JOB_TYPE_FILE_PROCESSING: _plain},
        maintenance_actors=production.maintenance_actors,
    )
    with pytest.raises(RegistryCompletenessError):
        non_durable.validate()


def test_publish_job_dispatch_builds_reference_only_message() -> None:
    """The registry publishes exactly ``job_id`` — never content (plan P3).

    The closed payload contract (``JobDispatchPayload``) and the registry
    function together mean a durable job actor receives only its job id, so
    file bytes, storage references, recipients or prompts can never cross the
    broker boundary.
    """
    from dramatiq.worker import Worker

    previous_broker = dramatiq.get_broker()
    broker = StubBroker()
    dramatiq.set_broker(broker)
    worker = Worker(broker, worker_timeout=100, worker_threads=1)
    worker.start()
    try:
        received: list[tuple[object, ...]] = []
        captured: list[dict[str, object]] = []

        def _record(*args: object, **kwargs: object) -> None:
            received.append(args)
            captured.append(kwargs)

        actor = dramatiq.actor(queue_name="registry-test")(_record)
        registry = DispatchRegistry(
            job_actors={JOB_TYPE_FILE_PROCESSING: actor}, maintenance_actors={}
        )
        job_id = uuid.uuid4()
        registry.publish_job_dispatch(JOB_TYPE_FILE_PROCESSING, str(job_id))
        broker.join("registry-test", timeout=10000)
        assert len(captured) == 1
        assert captured[0] == {"job_id": str(job_id)}
        assert received[0] == ()
    finally:
        worker.stop()
        broker.flush_all()
        dramatiq.set_broker(previous_broker)


def test_publish_maintenance_builds_argument_free_message() -> None:
    """A scheduled maintenance event carries no payload at all (plan P3)."""
    from dramatiq.worker import Worker

    previous_broker = dramatiq.get_broker()
    broker = StubBroker()
    dramatiq.set_broker(broker)
    worker = Worker(broker, worker_timeout=100, worker_threads=1)
    worker.start()
    try:
        captured: list[tuple[object, ...]] = []

        def _record(*args: object, **kwargs: object) -> None:
            captured.append((args, kwargs))

        actor = dramatiq.actor(queue_name="registry-maintenance-test")(_record)
        registry = DispatchRegistry(
            job_actors={}, maintenance_actors={EVENT_TYPE_AI_RETENTION: actor}
        )
        registry.publish_maintenance(EVENT_TYPE_AI_RETENTION)
        broker.join("registry-maintenance-test", timeout=10000)
        assert captured == [((), {})]
    finally:
        worker.stop()
        broker.flush_all()
        dramatiq.set_broker(previous_broker)


def test_entrypoint_binds_production_actors_to_the_configured_broker() -> None:
    """Regression: ``python -m app.job_coordinator`` never publishes through a default broker.

    Review found that importing the production registry before
    ``dramatiq.set_broker(build_broker())`` leaves every actor bound to the
    pre-configuration default broker (``Actor.send`` enqueues through
    ``actor.broker``), so the entrypoint could publish outside the configured
    transport. The entrypoint's exact ordering is run in a subprocess —
    pytest has already imported the task modules against the stub broker, so
    the defect cannot reproduce in-process — and both halves are asserted:

    - importing ``app.job_coordinator`` registers no actor at all (the
      package ``__init__`` is side-effect free), and
    - building the registry *after* the broker is installed binds every
      production actor to that broker.
    """
    code = (
        "import dramatiq\n"
        "from dramatiq.brokers.stub import StubBroker\n"
        "broker = StubBroker()\n"
        "dramatiq.set_broker(broker)\n"
        "import app.job_coordinator  # noqa: F401  side-effect-free package import\n"
        "assert not broker.actors, f'package import registered actors: {sorted(broker.actors)}'\n"
        "from app.broker import build_broker\n"
        "from app.job_coordinator.registry import build_default_registry\n"
        "configured = build_broker()\n"
        "dramatiq.set_broker(configured)\n"
        "registry = build_default_registry()\n"
        "actors = [*registry.job_actors.values(), *registry.maintenance_actors.values()]\n"
        "assert actors, 'the production registry must declare actors'\n"
        "for actor in actors:\n"
        "    assert actor.broker is configured, (\n"
        "        f'{actor.actor_name} is bound to {actor.broker!r}, '\n"
        "        'not the configured broker'\n"
        "    )\n"
        "print('entrypoint broker binding OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
