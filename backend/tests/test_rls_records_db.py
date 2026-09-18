"""Real-PostgreSQL RLS prototype suite (plan P2, ADR-0022).

This suite proves the P2 prototype against a real, migrated PostgreSQL using a
**restricted runtime login** (``app_runtime``) that owns no protected table,
is not a superuser and lacks ``BYPASSRLS``. It is the real-database counterpart
to the pure-Python context unit behaviour and maps to the P2 checkpoints:

- default denial: missing, empty and malformed context return no rows and
  fail closed for writes;
- organisation A reads only its rows; a foreign row cannot be selected,
  inserted, updated or deleted, including through an unscoped query;
- ``WITH CHECK`` rejects a mismatched insert and a tenant-key move;
- context cannot survive commit, rollback, exception, cancellation, timeout or
  pooled-connection reuse;
- a user who is owner in A and viewer in B gets the correct RLS context and
  application permissions on the real runtime-role app path;
- the runtime credential cannot disable policies, alter the schema or assume
  the owner role;
- migration upgrade, downgrade and re-upgrade pass; and
- representative query plans and latency are measured and recorded.

The prototype role (``app_runtime``), policies and ``app_current_tenant_id()``
helper are installed by ``c1d2e3f4a5b6``; the module reverts to base at
teardown, so the prototype leaves no residue.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import CursorResult, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool
from tests.auth_helpers import generate_key_pair
from tests.org_isolation_helpers import (
    FORBIDDEN,
    NOT_FOUND,
    Identity,
    IsolationWorld,
    auth_headers,
    build_isolation_app,
    seed_isolation_world,
)

from app.db.rls import (
    RLS_ORGANISATION_SETTING,
    bind_organisation_context,
    clear_organisation_context,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROLE = "app_runtime"
#: Test-only throwaway credential granted to the NOLOGIN prototype role. The
#: migration deliberately embeds no password; the test provisions one so a real
#: runtime login can be exercised the way a deployment would.
RUNTIME_PASSWORD = "rls-prototype-test-password"

#: A bounded latency budget for the representative list/detail/write operations
#: measured by the plan-check below. Deliberately generous so the check is a
#: regression signal, not a machine-speed assertion.
_LATENCY_BUDGET_MS = 500.0


def _database_reachable(database_url: str) -> bool:
    """Probe the configured database with a short async engine connect."""

    async def _probe() -> bool:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(_probe())


def _runtime_url(owner_url: str) -> str:
    """Return the test database URL for the restricted runtime login."""
    return (
        make_url(owner_url)
        .set(username=RUNTIME_ROLE, password=RUNTIME_PASSWORD)
        .render_as_string(hide_password=False)
    )


async def _provision_runtime_login(owner_url: str) -> None:
    """Grant the prototype role a login credential (test-only, idempotent).

    The migration creates ``app_runtime`` ``NOLOGIN`` so no secret is checked
    in; a deployment grants its own credential out of band. The proof needs a
    real credential, so this test fixture attaches a throwaway one.
    """
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(f"ALTER ROLE {RUNTIME_ROLE} LOGIN PASSWORD '{RUNTIME_PASSWORD}'")
            )
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    """One module-local RSA key for minting the prototype's WorkOS-style tokens."""
    key, _ = generate_key_pair()
    return key


@pytest.fixture
async def runtime_url(migrated_database: str) -> str:
    """Provision the runtime login and return its database URL."""
    await _provision_runtime_login(migrated_database)
    return _runtime_url(migrated_database)


@pytest.fixture
def world(migrated_database: str) -> IsolationWorld:
    """Seed a fresh two-organisation world (owner role, so RLS is bypassed)."""
    return asyncio.run(seed_isolation_world(migrated_database))


async def _runtime_engine(runtime_url: str, *, pooled: bool = False) -> AsyncEngine:
    """Build a runtime-role engine; a single-connection pool enables reuse proof."""
    if pooled:
        return create_async_engine(
            runtime_url,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=1,
            max_overflow=0,
        )
    return create_async_engine(runtime_url, poolclass=NullPool)


