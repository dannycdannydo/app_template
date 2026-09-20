"""Deployment check: prove the configured database roles are safely restricted.

Plan P4 / ``docs/rls-rollout.md`` §5. The RLS backstop is only real if the
normal application path authenticates as a non-owner, non-superuser role without
``BYPASSRLS``. This operator command connects with ``DATABASE_RUNTIME_URL`` (and
``DATABASE_COORDINATOR_URL`` when configured) and verifies, from the server
catalogue, that the authenticated role owns no table in ``public``, carries none
of ``SUPERUSER``/``BYPASSRLS``/``CREATEDB``/``CREATEROLE`` and inherits no
privileged role. It also proves ``app_operator`` is the only application role
carrying ``BYPASSRLS`` and that the runtime role cannot ``SET ROLE`` it.

The same check runs automatically at production startup (``create_app``'s
lifespan); this command exposes it for pre-deployment verification and for
environments where startup should stay independent of it. It exits non-zero on
any violation and never prints credential material.

Usage::

    uv run python -m scripts.verify_db_roles
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import get_settings
from app.db.role_checks import (
    DatabaseRoleCheckError,
    verify_coordinator_database_role,
    verify_runtime_database_role,
)
from app.db.session import build_coordinator_session_factory, build_session_factory


async def run() -> int:
    settings = get_settings()
    runtime_engine, _ = build_session_factory(settings)
    coordinator_engine = None
    try:
        runtime = await verify_runtime_database_role(runtime_engine)
        print(
            f"runtime role {runtime.role!r}: restricted "
            f"(owns {runtime.owned_table_count} table(s), BYPASSRLS=false)"
        )
        if settings.database_coordinator_url:
            coordinator_engine, _ = build_coordinator_session_factory(settings)
            coordinator = await verify_coordinator_database_role(coordinator_engine)
            print(
                f"coordinator role {coordinator.role!r}: restricted "
                f"(owns {coordinator.owned_table_count} table(s), BYPASSRLS=false)"
            )
    except DatabaseRoleCheckError as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        await runtime_engine.dispose()
        if coordinator_engine is not None:
            await coordinator_engine.dispose()
    print("database role check passed")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the configured database credentials are restricted roles"
    )
    parser.parse_args()
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
