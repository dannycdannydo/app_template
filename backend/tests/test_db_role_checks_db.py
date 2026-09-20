"""Real-PostgreSQL suite for the runtime database-role check (plan P4).

Plan P4 requires an automated startup/deployment check proving the runtime roles
do not own protected tables and lack ``BYPASSRLS`` (``docs/rls-rollout.md`` §5).
This suite migrates a reachable database to head and proves:

- the migrated ``app_runtime`` and ``app_coordinator`` credentials pass the check;
- the schema-owner credential is rejected (the "both URLs point at the same
  role" misconfiguration the resolver cannot detect);
- a runtime role that carries ``BYPASSRLS`` is rejected;
- a runtime role that can ``SET ROLE app_operator`` is rejected; and
- the production startup gate runs the check (and skips outside production).

The migration creates every role ``NOLOGIN``; the suite grants throwaway
credentials, and ``migrated_database`` reverts to base at teardown.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from tests.rls_helpers import (
    COORDINATOR_ROLE,
    OPERATOR_ROLE,
    RUNTIME_ROLE,
    coordinator_url,
    database_reachable,
    downgrade_to_base,
    provision_coordinator_login,
    provision_runtime_login,
    runtime_url,
    upgrade_to_head,
)

from app.core.config import get_settings
from app.db.role_checks import (
    DatabaseRoleCheckError,
    verify_coordinator_database_role,
    verify_production_database_roles,
    verify_runtime_database_role,
)


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
    database_url = os.environ["DATABASE_URL"]
    if not database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")
    upgrade_to_head()
    yield database_url
    downgrade_to_base()


@pytest.fixture
def runtime_database_url(migrated_database: str) -> str:
    """Provision the runtime login and return its database URL."""
    provision_runtime_login(migrated_database)
    return runtime_url(migrated_database)


@pytest.fixture
def coordinator_database_url(migrated_database: str) -> str:
    """Provision the coordinator login and return its database URL."""
    provision_coordinator_login(migrated_database)
    return coordinator_url(migrated_database)


def _engine(url: str) -> AsyncEngine:
    return create_async_engine(url, poolclass=NullPool)


async def test_restricted_runtime_and_coordinator_roles_pass(
    migrated_database: str,
    runtime_database_url: str,
    coordinator_database_url: str,
) -> None:
    """The migrated runtime and coordinator credentials satisfy the check."""
    runtime_engine = _engine(runtime_database_url)
    coordinator_engine = _engine(coordinator_database_url)
    try:
        runtime = await verify_runtime_database_role(runtime_engine)
        assert runtime.role == RUNTIME_ROLE
        assert runtime.rolsuper is False
        assert runtime.rolbypassrls is False
        assert runtime.owned_table_count == 0
        assert runtime.privileged_memberships == ()

        coordinator = await verify_coordinator_database_role(coordinator_engine)
        assert coordinator.role == COORDINATOR_ROLE
        assert coordinator.rolsuper is False
        assert coordinator.rolbypassrls is False
        assert coordinator.owned_table_count == 0
        assert coordinator.privileged_memberships == ()
    finally:
        await runtime_engine.dispose()
        await coordinator_engine.dispose()


async def test_schema_owner_credential_is_rejected(migrated_database: str) -> None:
    """Pointing the runtime credential at the owner fails the check."""
    owner_engine = _engine(migrated_database)
    try:
        with pytest.raises(DatabaseRoleCheckError) as excinfo:
            await verify_runtime_database_role(owner_engine)
        assert excinfo.value.label == "runtime"
        rendered = " | ".join(excinfo.value.problems)
        assert "owns" in rendered or "superuser" in rendered
    finally:
        await owner_engine.dispose()


async def test_production_startup_check_uses_the_engines(
    migrated_database: str,
    runtime_database_url: str,
    coordinator_database_url: str,
) -> None:
    """The production startup gate runs both checks for restricted credentials."""
    settings = get_settings().model_copy(update={"app_env": "production"})
    runtime_engine = _engine(runtime_database_url)
    coordinator_engine = _engine(coordinator_database_url)
    try:
        await verify_production_database_roles(
            settings,
            runtime_engine=runtime_engine,
            coordinator_engine=coordinator_engine,
        )
        owner_engine = _engine(migrated_database)
        try:
            with pytest.raises(DatabaseRoleCheckError):
                await verify_production_database_roles(
                    settings,
                    runtime_engine=owner_engine,
                    coordinator_engine=coordinator_engine,
                )
        finally:
            await owner_engine.dispose()
    finally:
        await runtime_engine.dispose()
        await coordinator_engine.dispose()


async def test_runtime_role_with_bypassrls_is_rejected(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A runtime credential that carries BYPASSRLS is rejected."""
    owner_engine = _engine(migrated_database)
    runtime_engine = _engine(runtime_database_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"ALTER ROLE {RUNTIME_ROLE} BYPASSRLS"))
        try:
            with pytest.raises(DatabaseRoleCheckError) as excinfo:
                await verify_runtime_database_role(runtime_engine)
            assert any("BYPASSRLS" in problem for problem in excinfo.value.problems)
        finally:
            async with owner_engine.begin() as connection:
                await connection.execute(text(f"ALTER ROLE {RUNTIME_ROLE} NOBYPASSRLS"))
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