async def _seed_tenant_rows(
    owner_url: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed two organisations, one record each and one revision each (owner role).

    The owner credential is used deliberately: seeding must succeed even though
    the runtime role could not write foreign rows.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    record_a, record_b = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:a, :an), (:b, :bn)"),
                {"a": org_a, "an": "RLS A", "b": org_b, "bn": "RLS B"},
            )
            await connection.execute(
                text(
                    "INSERT INTO records (id, organisation_id, title, body, version) "
                    "VALUES (:a, :ao, 'A record', '', 1), (:b, :bo, 'B record', '', 1)"
                ),
                {"a": record_a, "ao": org_a, "b": record_b, "bo": org_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO record_revisions "
                    "(id, record_id, organisation_id, version, action, title, body) "
                    "VALUES (:ra, :a, :ao, 1, 'created', 'A record', ''), "
                    "(:rb, :b, :bo, 1, 'created', 'B record', '')"
                ),
                {
                    "ra": uuid.uuid4(),
                    "rb": uuid.uuid4(),
                    "a": record_a,
                    "b": record_b,
                    "ao": org_a,
                    "bo": org_b,
                },
            )
    finally:
        await engine.dispose()
    return org_a, org_b, record_a, record_b


async def _open(session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
    """Open a runtime session.

    RLS context is bound transaction-locally by ``bind_organisation_context``;
    nothing is installed on the session and no value is re-applied to a later
    transaction.
    """
    return session_factory()


# --- Default denial ---------------------------------------------------------


async def test_missing_context_returns_no_rows_and_denies_writes(
    migrated_database: str, runtime_url: str
) -> None:
    """No bound context: zero tenant rows and every write is rejected."""
    await _seed_tenant_rows(migrated_database)
    engine = await _runtime_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
            assert await session.scalar(text("SELECT count(*) FROM record_revisions")) == 0
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO records (id, organisation_id, title, body, version) "
                        "VALUES (:id, :org, 'x', '', 1)"
                    ),
                    {"id": uuid.uuid4(), "org": uuid.uuid4()},
                )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_empty_and_malformed_context_deny(migrated_database: str, runtime_url: str) -> None:
    """Empty or non-UUID context resolves to NULL and matches no row."""
    org_a, _org_b, _record_a, _record_b = await _seed_tenant_rows(migrated_database)
    engine = await _runtime_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            # Empty value: the supported clear path.
            await clear_organisation_context(session)
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
            # Malformed value: the policy helper must not raise into a leak.
            await session.execute(
                text("SELECT set_config(:setting, 'not-a-uuid', true)"),
                {"setting": RLS_ORGANISATION_SETTING},
            )
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
            # A valid context still works afterwards.
            await bind_organisation_context(session, org_a)
            assert await session.scalar(text("SELECT count(*) FROM records")) == 1
            await session.rollback()
    finally:
        await engine.dispose()


# --- Cross-organisation default denial --------------------------------------


async def test_unscoped_query_returns_only_the_bound_organisation(
    migrated_database: str, runtime_url: str
) -> None:
    """Even a query with no organisation predicate sees only the bound tenant."""
    org_a, org_b, record_a, record_b = await _seed_tenant_rows(migrated_database)
    engine = await _runtime_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            rows = (await session.execute(text("SELECT id, organisation_id FROM records"))).all()
            assert {row.organisation_id for row in rows} == {org_a}
            assert (
                await session.scalar(
                    text("SELECT id FROM records WHERE id = :id"), {"id": record_b}
                )
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT id FROM records WHERE id = :id"), {"id": record_a}
                )
                == record_a
            )
            await session.rollback()
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_b)
            rows = (await session.execute(text("SELECT id FROM records"))).all()
            assert {row.id for row in rows} == {record_b}
            await session.rollback()
    finally:
        await engine.dispose()


async def test_foreign_rows_cannot_be_updated_or_deleted(
    migrated_database: str, runtime_url: str
) -> None:
    """UPDATE/DELETE of a foreign row affect nothing and leave it intact."""
    org_a, _org_b, _record_a, record_b = await _seed_tenant_rows(migrated_database)
    engine = await _runtime_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            updated = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE records SET title = 'hijacked' WHERE id = :id"),
                    {"id": record_b},
                ),
            )
            assert updated.rowcount == 0
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(text("DELETE FROM records WHERE id = :id"), {"id": record_b}),
            )
            assert deleted.rowcount == 0
            await session.rollback()
    finally:
        await engine.dispose()
    # The foreign row is untouched when read by an unrestricted owner connection.
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            title = await connection.scalar(
                text("SELECT title FROM records WHERE id = :id"), {"id": record_b}
            )
            assert title == "B record"
    finally:
        await owner_engine.dispose()


