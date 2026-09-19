"""Shared real-PostgreSQL helpers for the RLS rollout suites (plan P3).

``test_rls_records_db.py`` proved the P2 prototype with module-local helpers.
The production group rollouts (``docs/rls-rollout.md`` §3) each need the same
scaffolding against the restricted ``app_runtime`` login: reachability probing,
a throwaway credential for the ``NOLOGIN`` role, a runtime-role engine, and a
two-organisation seed. Those are collected here so a group suite stays focused
on the behaviour it is proving.

The seeding helpers deliberately connect with the **owner** credential
(``DATABASE_URL``): a seed must be able to write foreign rows that the runtime
role is denied, and it must run with RLS bypassed the way a migration does.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: The restricted, non-owner, non-``BYPASSRLS`` runtime role (ADR-0022
#: decision 2). The P2 prototype creates it ``NOLOGIN``; a deployment grants
#: its own credential out of band.
RUNTIME_ROLE = "app_runtime"

#: Test-only throwaway credential attached to the ``NOLOGIN`` role. The
#: migrations deliberately embed no secret, so a real runtime login can be
#: exercised the way a deployment would.
RUNTIME_PASSWORD = "rls-group-test-password"

#: The group-2 operational-metrics role (plan P3, ADR-0022 decision 3). It is
#: created ``NOLOGIN``; a focused test attaches a throwaway credential to prove
#: the narrow policy is load-bearing.
METRICS_ROLE = "app_metrics"

#: Test-only throwaway credential for the operational-metrics role.
METRICS_PASSWORD = "rls-metrics-test-password"


def alembic_config() -> Config:
    """Return an Alembic ``Config`` pointed at the backend project."""
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return config


def database_reachable(database_url: str) -> bool:
    """Probe ``database_url`` with a short async engine connect."""

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


def runtime_url(owner_url: str) -> str:
    """Return the test database URL for the restricted runtime login."""
    return (
        make_url(owner_url)
        .set(username=RUNTIME_ROLE, password=RUNTIME_PASSWORD)
        .render_as_string(hide_password=False)
    )


def provision_runtime_login(owner_url: str) -> None:
    """Grant the prototype/rollout role a login credential (idempotent)."""

    async def _run() -> None:
        engine = create_async_engine(owner_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f"ALTER ROLE {RUNTIME_ROLE} LOGIN PASSWORD '{RUNTIME_PASSWORD}'")
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def provision_metrics_login(owner_url: str) -> None:
    """Grant the operational-metrics role a throwaway login credential."""

    async def _run() -> None:
        engine = create_async_engine(owner_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f"ALTER ROLE {METRICS_ROLE} LOGIN PASSWORD '{METRICS_PASSWORD}'")
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def metrics_url(owner_url: str) -> str:
    """Return the test database URL for the operational-metrics login."""
    return (
        make_url(owner_url)
        .set(username=METRICS_ROLE, password=METRICS_PASSWORD)
        .render_as_string(hide_password=False)
    )


def runtime_engine(url: str, *, pooled: bool = False) -> AsyncEngine:
    """Build a runtime-role engine; a single-connection pool enables reuse proof."""
    if pooled:
        return create_async_engine(
            url,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=1,
            max_overflow=0,
        )
    return create_async_engine(url, poolclass=NullPool)


async def seed_two_organisation_records(
    owner_url: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed two organisations, one record and one revision each (owner role)."""
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    record_a, record_b = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:a, :an), (:b, :bn)"),
                {"a": org_a, "an": "Group A", "b": org_b, "bn": "Group B"},
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


async def seed_two_organisation_files(
    owner_url: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed two organisations and one file row each (owner role).

    The owner credential is used deliberately: grouping ``files`` is a direct
    tenant table, so a seed must be able to write foreign rows the restricted
    runtime role is denied. Returns ``(org_a, org_b, file_a, file_b)``.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    file_a, file_b = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:a, :an), (:b, :bn)"),
                {"a": org_a, "an": "Files Group A", "b": org_b, "bn": "Files Group B"},
            )
            await connection.execute(
                text(
                    "INSERT INTO files "
                    "(id, organisation_id, storage_provider, storage_bucket, object_key, "
                    "original_filename, content_type, size_bytes, status) "
                    "VALUES (:a, :ao, 'fake', 'bucket', :ka, 'a.pdf', "
                    "'application/pdf', 1, 'uploaded'), "
                    "(:b, :bo, 'fake', 'bucket', :kb, 'b.pdf', "
                    "'application/pdf', 1, 'uploaded')"
                ),
                {
                    "a": file_a,
                    "ao": org_a,
                    "ka": f"organisations/{org_a}/documents/{file_a}/original",
                    "b": file_b,
                    "bo": org_b,
                    "kb": f"organisations/{org_b}/documents/{file_b}/original",
                },
            )
    finally:
        await engine.dispose()
    return org_a, org_b, file_a, file_b


