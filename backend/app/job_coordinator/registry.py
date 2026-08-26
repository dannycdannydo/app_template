"""Allow-listed outbox dispatch registry (durable delivery plan P3).

The coordinator turns ``outbox_events`` rows into Dramatiq messages. Actor
selection and queue routing come exclusively from this internal, allow-listed
registry, keyed by the durable ``job_type`` (for ``job.dispatch_requested``
events) or by the maintenance ``event_type`` — never from an importable
function name or arbitrary payload stored in the database (plan decisions:
"actor selection and queue routing come from an internal, allow-listed
registry keyed by the durable job_type").

The registry is a static, checked-in map: the keys are the constants the task
modules declare, the values are the actor objects those modules registered, so
a producer and the broker path can never drift. :func:`validate` is the
startup completeness check the coordinator runs before its first poll, and the
structural test suite re-runs it plus a source scan proving every durable
producer registers with the map.

Importing this module must never import the task modules: those modules
register their actors with the process-wide Dramatiq broker at import time,
and the coordinator installs the configured broker *before* it builds the
registry (``loop._async_main``). :func:`build_default_registry` is the only
place that imports the durable actors, so it must only be called after the
broker is installed — the entrypoint does both in order, and a regression
test asserts every production actor is then bound to the configured broker.

Publishing stays reference-only: a durable job actor receives only its
``job_id``, and a maintenance actor receives no arguments at all. The
registry functions are the only production component that may call an actor's
``send()`` (blueprint §19: Redis executes, PostgreSQL provides durability).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from app.modules.outbox.contracts import (
    EVENT_TYPE_AI_RETENTION,
    EVENT_TYPE_TRANSFER_RECONCILE,
)


class DispatchTarget(Protocol):
    """Anything the registry may publish through.

    Production entries are :class:`dramatiq.Actor` instances; the protocol is
    deliberately minimal (a ``send`` method) so tests can substitute a fake
    target that fails like an unavailable broker without weakening the
    production allow-list.
    """

    def send(self, *args: Any, **kwargs: Any) -> Any: ...


# The closed catalogue of durable job types the coordinator may publish.
# These literal strings are the values the task modules declare; a task module
# that changes its ``JOB_TYPE_*`` constant fails ``DispatchRegistry.validate``
# (the actor map and the catalogue no longer match), and the structural suite
# asserts the catalogue still equals the constants the task modules own.
DURABLE_JOB_TYPES: tuple[str, ...] = (
    "file.processing",
    "notification.email",
    "ai.execute",
)

# The closed catalogue of scheduled maintenance event types the coordinator
# may publish (their UTC-bucket scheduling arrives in plan P4; the registry
# already owns the event-type -> actor mapping). The constants come from the
# outbox contracts module, which imports no actors.
MAINTENANCE_EVENT_TYPES: tuple[str, ...] = (
    EVENT_TYPE_AI_RETENTION,
    EVENT_TYPE_TRANSFER_RECONCILE,
)


class RegistryError(Exception):
    """An event/job has no allowed dispatch target.

    Raised by the registry when an outbox row names a job type or maintenance
    event that is not allow-listed. The coordinator treats this as a permanent
    ``dead`` outbox row (plan P3), never as a retryable error.
    """

    def __init__(self, *, kind: str, name: str) -> None:
        super().__init__(f"no registered actor for {kind} {name!r}")
        self.kind = kind
        self.name = name


class RegistryCompletenessError(Exception):
    """The allow-list is incomplete or contains an unknown entry."""


@dataclass(frozen=True)
class DispatchRegistry:
    """The allow-listed event type / job type -> actor map.

    Production uses :func:`build_default_registry`, whose entries are the real
    task modules' actors. Tests construct their own instance bound to actors
    re-declared on their own broker (the same seam the broker journey tests
    already use), so the coordinator code is exercised without ever touching
    the process-global actor registrations.
    """

    # Durable job type -> the actor that receives only ``job_id``.
    job_actors: dict[str, DispatchTarget] = field(default_factory=dict[str, DispatchTarget])
    # Maintenance event type -> the argument-free maintenance actor.
    maintenance_actors: dict[str, DispatchTarget] = field(default_factory=dict[str, DispatchTarget])

    def job_types(self) -> set[str]:
        return set(self.job_actors)

    def maintenance_event_types(self) -> set[str]:
        return set(self.maintenance_actors)

    def actor_for_job_type(self, job_type: str) -> DispatchTarget:
        """Return the allow-listed actor for a durable ``job_type``.

        Raises :class:`RegistryError` for a job type outside the allow-list
        (the coordinator marks the event ``dead``; nothing is ever resolved
        from persisted strings).
        """
        try:
            return self.job_actors[job_type]
        except KeyError:
            raise RegistryError(kind="job_type", name=job_type) from None

    def actor_for_maintenance_event(self, event_type: str) -> DispatchTarget:
        """Return the allow-listed maintenance actor for an ``event_type``."""
        try:
            return self.maintenance_actors[event_type]
        except KeyError:
            raise RegistryError(kind="maintenance event", name=event_type) from None

    def publish_job_dispatch(self, job_type: str, job_id: str) -> None:
        """Publish the reference-only dispatch message for a durable job.

        The broker message carries exactly one field: the job id. No file
        bytes, document text, prompts, signed URLs, object keys, recipients,
        credentials or provider responses ever cross this boundary.
        """
        actor = self.actor_for_job_type(job_type)
        actor.send(job_id=job_id)

    def publish_maintenance(self, event_type: str) -> None:
        """Publish the argument-free message for a scheduled maintenance event."""
        actor = self.actor_for_maintenance_event(event_type)
        actor.send()

    def validate(self) -> None:
        """Fail when the allow-list is incomplete or contains unknown entries.

        Runs at coordinator startup and in tests: the map must cover exactly
        the durable job types and maintenance event types this plan owns, and
        every durable actor must be a retry-policy actor (so a maintenance
        actor is never accidentally registered as a durable job producer).

        ``jobs.service`` is imported here, not at module level: importing it
        as the *first* application import triggers the pre-existing
        ``audit.models`` circular import (``audit.service`` ->
        ``audit.models`` -> ``db.base`` -> ``ai.persistence.models`` ->
        ``app.ai`` -> ``transfer_orchestrator`` -> ``audit.service``). By the
        time validation runs the durable actors have been imported, which
        resolves that chain.
        """
        from app.modules.jobs import service as jobs_service

        missing_jobs = set(DURABLE_JOB_TYPES) - set(self.job_actors)
        unknown_jobs = set(self.job_actors) - set(DURABLE_JOB_TYPES)
        missing_maintenance = set(MAINTENANCE_EVENT_TYPES) - set(self.maintenance_actors)
        unknown_maintenance = set(self.maintenance_actors) - set(MAINTENANCE_EVENT_TYPES)
        problems: list[str] = []
        if missing_jobs:
            problems.append(f"missing durable job actors: {sorted(missing_jobs)}")
        if unknown_jobs:
            problems.append(f"unregistered durable job types: {sorted(unknown_jobs)}")
        if missing_maintenance:
            problems.append(f"missing maintenance actors: {sorted(missing_maintenance)}")
        if unknown_maintenance:
            problems.append(f"unregistered maintenance event types: {sorted(unknown_maintenance)}")
        for job_type, actor in self.job_actors.items():
            if (
                getattr(actor, "options", {}).get("on_retry_exhausted")
                != jobs_service.MARK_FAILED_AFTER_RETRIES_ACTOR
            ):
                problems.append(
                    f"job type {job_type!r} is not a durable retry-policy actor "
                    "(its options do not name the retries-exhausted finalizer)"
                )
        if problems:
            raise RegistryCompletenessError("; ".join(problems))


def build_default_registry() -> DispatchRegistry:
    """Build the production allow-list from the real task modules.

    Importing the task modules is what registers their actors with the
    process-wide Dramatiq broker, so this must only be called after the
    configured broker has been installed: the coordinator entrypoint does both
    in order (``loop._async_main``), and conftest installs the network-free
    stub broker before any test imports a task module. A fourth durable actor
    requires an explicit entry here and in :data:`DURABLE_JOB_TYPES`; the
    completeness validation and the structural suite fail until both name it.
    """
    from app.ai.execution import JOB_TYPE_AI_EXECUTE, execute_ai_task_actor
    from app.ai.persistence.tasks import (
        enforce_ai_retention_actor,
        reconcile_provider_file_references_actor,
    )
    from app.modules.files.tasks import JOB_TYPE_FILE_PROCESSING, process_file_actor
    from app.modules.notifications.tasks import (
        JOB_TYPE_NOTIFICATION_EMAIL,
        send_notification_email_actor,
    )

    return DispatchRegistry(
        job_actors={
            JOB_TYPE_FILE_PROCESSING: cast(DispatchTarget, process_file_actor),
            JOB_TYPE_NOTIFICATION_EMAIL: cast(DispatchTarget, send_notification_email_actor),
            JOB_TYPE_AI_EXECUTE: cast(DispatchTarget, execute_ai_task_actor),
        },
        maintenance_actors={
            EVENT_TYPE_AI_RETENTION: cast(DispatchTarget, enforce_ai_retention_actor),
            EVENT_TYPE_TRANSFER_RECONCILE: cast(
                DispatchTarget, reconcile_provider_file_references_actor
            ),
        },
    )