async def test_with_check_rejects_mismatched_insert_and_tenant_key_move(
    migrated_database: str, runtime_url: str
) -> None:
    """Equivalent USING/WITH CHECK: inserts and moves cannot cross tenants."""
    org_a, org_b, record_a, _record_b = await _seed_tenant_rows(migrated_database)
    engine = await _runtime_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            # A same-tenant insert is allowed.
            await session.execute(
                text(
                    "INSERT INTO records (id, organisation_id, title, body, version) "
                    "VALUES (:id, :org, 'own', '', 1)"
                ),
                {"id": uuid.uuid4(), "org": org_a},
            )
            # A mismatched insert is rejected by WITH CHECK.
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO records (id, organisation_id, title, body, version) "
                        "VALUES (:id, :org, 'foreign', '', 1)"
                    ),
                    {"id": uuid.uuid4(), "org": org_b},
                )
            await session.rollback()
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            # Moving a same-tenant row into another tenant is rejected too.
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text("UPDATE records SET organisation_id = :org WHERE id = :id"),
                    {"org": org_b, "id": record_a},
                )
            await session.rollback()
            # The move was rejected, so the row is still in organisation A.
            await bind_organisation_context(session, org_a)
            assert (
                await session.scalar(
                    text("SELECT organisation_id FROM records WHERE id = :id"), {"id": record_a}
                )
                == org_a
            )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_record_revisions_enforce_the_same_boundary(
    migrated_database: str, runtime_url: str
) -> None:
    """The append-only revision ledger carries the same default-deny policy."""
    org_a, org_b, _record_a, _record_b = await _seed_tenant_rows(migrated_database)
    engine = await _runtime_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            rows = (
                await session.execute(text("SELECT organisation_id FROM record_revisions"))
            ).all()
            assert {row.organisation_id for row in rows} == {org_a}
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO record_revisions "
                        "(id, record_id, organisation_id, version, action, title, body) "
                        "VALUES (:id, :rid, :org, 1, 'created', 'foreign', '')"
                    ),
                    {"id": uuid.uuid4(), "rid": uuid.uuid4(), "org": org_b},
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Context lifetime and pool reuse ----------------------------------------


