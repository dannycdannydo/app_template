"""Durable outbox coordinator (durable delivery plan P3).

The coordinator is the standalone process that turns durable PostgreSQL
``outbox_events`` rows into Dramatiq messages. Redis executes; PostgreSQL
provides durability (blueprint §19). See ``app.job_coordinator.loop`` for the
run loop and ``app.job_coordinator.registry`` for the allow-listed
event/job-type -> actor map. Native entry point:
``uv run python -m app.job_coordinator``.

Importing this package must never import the task modules: those modules
register their actors with the process-wide Dramatiq broker at import time,
and ``python -m app.job_coordinator`` imports this package before
``__main__`` runs. The entrypoint installs the configured broker *before* it
builds the registry (``loop._async_main``), so the public names below are
resolved lazily on first access instead of eagerly at package import.
"""

from __future__ import annotations

# The names below are lazy PEP 562 ``__getattr__`` exports; pyright cannot see
# them as module members, which is exactly the laziness the entrypoint needs.
__all__ = [
    "DURABLE_JOB_TYPES",  # pyright: ignore[reportUnsupportedDunderAll]
    "MAINTENANCE_EVENT_TYPES",  # pyright: ignore[reportUnsupportedDunderAll]
    "DispatchRegistry",  # pyright: ignore[reportUnsupportedDunderAll]
    "RegistryCompletenessError",  # pyright: ignore[reportUnsupportedDunderAll]
    "RegistryError",  # pyright: ignore[reportUnsupportedDunderAll]
    "build_default_registry",  # pyright: ignore[reportUnsupportedDunderAll]
    "main",  # pyright: ignore[reportUnsupportedDunderAll]
]


def __getattr__(name: str) -> object:
    """Resolve a coordinator name lazily (never at package import).

    ``registry`` imports no task modules itself, so the registry names are
    safe to resolve here; ``loop`` is likewise safe because it imports the
    registry only after the broker is installed. Resolving eagerly would
    re-introduce the entrypoint defect the regression test guards against.
    The resolved name is cached in the module namespace for later lookups.
    """
    if name == "main":
        from app.job_coordinator import loop

        value: object = loop.main
    elif name in {
        "DURABLE_JOB_TYPES",
        "MAINTENANCE_EVENT_TYPES",
        "DispatchRegistry",
        "RegistryCompletenessError",
        "RegistryError",
        "build_default_registry",
    }:
        from app.job_coordinator import registry

        value = getattr(registry, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