async def test_runtime_role_that_can_assume_operator_is_rejected(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A runtime credential that can SET ROLE app_operator is rejected."""
    owner_engine = _engine(migrated_database)
    runtime_engine = _engine(runtime_database_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"GRANT {OPERATOR_ROLE} TO {RUNTIME_ROLE}"))
        try:
            with pytest.raises(DatabaseRoleCheckError) as excinfo:
                await verify_runtime_database_role(runtime_engine)
            rendered = " | ".join(excinfo.value.problems)
            assert OPERATOR_ROLE in rendered
        finally:
            async with owner_engine.begin() as connection:
                await connection.execute(text(f"REVOKE {OPERATOR_ROLE} FROM {RUNTIME_ROLE}"))
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


async def test_set_role_denial_is_proven_from_the_runtime_connection(
    runtime_database_url: str,
) -> None:
    """The runtime connection cannot SET ROLE app_operator (the check's raw proof)."""
    engine = _engine(runtime_database_url)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text(f"SET ROLE {OPERATOR_ROLE}"))
            await connection.rollback()
    finally:
        await engine.dispose()


async def test_indirect_membership_chain_reaching_bypassrls_is_rejected(
    migrated_database: str,
    runtime_database_url: str,
    coordinator_database_url: str,
) -> None:
    """A transitive membership chain must not hide a privileged role.

    ``app_runtime -> rls_bridge_role -> rls_privileged_role`` (and the same for
    the coordinator) is just as escalatable as a direct grant: PostgreSQL
    resolves ``SET ROLE`` through indirect membership. A direct-only query would
    pass a credential that can reach ``BYPASSRLS``; the recursive check must
    reject both roles and name the reachable privileged role.
    """
    bridge_role = "rls_bridge_role"
    privileged_role = "rls_privileged_role"
    owner_engine = _engine(migrated_database)
    runtime_engine = _engine(runtime_database_url)
    coordinator_engine = _engine(coordinator_database_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"CREATE ROLE {bridge_role} NOLOGIN"))
            await connection.execute(text(f"CREATE ROLE {privileged_role} NOLOGIN BYPASSRLS"))
            await connection.execute(text(f"GRANT {privileged_role} TO {bridge_role}"))
            await connection.execute(text(f"GRANT {bridge_role} TO {RUNTIME_ROLE}"))
            await connection.execute(text(f"GRANT {bridge_role} TO {COORDINATOR_ROLE}"))
        try:
            with pytest.raises(DatabaseRoleCheckError) as runtime_excinfo:
                await verify_runtime_database_role(runtime_engine)
            assert privileged_role in " | ".join(runtime_excinfo.value.problems)

            with pytest.raises(DatabaseRoleCheckError) as coordinator_excinfo:
                await verify_coordinator_database_role(coordinator_engine)
            assert privileged_role in " | ".join(coordinator_excinfo.value.problems)
        finally:
            async with owner_engine.begin() as connection:
                await connection.execute(text(f"REVOKE {bridge_role} FROM {RUNTIME_ROLE}"))
                await connection.execute(text(f"REVOKE {bridge_role} FROM {COORDINATOR_ROLE}"))
                await connection.execute(text(f"REVOKE {privileged_role} FROM {bridge_role}"))
                await connection.execute(text(f"DROP ROLE {bridge_role}"))
                await connection.execute(text(f"DROP ROLE {privileged_role}"))
    finally:
        await runtime_engine.dispose()
        await coordinator_engine.dispose()
        await owner_engine.dispose()