async def test_context_does_not_survive_commit_rollback_or_exception(
    migrated_database: str, runtime_url: str
) -> None:
    """Context clears on every transaction boundary *within the same session*.

    The transaction-local setting is absent after commit, rollback and a
    recovered exception, so a protected read is default-denied until the caller
    explicitly rebinds a validated organisation. This is the per-session
    lifetime proof; the pool-reuse tests cover a *different* session on the
    same physical connection.
    """
    org_a, _org_b, _record_a, _record_b = await _seed_tenant_rows(migrated_database)
    engine = await _runtime_engine(runtime_url, pooled=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            assert await session.scalar(text("SELECT count(*) FROM records")) == 1

            # Commit: the same session's next transaction has no context.
            await session.commit()
            assert await session.scalar(
                text("SELECT current_setting(:s, true)"), {"s": RLS_ORGANISATION_SETTING}
            ) in (None, "")
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
            # An explicit validated rebind restores access.
            await bind_organisation_context(session, org_a)
            assert await session.scalar(text("SELECT count(*) FROM records")) == 1

            # Rollback: the same session's next transaction has no context.
            await session.rollback()
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
            await bind_organisation_context(session, org_a)
            assert await session.scalar(text("SELECT count(*) FROM records")) == 1

            # A recovered statement exception (transaction aborted) followed by
            # rollback: the same session's next transaction has no context.
            with pytest.raises(DBAPIError):
                await session.execute(text("SELECT 1 / 0"))
            await session.rollback()
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0

        # A brand-new session on the freed pooled connection is default-denied.
        async with await _open(factory) as session:
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
    finally:
        await engine.dispose()


async def test_interleaved_organisations_on_one_pooled_connection(
    migrated_database: str, runtime_url: str
) -> None:
    """Reusing one physical connection across tenants never leaks context."""
    org_a, org_b, record_a, record_b = await _seed_tenant_rows(migrated_database)
    engine = await _runtime_engine(runtime_url, pooled=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            assert (
                await session.scalar(
                    text("SELECT id FROM records WHERE id = :id"), {"id": record_a}
                )
                == record_a
            )
            assert (
                await session.scalar(
                    text("SELECT id FROM records WHERE id = :id"), {"id": record_b}
                )
                is None
            )
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_b)
            assert (
                await session.scalar(
                    text("SELECT id FROM records WHERE id = :id"), {"id": record_b}
                )
                == record_b
            )
            assert (
                await session.scalar(
                    text("SELECT id FROM records WHERE id = :id"), {"id": record_a}
                )
                is None
            )
        async with await _open(factory) as session:
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
    finally:
        await engine.dispose()


async def test_timeout_and_cancellation_do_not_leak_context(
    migrated_database: str, runtime_url: str
) -> None:
    """A cancelled/timed-out transaction leaves no context on the connection."""
    org_a, _org_b, _record_a, _record_b = await _seed_tenant_rows(migrated_database)
    engine = await _runtime_engine(runtime_url, pooled=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            await session.execute(text("SET LOCAL statement_timeout = '50ms'"))
            with pytest.raises(DBAPIError):
                await session.execute(text("SELECT pg_sleep(1)"))
            await session.rollback()
        async with await _open(factory) as session:
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await session.execute(text("SELECT pg_sleep(1)"))
            await session.rollback()
        async with await _open(factory) as session:
            assert await session.scalar(text("SELECT count(*) FROM records")) == 0
    finally:
        await engine.dispose()


# --- Restricted runtime credential ------------------------------------------


async def test_runtime_credential_cannot_disable_policies_or_alter_schema(
    migrated_database: str, runtime_url: str
) -> None:
    """The runtime role is non-owner, non-superuser and non-BYPASSRLS.

    The owner-assumption case uses the *actual* owner of the protected table
    discovered from the catalogue, not a hard-coded environment role name, so
    the proof is not tied to the local ``app`` naming.
    """
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            flags = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role"),
                    {"role": RUNTIME_ROLE},
                )
            ).one()
            assert flags.rolsuper is False
            assert flags.rolbypassrls is False
            owner = await connection.scalar(
                text("SELECT relowner::regrole::text FROM pg_class WHERE relname = 'records'")
            )
            assert owner is not None
            assert owner != RUNTIME_ROLE
    finally:
        await owner_engine.dispose()

    engine = await _runtime_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with await _open(factory) as session:
            for statement in (
                "ALTER TABLE records DISABLE ROW LEVEL SECURITY",
                "ALTER TABLE records NO FORCE ROW LEVEL SECURITY",
                "DROP TABLE records",
                "ALTER TABLE records ADD COLUMN hacked integer",
                "CREATE TABLE rls_escalation (id integer)",
                "ALTER ROLE app_runtime BYPASSRLS",
                f"SET ROLE {owner}",
            ):
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
                await session.rollback()
    finally:
        await engine.dispose()


# --- Real app path with the runtime credential ------------------------------


@pytest.fixture
async def runtime_app(
    migrated_database: str,
    runtime_url: str,
    private_key: rsa.RSAPrivateKey,
) -> AsyncIterator[FastAPI]:
    """Build the real ASGI app wired to the restricted runtime role."""
    built = build_isolation_app(runtime_url, private_key)
    try:
        yield built
    finally:
        await built.state.isolation_engine.dispose()