@dataclass(frozen=True)
class NotificationIsolationSeed:
    """Identifiers for one user-private notifications isolation world.

    ``org_a`` holds two recipients (``user_a1``/``user_a2``) with one
    notification and delivery each; ``org_b`` holds one recipient
    (``user_b1``) with its own rows. That shape proves both boundaries the
    group-2 policies enforce: the organisation boundary and the recipient
    boundary inside a single organisation.
    """

    org_a: uuid.UUID
    org_b: uuid.UUID
    user_a1: uuid.UUID
    user_a2: uuid.UUID
    user_b1: uuid.UUID
    notification_a1: uuid.UUID
    notification_a2: uuid.UUID
    notification_b1: uuid.UUID
    delivery_a1: uuid.UUID
    delivery_a2: uuid.UUID
    delivery_b1: uuid.UUID


async def seed_two_organisation_notifications(
    owner_url: str,
) -> NotificationIsolationSeed:
    """Seed two organisations, three recipients and their notification rows.

    The owner credential is used deliberately: the notifications group is
    user-private, so a seed must be able to write rows the restricted runtime
    role is denied, and it must run with RLS bypassed the way a migration does.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    user_a1, user_a2, user_b1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    notification_a1, notification_a2, notification_b1 = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    delivery_a1, delivery_a2, delivery_b1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:a, :an), (:b, :bn)"),
                {"a": org_a, "an": "Notifications A", "b": org_b, "bn": "Notifications B"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, workos_user_id, email, name, is_active) "
                    "VALUES (:id, :workos, :email, :name, true)"
                ),
                [
                    {
                        "id": user_a1,
                        "workos": f"user_notif_a1_{uuid.uuid4().hex[:10]}",
                        "email": f"a1-{user_a1}@example.com",
                        "name": "A1",
                    },
                    {
                        "id": user_a2,
                        "workos": f"user_notif_a2_{uuid.uuid4().hex[:10]}",
                        "email": f"a2-{user_a2}@example.com",
                        "name": "A2",
                    },
                    {
                        "id": user_b1,
                        "workos": f"user_notif_b1_{uuid.uuid4().hex[:10]}",
                        "email": f"b1-{user_b1}@example.com",
                        "name": "B1",
                    },
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO notifications "
                    "(id, organisation_id, user_id, type, title, body) "
                    "VALUES (:id, :org, :user, 'notification.test_sent', :title, '')"
                ),
                [
                    {
                        "id": notification_a1,
                        "org": org_a,
                        "user": user_a1,
                        "title": "a1 notification",
                    },
                    {
                        "id": notification_a2,
                        "org": org_a,
                        "user": user_a2,
                        "title": "a2 notification",
                    },
                    {
                        "id": notification_b1,
                        "org": org_b,
                        "user": user_b1,
                        "title": "b1 notification",
                    },
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO notification_deliveries "
                    "(id, notification_id, channel, recipient, delivery_identity, status) "
                    "VALUES (:id, :notification, 'email', :recipient, :identity, 'queued')"
                ),
                [
                    {
                        "id": delivery_a1,
                        "notification": notification_a1,
                        "recipient": f"a1-{user_a1}@example.com",
                        "identity": str(uuid.uuid4()),
                    },
                    {
                        "id": delivery_a2,
                        "notification": notification_a2,
                        "recipient": f"a2-{user_a2}@example.com",
                        "identity": str(uuid.uuid4()),
                    },
                    {
                        "id": delivery_b1,
                        "notification": notification_b1,
                        "recipient": f"b1-{user_b1}@example.com",
                        "identity": str(uuid.uuid4()),
                    },
                ],
            )
    finally:
        await engine.dispose()
    return NotificationIsolationSeed(
        org_a=org_a,
        org_b=org_b,
        user_a1=user_a1,
        user_a2=user_a2,
        user_b1=user_b1,
        notification_a1=notification_a1,
        notification_a2=notification_a2,
        notification_b1=notification_b1,
        delivery_a1=delivery_a1,
        delivery_a2=delivery_a2,
        delivery_b1=delivery_b1,
    )


async def seed_representative_notifications(
    owner_url: str,
    *,
    organisations: int = 40,
    users_per_organisation: int = 5,
    rows_per_user: int = 10,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed a multi-tenant ``notifications`` table for the plan review.

    The user-private list plan is driven by the composite
    ``(organisation_id, user_id, created_at)`` index, so the table needs enough
    rows across organisations and recipients for the planner to prefer the
    index over a sequential scan. Returns organisation A, recipient A1 and one
    of A1's notification ids.
    """
    org_a = uuid.uuid4()
    user_a1 = uuid.uuid4()
    other_orgs = [uuid.uuid4() for _ in range(organisations - 1)]
    sample_id = uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:id, :name)"),
                [
                    {"id": org, "name": f"Notifications plan {i}"}
                    for i, org in enumerate([org_a, *other_orgs])
                ],
            )
            users: list[dict[str, object]] = []
            for index in range(users_per_organisation):
                user_id = user_a1 if index == 0 else uuid.uuid4()
                users.append(
                    {
                        "id": user_id,
                        "org": org_a,
                        "workos": f"user_notif_plan_{uuid.uuid4().hex[:12]}",
                        "email": f"plan-{user_id}@example.com",
                        "name": f"Plan user {index}",
                    }
                )
            for org_index, org in enumerate(other_orgs, start=1):
                user_id = uuid.uuid4()
                users.append(
                    {
                        "id": user_id,
                        "org": org,
                        "workos": f"user_notif_plan_{uuid.uuid4().hex[:12]}",
                        "email": f"plan-{user_id}@example.com",
                        "name": f"Other user {org_index}",
                    }
                )
            await connection.execute(
                text(
                    "INSERT INTO users (id, workos_user_id, email, name, is_active) "
                    "VALUES (:id, :workos, :email, :name, true)"
                ),
                users,
            )
            rows: list[dict[str, object]] = [
                {
                    "id": sample_id,
                    "org": org_a,
                    "user": user_a1,
                    "title": "sample",
                }
            ]
            rows.extend(
                {
                    "id": uuid.uuid4(),
                    "org": user["org"],
                    "user": user["id"],
                    "title": f"notification {index}",
                }
                for user in users
                for index in range(rows_per_user)
            )
            await connection.execute(
                text(
                    "INSERT INTO notifications "
                    "(id, organisation_id, user_id, type, title, body) "
                    "VALUES (:id, :org, :user, 'notification.test_sent', :title, '')"
                ),
                rows,
            )
            await connection.execute(text("ANALYZE notifications"))
    finally:
        await engine.dispose()
    return org_a, user_a1, sample_id


