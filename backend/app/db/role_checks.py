"""Runtime database-role privilege verification (plan P4, ADR-0022 decisions 2/4).

The RLS backstop only means something if the normal application path
authenticates as a **restricted** role: non-owner, non-superuser and without
``BYPASSRLS``. The P2 prototype and every later group migration provision such
roles and ``app/db/session.py`` refuses to start a production process without a
runtime *credential*, but that is a capability, not proof: a deployment can
still point ``DATABASE_RUNTIME_URL`` at the schema owner or a ``BYPASSRLS`` role
and silently defeat every policy (``docs/rls-rollout.md`` §5). This module closes
that gap. It connects with the configured credential and proves, from the
server catalogue, that the authenticated role owns no table in ``public``, lacks
``BYPASSRLS``/``SUPERUSER``/``CREATEDB``/``CREATEROLE`` and inherits no
privileged role. It also proves the group-6 operational credential is isolated:
``app_operator`` is the one application role that may carry ``BYPASSRLS``, owns
no table and has no membership in either direction, and the ordinary runtime
role cannot ``SET ROLE`` it (ADR-0022 decision 4).

The check never connects with the owner credential — proving the *runtime*
credential is restricted is the whole point, and a misconfigured runtime URL is
caught precisely because the owner would fail the ownership/BYPASSRLS
predicates. It runs automatically at production startup (``create_app``'s
lifespan, before the process serves traffic) and is exposed by
``scripts/verify_db_roles`` as the documented deployment check
(``docs/operations.md`` → Database roles; ``docs/rls-rollout.md`` §5). Outside
production the startup hook is a no-op, because the local/test arrangement uses
the owner credential with RLS disabled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import Settings

logger = structlog.get_logger()

#: The restricted, non-owner runtime role the API and workers use (ADR-0022
#: decision 2). Every rollout migration grants its policies to this name.
RUNTIME_ROLE: Final = "app_runtime"

#: The non-bypass outbox-coordinator role (ADR-0022 decision 3).
COORDINATOR_ROLE: Final = "app_coordinator"

#: The ``NOLOGIN`` aggregate-metrics role (ADR-0022 decision 3).
METRICS_ROLE: Final = "app_metrics"

#: The isolated operational/backup credential; the only application role that
#: may carry ``BYPASSRLS`` (ADR-0022 decision 4).
OPERATOR_ROLE: Final = "app_operator"

#: Every role this project provisions. The bypass check asserts that only
#: ``app_operator`` carries ``BYPASSRLS`` among them; the operational
#: credential's absence is required for every other name.
APPLICATION_ROLES: Final = (RUNTIME_ROLE, COORDINATOR_ROLE, METRICS_ROLE, OPERATOR_ROLE)


class DatabaseRoleCheckError(RuntimeError):
    """Raised when a configured database credential is not safely restricted.

    The message names the role and the exact catalogue facts that make it
    unsafe, so a failed deployment log points straight at the misconfiguration
    without echoing any credential material.
    """

    def __init__(self, label: str, role: str, problems: Sequence[str]) -> None:
        self.label = label
        self.role = role
        self.problems = tuple(problems)
        super().__init__(
            f"{label} database role {role!r} is not safe for row-level security: "
            + "; ".join(problems)
        )


@dataclass(frozen=True)
class RoleReport:
    """The server-catalogue facts collected for one authenticated role."""

    label: str
    role: str
    rolsuper: bool
    rolbypassrls: bool
    rolcreatedb: bool
    rolcreaterole: bool
    owned_table_count: int
    privileged_memberships: tuple[str, ...]

    def problems(self, *, expected_role: str | None = None) -> tuple[str, ...]:
        """Return the reasons this role is unsafe, or an empty tuple when safe."""
        problems: list[str] = []
        if expected_role is not None and self.role != expected_role:
            problems.append(f"authenticated as {self.role!r}, expected {expected_role!r}")
        if self.rolsuper:
            problems.append("is a superuser")
        if self.rolbypassrls:
            problems.append("carries BYPASSRLS")
        if self.rolcreatedb:
            problems.append("carries CREATEDB")
        if self.rolcreaterole:
            problems.append("carries CREATEROLE")
        if self.owned_table_count:
            problems.append(f"owns {self.owned_table_count} table(s) in schema public")
        if self.privileged_memberships:
            problems.append(
                "inherits privileged role(s): " + ", ".join(self.privileged_memberships)
            )
        return tuple(problems)


_ROLE_ATTRIBUTES_SQL = text(
    """
    SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
    FROM pg_roles
    WHERE rolname = current_user
    """
)

_OWNED_TABLE_COUNT_SQL = text(
    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = :role"
)

#: Memberships that would let the role escalate: a superuser, a ``BYPASSRLS``
#: role, or the owner of a protected table (which could disable policies).
#: PostgreSQL resolves ``SET ROLE`` through *indirect* membership, so the graph
#: is traversed recursively: ``app_runtime -> bridge_role -> owner`` is just as
#: escalatable as a direct grant, and the same holds for a transitive superuser
#: or ``BYPASSRLS`` role. A direct-only query would pass such a credential. The
#: runtime and coordinator roles are ``NOINHERIT`` and provisioned with no
#: memberships, so this must always be empty.
_PRIVILEGED_MEMBERSHIPS_SQL = text(
    """
    WITH RECURSIVE reachable(roleid) AS (
        SELECT m.roleid
        FROM pg_auth_members m
        JOIN pg_roles member ON member.oid = m.member
        WHERE member.rolname = :role
        UNION
        SELECT m.roleid
        FROM reachable
        JOIN pg_auth_members m ON m.member = reachable.roleid
    )
    SELECT DISTINCT granted.rolname
    FROM reachable
    JOIN pg_roles granted ON granted.oid = reachable.roleid
    WHERE granted.rolsuper
       OR granted.rolbypassrls
       OR EXISTS (
           SELECT 1 FROM pg_tables t
           WHERE t.schemaname = 'public' AND t.tableowner = granted.rolname
       )
    ORDER BY granted.rolname
    """
)

_OPERATOR_ATTRIBUTES_SQL = text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role")

#: Roles the operator is a member of (it inherits their privileges).
_OPERATOR_MEMBER_OF_SQL = text(
    """
    SELECT granted.rolname
    FROM pg_auth_members m
    JOIN pg_roles granted ON granted.oid = m.roleid
    JOIN pg_roles member ON member.oid = m.member
    WHERE member.rolname = :role
    ORDER BY granted.rolname
    """
)

#: Roles that are members of the operator (they could assume its bypass).
_OPERATOR_HAS_MEMBER_SQL = text(
    """
    SELECT member.rolname
    FROM pg_auth_members m
    JOIN pg_roles granted ON granted.oid = m.roleid
    JOIN pg_roles member ON member.oid = m.member
    WHERE granted.rolname = :role
    ORDER BY member.rolname
    """
)

#: The ``BYPASSRLS`` flag for the project's application roles. The role names
#: are internal constants, never user input, so interpolating them is safe.
_APPLICATION_BYPASS_SQL = text(
    "SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname IN ("
    + ", ".join(f"'{role}'" for role in APPLICATION_ROLES)
    + ")"
)

#: PostgreSQL SQLSTATE for ``insufficient_privilege``. It is the *only* result
#: the raw ``SET ROLE`` denial probe may read as proof of denial; a timeout,
#: cancellation, connection loss or any other database failure must fail the
#: check closed rather than masquerade as a successful denial.
_INSUFFICIENT_PRIVILEGE_SQLSTATE: Final = "42501"


def _dbapi_sqlstate(exc: DBAPIError) -> str | None:
    """Return the PostgreSQL SQLSTATE carried by a :class:`DBAPIError`, if any.

    asyncpg and psycopg expose it as ``sqlstate``; psycopg2 as ``pgcode``.
    """
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


async def inspect_connected_role(connection: AsyncConnection, *, label: str) -> RoleReport:
    """Collect the safety facts for the role this connection authenticated as."""
    attributes = (await connection.execute(_ROLE_ATTRIBUTES_SQL)).one()
    role = attributes.rolname
    owned_table_count = await connection.scalar(_OWNED_TABLE_COUNT_SQL, {"role": role})
    privileged_memberships = tuple(
        (await connection.execute(_PRIVILEGED_MEMBERSHIPS_SQL, {"role": role})).scalars().all()
    )
    return RoleReport(
        label=label,
        role=role,
        rolsuper=attributes.rolsuper,
        rolbypassrls=attributes.rolbypassrls,
        rolcreatedb=attributes.rolcreatedb,
        rolcreaterole=attributes.rolcreaterole,
        owned_table_count=int(owned_table_count or 0),
        privileged_memberships=privileged_memberships,
    )


async def _runtime_can_assume_operator(connection: AsyncConnection) -> bool:
    """Return whether the current role may ``SET ROLE`` to ``app_operator``.

    ``SET ROLE`` requires membership regardless of ``NOINHERIT``, so this proves
    the runtime credential cannot borrow the one bypass credential. The
    statement runs in the current transaction; a failure aborts it (the caller
    is about to raise) and a success is undone by ``RESET ROLE`` and the
    connection's rollback on close.

    Only PostgreSQL's explicit ``insufficient_privilege`` result counts as the
    expected denial. A timeout, cancellation, connection loss or other database
    failure is not evidence that the role cannot assume the operator, so the
    probe fails closed with :class:`DatabaseRoleCheckError` instead.
    """
    try:
        await connection.execute(text(f"SET ROLE {OPERATOR_ROLE}"))
    except DBAPIError as exc:
        sqlstate = _dbapi_sqlstate(exc)
        if sqlstate != _INSUFFICIENT_PRIVILEGE_SQLSTATE:
            raise DatabaseRoleCheckError(
                "operational",
                OPERATOR_ROLE,
                [
                    "could not prove the runtime credential cannot SET ROLE "
                    f"{OPERATOR_ROLE!r}: unexpected {type(exc.orig).__name__} "
                    f"(SQLSTATE {sqlstate or 'unknown'}) while assuming the role"
                ],
            ) from exc
        return False
    await connection.execute(text("RESET ROLE"))
    return True


async def verify_operator_credential(
    connection: AsyncConnection,
    *,
    require_operator: bool,
    runtime_role: str,
) -> None:
    """Verify the isolated operational credential and the application bypass set.

    ``app_operator`` is the deliberate, reviewed exception (ADR-0022 decision 4):
    it is the only application role allowed to carry ``BYPASSRLS``, it owns no
    table and has no membership in either direction, and the runtime role cannot
    ``SET ROLE`` it.
    """
    bypass_rows = (await connection.execute(_APPLICATION_BYPASS_SQL)).mappings().all()
    present = {row["rolname"]: bool(row["rolbypassrls"]) for row in bypass_rows}
    bypass_roles = tuple(sorted(name for name, bypass in present.items() if bypass))
    problems: list[str] = []

    if OPERATOR_ROLE not in present:
        if require_operator:
            problems.append(
                f"{OPERATOR_ROLE!r} is absent; the reviewed operational credential "
                "is required once row-level security is enforced"
            )
    else:
        attributes = (
            await connection.execute(_OPERATOR_ATTRIBUTES_SQL, {"role": OPERATOR_ROLE})
        ).one()
        owned_table_count = await connection.scalar(_OWNED_TABLE_COUNT_SQL, {"role": OPERATOR_ROLE})
        is_member_of = tuple(
            (await connection.execute(_OPERATOR_MEMBER_OF_SQL, {"role": OPERATOR_ROLE}))
            .scalars()
            .all()
        )
        has_member = tuple(
            (await connection.execute(_OPERATOR_HAS_MEMBER_SQL, {"role": OPERATOR_ROLE}))
            .scalars()
            .all()
        )
        if attributes.rolsuper:
            problems.append(f"{OPERATOR_ROLE!r} is a superuser")
        if not attributes.rolbypassrls:
            problems.append(f"{OPERATOR_ROLE!r} lacks BYPASSRLS")
        if owned_table_count:
            problems.append(
                f"{OPERATOR_ROLE!r} owns {int(owned_table_count)} table(s) in schema public"
            )
        if is_member_of:
            problems.append(f"{OPERATOR_ROLE!r} is a member of: " + ", ".join(is_member_of))
        if has_member:
            problems.append(f"role(s) are members of {OPERATOR_ROLE!r}: " + ", ".join(has_member))
        if bypass_roles != (OPERATOR_ROLE,):
            rendered = ", ".join(bypass_roles) if bypass_roles else "none"
            problems.append(
                f"only {OPERATOR_ROLE!r} may carry BYPASSRLS among application roles; "
                f"found: {rendered}"
            )
        if await _runtime_can_assume_operator(connection):
            problems.append(
                f"{runtime_role!r} can SET ROLE {OPERATOR_ROLE!r}; the ordinary runtime "
                "credential must not be able to assume the bypass credential"
            )

    if problems:
        raise DatabaseRoleCheckError("operational", OPERATOR_ROLE, problems)


async def verify_runtime_database_role(
    engine: AsyncEngine, *, require_operator: bool = True
) -> RoleReport:
    """Verify the runtime engine authenticates as the restricted runtime role.

    Raises :class:`DatabaseRoleCheckError` with the exact violations when the
    credential is the owner/superuser, carries ``BYPASSRLS`` or inherits a
    privileged role. Also verifies the operational credential's isolation via a
    catalogue read on the same connection (``require_operator`` controls whether
    its absence is fatal).
    """
    async with engine.connect() as connection:
        report = await inspect_connected_role(connection, label="runtime")
        problems = report.problems(expected_role=RUNTIME_ROLE)
        if problems:
            raise DatabaseRoleCheckError("runtime", report.role, problems)
        await verify_operator_credential(
            connection, require_operator=require_operator, runtime_role=report.role
        )
    logger.info("database_role_verified", role=report.role, label="runtime")
    return report


async def verify_coordinator_database_role(engine: AsyncEngine) -> RoleReport:
    """Verify the coordinator engine authenticates as the restricted coordinator role."""
    async with engine.connect() as connection:
        report = await inspect_connected_role(connection, label="coordinator")
        problems = report.problems(expected_role=COORDINATOR_ROLE)
        if problems:
            raise DatabaseRoleCheckError("coordinator", report.role, problems)
    logger.info("database_role_verified", role=report.role, label="coordinator")
    return report


async def verify_production_database_roles(
    settings: Settings,
    *,
    runtime_engine: AsyncEngine,
    coordinator_engine: AsyncEngine | None = None,
) -> None:
    """Run the startup role checks for a production process (plan P4).

    Outside production this is a deliberate no-op: the local and test profiles
    use the owner credential with RLS disabled, so requiring restricted roles
    there would make development impossible. In production the process must not
    begin serving traffic unless the credentials are proven restricted, so a
    violation raises and aborts startup.
    """
    if settings.app_env != "production":
        logger.debug("database_role_check_skipped", app_env=settings.app_env)
        return
    await verify_runtime_database_role(runtime_engine)
    if coordinator_engine is not None:
        await verify_coordinator_database_role(coordinator_engine)