@pytest.fixture
async def client(runtime_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=runtime_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as async_client:
        yield async_client


async def _call(
    client: AsyncClient,
    method: str,
    path: str,
    *,
    private_key: rsa.RSAPrivateKey,
    identity: Identity,
    org_id: uuid.UUID | None = None,
    json: dict[str, object] | None = None,
    params: dict[str, str | int] | None = None,
) -> Response:
    return await client.request(
        method,
        path,
        headers=auth_headers(private_key, identity, org_id=org_id),
        json=json,
        params=params,
    )


async def test_runtime_app_sets_context_and_permissions_per_request(
    migrated_database: str,
    world: IsolationWorld,
    client: AsyncClient,
    private_key: rsa.RSAPrivateKey,
) -> None:
    """Owner-in-A/viewer-in-B gets correct context and permissions under RLS.

    The app runs on the restricted runtime role, so a successful write proves
    ``get_current_membership`` bound the transaction-local context *and* that
    the post-commit refresh re-applied it. A foreign record stays a 404 under
    the real policy, and the viewer's write is still a 403.
    """
    created = await _call(
        client,
        "POST",
        "/api/v1/records",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_a,
        json={"title": "Created under RLS", "body": ""},
    )
    assert created.status_code == 201, created.text
    assert created.json()["title"] == "Created under RLS"

    listing_a = await _call(
        client,
        "GET",
        "/api/v1/records",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_a,
    )
    assert listing_a.status_code == 200, listing_a.text
    titles = {item["title"] for item in listing_a.json()["items"]}
    assert "Created under RLS" in titles
    assert "B record" not in titles

    viewer_write = await _call(
        client,
        "POST",
        "/api/v1/records",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_b,
        json={"title": "Should be denied", "body": ""},
    )
    assert viewer_write.status_code == FORBIDDEN, viewer_write.text
    assert viewer_write.json()["code"] == "permission_denied"

    viewer_read_b = await _call(
        client,
        "GET",
        f"/api/v1/records/{world.record_b}",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_b,
    )
    assert viewer_read_b.status_code == 200, viewer_read_b.text
    assert viewer_read_b.json()["title"] == "B record"

    # The same user in the A context cannot see B's record: both the service
    # predicate and the RLS policy deny it, and the contract stays a 404.
    cross_org = await _call(
        client,
        "GET",
        f"/api/v1/records/{world.record_b}",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_a,
    )
    assert cross_org.status_code == NOT_FOUND, cross_org.text
    assert cross_org.json()["code"] == "record_not_found"


async def test_runtime_role_serves_non_tenant_routes_without_context(
    migrated_database: str,
    client: AsyncClient,
    private_key: rsa.RSAPrivateKey,
    world: IsolationWorld,
) -> None:
    """Health, auth, public and platform paths work with no tenant context.

    P2 claims these paths stay functional under the restricted runtime role
    without fabricating an organisation context (ADR-0022 decision 9). None of
    them binds ``app.organisation_id`` and none touches a protected row.
    """
    health = await client.get("/health")
    assert health.status_code == 200, health.text
    assert health.json()["status"] == "ok"

    ready = await client.get("/ready")
    assert ready.status_code == 200, ready.text

    # Authentication surface: a valid token with no X-Org-Id header.
    me = await client.get(
        "/api/v1/me",
        headers=auth_headers(private_key, world.a_owner),
    )
    assert me.status_code == 200, me.text

    # Signature-gated public webhook surface: fail-closed, never a 500.
    webhook = await client.post("/api/v1/webhooks/workos", content=b"{}")
    assert webhook.status_code in (400, 401), webhook.text

    # Platform plane: the platform-only user needs no organisation context.
    platform = await client.get(
        "/api/v1/platform/admins",
        headers=auth_headers(private_key, world.platform_only),
    )
    assert platform.status_code == 200, platform.text


# --- Migration reversibility -------------------------------------------------


def test_migration_downgrade_and_reupgrade(migrated_database: str) -> None:
    """The RLS chain reverses below the prototype and re-applies with no residue.

    Plan P3 adds the production records-group enablement migration above this
    prototype. Reverting to ``b1c2d3e4f5a6`` runs both downgrades while leaving
    the schema tables in place: the prototype role, helper and RLS disappear,
    and re-upgrading to head re-installs the prototype and then the canonical
    production policy.

    Alembic's async environment runs its own event loop, so this test is
    synchronous and inspects the schema with separate ``asyncio.run`` calls.
    """
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))

    async def _absent() -> None:
        owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
        try:
            async with owner_engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM pg_roles WHERE rolname = :role"),
                        {"role": RUNTIME_ROLE},
                    )
                    == 0
                )
                assert (
                    await connection.scalar(
                        text("SELECT relrowsecurity FROM pg_class WHERE relname = 'records'")
                    )
                    is False
                )
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM pg_proc WHERE proname = 'app_current_tenant_id'")
                    )
                    == 0
                )
        finally:
            await owner_engine.dispose()

    async def _present() -> None:
        owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
        try:
            async with owner_engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM pg_roles WHERE rolname = :role"),
                        {"role": RUNTIME_ROLE},
                    )
                    == 1
                )
                assert await connection.scalar(
                    text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'records'")
                )
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_policies "
                            "WHERE tablename = 'records' "
                            "AND policyname = 'records_organisation_isolation'"
                        )
                    )
                    == 1
                )
        finally:
            await owner_engine.dispose()

    command.downgrade(config, "b1c2d3e4f5a6")
    try:
        asyncio.run(_absent())
    finally:
        command.upgrade(config, "head")
    asyncio.run(_present())