async def seed_representative_files(
    owner_url: str,
    *,
    organisations: int = 40,
    rows_per_organisation: int = 30,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a multi-tenant ``files`` table for the representative plan review.

    A two-row table cannot give a representative plan; with many organisations
    the indexed ``organisation_id``/``created_at`` path is the planner's real
    choice. Returns organisation A and one of its file ids.
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
                    {"id": org, "name": f"Files plan {i}"}
                    for i, org in enumerate([org_a, *other_orgs])
                ],
            )
            rows = [
                {"id": sample_id, "org": org_a, "name": "sample.pdf"},
                *(
                    {"id": uuid.uuid4(), "org": org, "name": f"file {index}.pdf"}
                    for org in [org_a, *other_orgs]
                    for index in range(rows_per_organisation)
                ),
            ]
            for row in rows:
                row["key"] = f"organisations/{row['org']}/documents/{row['id']}/original"
            await connection.execute(
                text(
                    "INSERT INTO files "
                    "(id, organisation_id, storage_provider, storage_bucket, object_key, "
                    "original_filename, content_type, size_bytes, status) "
                    "VALUES (:id, :org, 'fake', 'bucket', :key, "
                    ":name, 'application/pdf', 1, 'uploaded')"
                ),
                rows,
            )
            await connection.execute(text("ANALYZE files"))
    finally:
        await engine.dispose()
    return org_a, sample_id


async def seed_representative_records(
    owner_url: str,
    *,
    organisations: int = 40,
    rows_per_organisation: int = 30,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a multi-tenant ``records`` table and its revision ledger.

    A two-row table cannot give a representative plan; with many organisations
    the indexed ``organisation_id``/``created_at`` path is the planner's real
    choice. Each record also gets a small revision history so the ledger's
    ``record_id``/``created_at`` index can be reviewed. Returns organisation A
    and one of its record ids.
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
                    {"id": org, "name": f"Group plan {i}"}
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
            revisions = [
                {
                    "id": uuid.uuid4(),
                    "rid": row["id"],
                    "org": row["org"],
                    "version": version,
                }
                for row in rows
                for version in range(1, 4)
            ]
            await connection.execute(
                text(
                    "INSERT INTO record_revisions "
                    "(id, record_id, organisation_id, version, action, title, body) "
                    "VALUES (:id, :rid, :org, :version, 'updated', 'revision', '')"
                ),
                revisions,
            )
            await connection.execute(text("ANALYZE records"))
            await connection.execute(text("ANALYZE record_revisions"))
    finally:
        await engine.dispose()
    return org_a, sample_id


def force_drop_runtime_role(database_url: str) -> None:
    """Revoke grants and drop ``app_runtime`` if it exists (test cleanup)."""

    async def _run() -> None:
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

    asyncio.run(_run())


def upgrade_to_head() -> None:
    """Apply every migration to head."""
    command.upgrade(alembic_config(), "head")


def downgrade_to_base() -> None:
    """Revert every migration to base."""
    command.downgrade(alembic_config(), "base")


__all__ = [
    "BACKEND_ROOT",
    "METRICS_PASSWORD",
    "METRICS_ROLE",
    "RUNTIME_PASSWORD",
    "RUNTIME_ROLE",
    "NotificationIsolationSeed",
    "alembic_config",
    "database_reachable",
    "downgrade_to_base",
    "force_drop_runtime_role",
    "metrics_url",
    "provision_metrics_login",
    "provision_runtime_login",
    "runtime_engine",
    "runtime_url",
    "seed_representative_files",
    "seed_representative_notifications",
    "seed_representative_records",
    "seed_two_organisation_files",
    "seed_two_organisation_notifications",
    "seed_two_organisation_records",
    "upgrade_to_head",
]
