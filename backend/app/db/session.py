"""Async engine and session factory (blueprint §5, §10).

The engine is created once from the typed settings. ``pool_pre_ping`` makes
connections resilient to database restarts, which the ``/ready`` endpoint
relies on.

RLS prototype (plan P2, ADR-0022 decision 2): the normal application path must
never connect as the schema owner. When ``DATABASE_RUNTIME_URL`` is configured,
the API and workers connect as the restricted, non-owner runtime role and are
subject to the enabled row-level-security policies. ``DATABASE_URL`` stays the
schema-owner/migration credential (Alembic does not import this module).

A production process **refuses to start** without a configured runtime
credential, so a deployed application can never silently fall back to the
schema owner. Outside production the owner URL is used only when no runtime URL
is set (the explicit local-development and test arrangement, where the local
owner is a superuser and no policy is enforced); this is not a silent fallback
and is never available in production.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings


def resolve_database_url(settings: Settings) -> str:
    """Return the URL the ordinary application engine must use.

    A configured ``database_runtime_url`` always wins, so the normal path runs
    as the restricted runtime role. In production an empty runtime URL is a
    hard error: the schema-owner/migration credential must not be used by the
    application (plan P2, ADR-0022 decision 2). Outside production the owner URL
    is the explicit no-run-time-role arrangement used by local development and
    the default test profile.
    """
    if settings.database_runtime_url:
        return settings.database_runtime_url
    if settings.app_env == "production":
        raise RuntimeError(
            "DATABASE_RUNTIME_URL is required in production: the normal application "
            "path must connect as a restricted, non-owner runtime role and must not "
            "use the schema-owner/migration credential (ADR-0022 decision 2)."
        )
    return settings.database_url


def resolve_coordinator_database_url(settings: Settings) -> str:
    """Return the URL the outbox-coordinator engine must use.

    The coordinator is the one production component whose legitimate scope is
    the global dispatch ledgers across every tenant, so it runs as the separate
    non-bypass ``app_coordinator`` role (ADR-0022 decision 3) rather than the
    tenant-scoped runtime role. A configured ``database_coordinator_url`` always
    wins. In production it is required once the jobs table group is enforced:
    falling back to the runtime credential would silently strand the coordinator
    behind the tenant policies. Outside production the runtime (or owner, when
    no runtime URL is set) credential is used for local development and the
    default test profile.
    """
    if settings.database_coordinator_url:
        return settings.database_coordinator_url
    if settings.app_env == "production":
        raise RuntimeError(
            "DATABASE_COORDINATOR_URL is required in production: the outbox "
            "coordinator reads and writes cross-tenant dispatch state and must "
            "connect as the restricted, non-owner coordinator role rather than the "
            "tenant-scoped runtime credential (ADR-0022 decision 3)."
        )
    return resolve_database_url(settings)


def build_session_factory(
    settings: Settings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Build the engine and session factory for ``settings``.

    Kept as a function so the credential selection (``resolve_database_url``)
    can be exercised directly and the module-level wiring below stays trivial.
    """
    engine = create_async_engine(
        resolve_database_url(settings),
        pool_pre_ping=True,
        echo=settings.debug,
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def build_coordinator_session_factory(
    settings: Settings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Build the coordinator engine and session factory for ``settings``.

    Mirrors :func:`build_session_factory` but binds the separate
    ``app_coordinator`` credential via :func:`resolve_coordinator_database_url`.
    """
    engine = create_async_engine(
        resolve_coordinator_database_url(settings),
        pool_pre_ping=True,
        echo=settings.debug,
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


engine, async_session_factory = build_session_factory(get_settings())
coordinator_engine, coordinator_session_factory = build_coordinator_session_factory(get_settings())