async def _force_drop_runtime_role(database_url: str) -> None:
    """Revoke prototype grants and drop ``app_runtime`` if it exists (test cleanup)."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f"""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}'
                        ) THEN
                            EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public
                                     FROM {RUNTIME_ROLE}';
                            EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
                                     FROM {RUNTIME_ROLE}';
                            EXECUTE 'REVOKE ALL ON SCHEMA public FROM {RUNTIME_ROLE}';
                            EXECUTE 'DROP ROLE {RUNTIME_ROLE}';
                        END IF;
                    END
                    $$;
                    """
                )
            )
    finally:
        await engine.dispose()


def test_migration_adopts_a_preexisting_runtime_role_safely(migrated_database: str) -> None:
    """A deployment-provisioned role is corrected, not dropped on rollback.

    The migration must not assume it created ``app_runtime``: a deployment may
    have pre-provisioned a login credential. The upgrade forces the safe
    attributes and removes memberships that could escalate to a privileged
    role; the downgrade leaves the adopted role (and its credential) in place
    because it does not carry the prototype migration's ownership marker (the
    production records-group enablement above it adopts without re-marking).
    """
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))

    async def _run(sql: str, params: dict[str, object] | None = None) -> None:
        engine = create_async_engine(migrated_database, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(sql), params or {})
        finally:
            await engine.dispose()

    async def _table_owner() -> str:
        engine = create_async_engine(migrated_database, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                owner = await connection.scalar(
                    text("SELECT relowner::regrole::text FROM pg_class WHERE relname = 'records'")
                )
        finally:
            await engine.dispose()
        assert owner is not None
        return owner

    async def _role_state_and_membership() -> tuple[Any, int]:
        engine = create_async_engine(migrated_database, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                state = (
                    await connection.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
                            "WHERE rolname = :role"
                        ),
                        {"role": RUNTIME_ROLE},
                    )
                ).one()
                membership = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_auth_members m "
                        "JOIN pg_roles granted ON granted.oid = m.roleid "
                        "JOIN pg_roles member ON member.oid = m.member "
                        "WHERE member.rolname = :role AND granted.rolname = :owner"
                    ),
                    {"role": RUNTIME_ROLE, "owner": await _current_owner(connection)},
                )
        finally:
            await engine.dispose()
        return state, int(membership or 0)

    async def _current_owner(connection: Any) -> str:
        return str(
            await connection.scalar(
                text("SELECT relowner::regrole::text FROM pg_class WHERE relname = 'records'")
            )
        )

    # Start from a clean boundary with no role (reverting the prototype and the
    # production records-group migration) while the records table still exists,
    # then pre-provision an unsafe login role that is also a member of that
    # table's owner.
    command.downgrade(config, "b1c2d3e4f5a6")
    try:
        owner = asyncio.run(_table_owner())
        asyncio.run(_run(f"CREATE ROLE {RUNTIME_ROLE} LOGIN SUPERUSER BYPASSRLS"))
        asyncio.run(_run(f"GRANT {owner} TO {RUNTIME_ROLE}"))
        command.upgrade(config, "head")

        state, membership = asyncio.run(_role_state_and_membership())
        assert state.rolsuper is False, "the adopted role must be forced non-superuser"
        assert state.rolbypassrls is False, "the adopted role must lose BYPASSRLS"
        assert state.rolcanlogin is True, "the adopted role's own credential is preserved"
        assert membership == 0, "escalation membership must be revoked"

        # Ownership is explicit: an adopted role is not marked as created here,
        # so the full downgrade must not drop it (or its out-of-band credential).
        command.downgrade(config, "b1c2d3e4f5a6")
        assert asyncio.run(_role_exists(migrated_database)) is True
    finally:
        # Restore a clean, migration-created role at head for the shared fixture.
        command.downgrade(config, "base")
        asyncio.run(_force_drop_runtime_role(migrated_database))
        command.upgrade(config, "head")


async def _role_exists(database_url: str) -> bool:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(
                text("SELECT count(*) FROM pg_roles WHERE rolname = :role"),
                {"role": RUNTIME_ROLE},
            )
    finally:
        await engine.dispose()
    return bool(count)


# --- Query plan and latency findings ----------------------------------------

_LIST_PAGE_SQL = (
    "SELECT id, title FROM records WHERE organisation_id = :org "
    "ORDER BY created_at DESC, id DESC LIMIT 50"
)


async def _seed_representative_records(
    owner_url: str,
    *,
    organisations: int = 40,
    rows_per_organisation: int = 50,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a realistically multi-tenant ``records`` table via the owner role.

    A two-row table cannot give a representative plan: the tenant filter is not
    selective and a sequential scan is genuinely optimal. Seeding many
    organisations makes the indexed ``organisation_id``/``created_at`` access
    path the representative choice. Returns organisation A and one of its
    record ids (used for the detail plan).
    """
    org_a = uuid.uuid4()
    other_orgs = [uuid.uuid4() for _ in range(organisations - 1)]
    sample_id = uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:id, :name)"),
                [
                    {"id": org, "name": f"RLS plan {i}"}
                    for i, org in enumerate([org_a, *other_orgs])
                ],
            )
            rows = [{"id": sample_id, "org": org_a, "title": "sample"}]
            for org in [org_a, *other_orgs]:
                rows.extend(
                    {"id": uuid.uuid4(), "org": org, "title": f"row {index}"}
                    for index in range(rows_per_organisation)
                )
            await connection.execute(
                text(
                    "INSERT INTO records (id, organisation_id, title, body, version) "
                    "VALUES (:id, :org, :title, '', 1)"
                ),
                rows,
            )
            await connection.execute(text("ANALYZE records"))
    finally:
        await engine.dispose()
    return org_a, sample_id


async def _explain(session: AsyncSession, statement: str, params: dict[str, object]) -> str:
    """Return the text plan for ``statement`` under the session's current settings."""
    rows = (await session.execute(text("EXPLAIN " + statement), params)).all()
    return "\n".join(row[0] for row in rows)


async def test_representative_query_plans_use_the_tenant_index(
    migrated_database: str, runtime_url: str
) -> None:
    """Normal-planner list/detail plans use the tenant indexes; latency bounded.

    A representative multi-tenant table is seeded so the tenant filter is
    selective. The list and detail plans are recorded under the **normal**
    planner settings (no ``enable_seqscan`` hint) and measured under those
    settings; a separate forced-plan check proves the composite index is
    available even where a tiny table would legitimately prefer a sequential
    scan.
    """
    org_a, sample_id = await _seed_representative_records(migrated_database)
    engine = await _runtime_engine(runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Forced-index availability check: its own transaction, rolled back.
        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            await session.execute(text("SET LOCAL enable_seqscan = off"))
            forced_plan = await _explain(session, _LIST_PAGE_SQL, {"org": org_a})
            assert "ix_records_organisation_id_created_at" in forced_plan
            await session.rollback()

        async with await _open(factory) as session:
            await bind_organisation_context(session, org_a)
            list_plan = await _explain(session, _LIST_PAGE_SQL, {"org": org_a})
            detail_plan = await _explain(
                session, "SELECT * FROM records WHERE id = :id", {"id": sample_id}
            )
            print("RLS list plan (normal planner):\n" + list_plan)
            print("RLS detail plan (normal planner):\n" + detail_plan)
            assert "Seq Scan" not in list_plan
            assert "ix_records_organisation_id_created_at" in list_plan
            assert "Seq Scan" not in detail_plan
            assert "pk_records" in detail_plan

            # Latency under the normal planner settings (no forced hints).
            loop = asyncio.get_running_loop()
            start = loop.time()
            list_rows = (await session.execute(text(_LIST_PAGE_SQL), {"org": org_a})).all()
            list_ms = (loop.time() - start) * 1000

            detail_id = list_rows[0].id
            start = loop.time()
            await session.execute(text("SELECT * FROM records WHERE id = :id"), {"id": detail_id})
            detail_ms = (loop.time() - start) * 1000

            start = loop.time()
            await session.execute(
                text(
                    "INSERT INTO records (id, organisation_id, title, body, version) "
                    "VALUES (:id, :org, 'measured', '', 1)"
                ),
                {"id": uuid.uuid4(), "org": org_a},
            )
            write_ms = (loop.time() - start) * 1000
            print(
                json.dumps(
                    {
                        "list_ms": round(list_ms, 3),
                        "detail_ms": round(detail_ms, 3),
                        "write_ms": round(write_ms, 3),
                    }
                )
            )
            assert list_ms < _LATENCY_BUDGET_MS
            assert detail_ms < _LATENCY_BUDGET_MS
            assert write_ms < _LATENCY_BUDGET_MS
            await session.rollback()
    finally:
        await engine.dispose()
