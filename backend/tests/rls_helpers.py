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
from datetime import UTC, datetime, timedelta
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

#: The group-4b non-bypass outbox-coordinator role (ADR-0022 decision 3). It is
#: created ``NOLOGIN`` by the group-4b migration; a focused test attaches a
#: throwaway credential to prove its dispatch-state policies are load-bearing.
COORDINATOR_ROLE = "app_coordinator"

#: Test-only throwaway credential for the coordinator role.
COORDINATOR_PASSWORD = "rls-coordinator-test-password"


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


@dataclass(frozen=True)
class AIIsolationSeed:
    """Identifiers for one two-organisation AI-data isolation world.

    Each organisation owns one row in every group-3 table: an ``ai_requests``
    attempt, its ``ai_outputs`` result, an ``ai_attachment_references`` live
    transfer and an ``ai_scratch_uploads`` intent. The shape proves the single
    organisation boundary the four policies enforce.
    """

    org_a: uuid.UUID
    org_b: uuid.UUID
    request_a: uuid.UUID
    request_b: uuid.UUID
    request_id_a: str
    request_id_b: str
    output_a: uuid.UUID
    output_b: uuid.UUID
    reference_a: uuid.UUID
    reference_b: uuid.UUID
    scratch_a: uuid.UUID
    scratch_b: uuid.UUID


async def seed_two_organisation_ai(owner_url: str) -> AIIsolationSeed:
    """Seed two organisations and one AI row each across the group-3 tables.

    The owner credential is used deliberately: the AI-data tables are direct
    organisation-owned tables, so a seed must be able to write rows the
    restricted runtime role is denied, and it must run with RLS bypassed the
    way a migration does.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    request_a, request_b = uuid.uuid4(), uuid.uuid4()
    request_id_a, request_id_b = uuid.uuid4().hex, uuid.uuid4().hex
    output_a, output_b = uuid.uuid4(), uuid.uuid4()
    reference_a, reference_b = uuid.uuid4(), uuid.uuid4()
    scratch_a, scratch_b = uuid.uuid4(), uuid.uuid4()
    digest_a, digest_b = "a1" * 32, "b2" * 32
    key_a, key_b = "c3" * 32, "d4" * 32
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:a, :an), (:b, :bn)"),
                {"a": org_a, "an": "AI Group A", "b": org_b, "bn": "AI Group B"},
            )
            await connection.execute(
                text(
                    "INSERT INTO ai_requests "
                    "(id, organisation_id, request_id, attempt_number, task, provider, model, "
                    "prompt_name, prompt_version, routing_reason, region, status, input_tokens, "
                    "output_tokens, latency_ms) "
                    "VALUES (:id, :org, :request_id, 1, 'document.classify', 'fake', "
                    "'fake.document-classifier', 'document.classify', 1, 'seeded', '', 'failed', "
                    "0, 0, 0)"
                ),
                [
                    {"id": request_a, "org": org_a, "request_id": request_id_a},
                    {"id": request_b, "org": org_b, "request_id": request_id_b},
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO ai_outputs (id, ai_request_id, organisation_id, output_json) "
                    "VALUES (:id, :request, :org, '{\"seeded\": true}'::jsonb)"
                ),
                [
                    {"id": output_a, "request": request_a, "org": org_a},
                    {"id": output_b, "request": request_b, "org": org_b},
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO ai_attachment_references "
                    "(id, organisation_id, logical_request_id, provider, transfer_mode, "
                    "external_id, source_reference, source_digest, size_bytes, mime_type, "
                    "source_lifecycle, region, status, idempotency_key) "
                    "VALUES (:id, :org, :logical, 'fake', 'provider_upload', :external, "
                    ":source_reference, :digest, 1600, 'application/pdf', 'transient', "
                    "'eu-west-1', 'live', :key)"
                ),
                [
                    {
                        "id": reference_a,
                        "org": org_a,
                        "logical": "rls-ai-logical-a",
                        "external": f"fake-a-{uuid.uuid4().hex[:12]}",
                        "source_reference": f"organisations/{org_a}/documents/x/original",
                        "digest": digest_a,
                        "key": key_a,
                    },
                    {
                        "id": reference_b,
                        "org": org_b,
                        "logical": "rls-ai-logical-b",
                        "external": f"fake-b-{uuid.uuid4().hex[:12]}",
                        "source_reference": f"organisations/{org_b}/documents/x/original",
                        "digest": digest_b,
                        "key": key_b,
                    },
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO ai_scratch_uploads "
                    "(id, organisation_id, upload_id, object_key, content_type, "
                    "size_bytes, status, expires_at) "
                    "VALUES (:id, :org, :upload_id, :key, 'application/pdf', 1024, "
                    "'pending', :expires)"
                ),
                [
                    {
                        "id": scratch_a,
                        "org": org_a,
                        "upload_id": uuid.uuid4(),
                        "key": f"organisations/{org_a}/ai/scratch/{uuid.uuid4()}.pdf",
                        "expires": expires_at,
                    },
                    {
                        "id": scratch_b,
                        "org": org_b,
                        "upload_id": uuid.uuid4(),
                        "key": f"organisations/{org_b}/ai/scratch/{uuid.uuid4()}.pdf",
                        "expires": expires_at,
                    },
                ],
            )
    finally:
        await engine.dispose()
    return AIIsolationSeed(
        org_a=org_a,
        org_b=org_b,
        request_a=request_a,
        request_b=request_b,
        request_id_a=request_id_a,
        request_id_b=request_id_b,
        output_a=output_a,
        output_b=output_b,
        reference_a=reference_a,
        reference_b=reference_b,
        scratch_a=scratch_a,
        scratch_b=scratch_b,
    )


@dataclass(frozen=True)
class SettingsIsolationSeed:
    """Identifiers for one two-organisation organisation-settings world.

    Each organisation owns one ``organisation_features`` override and one
    ``organisation_ai_settings`` policy row. ``org_c`` is an organisation with
    no settings rows, used for the same-tenant insert and mismatched-insert
    proofs (both settings tables have a per-organisation uniqueness invariant,
    so an existing organisation cannot receive a second row).
    """

    org_a: uuid.UUID
    org_b: uuid.UUID
    org_c: uuid.UUID
    feature_a: uuid.UUID
    feature_b: uuid.UUID
    ai_settings_a: uuid.UUID
    ai_settings_b: uuid.UUID


async def seed_two_organisation_settings(owner_url: str) -> SettingsIsolationSeed:
    """Seed three organisations and their group-4a settings rows (owner role).

    The owner credential is used deliberately: the settings tables are direct
    organisation-owned tables, so a seed must be able to write rows the
    restricted runtime role is denied, and it must run with RLS bypassed the
    way a migration does.
    """
    org_a, org_b, org_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    feature_a, feature_b = uuid.uuid4(), uuid.uuid4()
    ai_settings_a, ai_settings_b = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:a, :an), (:b, :bn), (:c, :cn)"),
                {
                    "a": org_a,
                    "an": "Settings A",
                    "b": org_b,
                    "bn": "Settings B",
                    "c": org_c,
                    "cn": "Settings C",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO organisation_features "
                    "(id, organisation_id, feature_key, enabled) "
                    "VALUES (:id, :org, 'records.deletion', true)"
                ),
                [
                    {"id": feature_a, "org": org_a},
                    {"id": feature_b, "org": org_b},
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO organisation_ai_settings (id, organisation_id, enabled) "
                    "VALUES (:id, :org, false)"
                ),
                [
                    {"id": ai_settings_a, "org": org_a},
                    {"id": ai_settings_b, "org": org_b},
                ],
            )
    finally:
        await engine.dispose()
    return SettingsIsolationSeed(
        org_a=org_a,
        org_b=org_b,
        org_c=org_c,
        feature_a=feature_a,
        feature_b=feature_b,
        ai_settings_a=ai_settings_a,
        ai_settings_b=ai_settings_b,
    )


async def seed_representative_ai(
    owner_url: str,
    *,
    organisations: int = 40,
    rows_per_organisation: int = 30,
) -> tuple[uuid.UUID, str]:
    """Seed a multi-tenant ``ai_requests``/``ai_outputs`` pair for plan review.

    A two-row table cannot give a representative plan; with many organisations
    the indexed ``organisation_id``/``request_id`` path is the planner's real
    choice. Returns organisation A and one of its request ids.
    """
    org_a = uuid.uuid4()
    other_orgs = [uuid.uuid4() for _ in range(organisations - 1)]
    sample_request_id = uuid.uuid4().hex
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:id, :name)"),
                [{"id": org, "name": f"AI plan {i}"} for i, org in enumerate([org_a, *other_orgs])],
            )
            requests: list[dict[str, object]] = []
            for org in [org_a, *other_orgs]:
                for index in range(rows_per_organisation):
                    requests.append(
                        {
                            "id": uuid.uuid4(),
                            "org": org,
                            "request_id": (
                                sample_request_id
                                if org == org_a and index == 0
                                else uuid.uuid4().hex
                            ),
                        }
                    )
            await connection.execute(
                text(
                    "INSERT INTO ai_requests "
                    "(id, organisation_id, request_id, attempt_number, task, provider, model, "
                    "prompt_name, prompt_version, routing_reason, region, status, input_tokens, "
                    "output_tokens, latency_ms) "
                    "VALUES (:id, :org, :request_id, 1, 'document.classify', 'fake', "
                    "'fake.document-classifier', 'document.classify', 1, 'plan', '', 'succeeded', "
                    "0, 0, 0)"
                ),
                requests,
            )
            await connection.execute(
                text(
                    "INSERT INTO ai_outputs (id, ai_request_id, organisation_id, output_json) "
                    "VALUES (:id, :request, :org, '{\"plan\": true}'::jsonb)"
                ),
                [{"id": uuid.uuid4(), "request": row["id"], "org": row["org"]} for row in requests],
            )
            await connection.execute(text("ANALYZE ai_requests"))
            await connection.execute(text("ANALYZE ai_outputs"))
    finally:
        await engine.dispose()
    return org_a, sample_request_id


async def seed_representative_settings(
    owner_url: str,
    *,
    organisations: int = 2_000,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed a multi-tenant organisation-settings world for the plan review.

    ``organisation_features`` and ``organisation_ai_settings`` are one-row-per-
    organisation configuration tables, but their **total** cardinality grows
    with the organisation count, so the rollout-principle-4 plan review still
    needs a realistically sized table: a two-row seed cannot prove the planner
    prefers the per-organisation index. Every organisation gets one feature
    override and one AI-settings row. Returns organisation A plus one feature
    row id and one AI-settings row id belonging to it.
    """
    org_a = uuid.uuid4()
    other_orgs = [uuid.uuid4() for _ in range(organisations - 1)]
    feature_a = uuid.uuid4()
    ai_settings_a = uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:id, :name)"),
                [
                    {"id": org, "name": f"Settings plan {i}"}
                    for i, org in enumerate([org_a, *other_orgs])
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO organisation_features "
                    "(id, organisation_id, feature_key, enabled) "
                    "VALUES (:id, :org, 'records.deletion', true)"
                ),
                [
                    {"id": feature_a if org == org_a else uuid.uuid4(), "org": org}
                    for org in [org_a, *other_orgs]
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO organisation_ai_settings (id, organisation_id, enabled) "
                    "VALUES (:id, :org, false)"
                ),
                [
                    {"id": ai_settings_a if org == org_a else uuid.uuid4(), "org": org}
                    for org in [org_a, *other_orgs]
                ],
            )
            await connection.execute(text("ANALYZE organisation_features"))
            await connection.execute(text("ANALYZE organisation_ai_settings"))
    finally:
        await engine.dispose()
    return org_a, feature_a, ai_settings_a


@dataclass(frozen=True)
class JobsIsolationSeed:
    """Identifiers for one two-organisation jobs/attempts isolation world.

    Each organisation owns one dispatched job and one open attempt on it. The
    job carries a ``dispatch_id`` and ``owner_token`` so it satisfies both the
    runtime organisation policy and the coordinator dispatch-state policies.
    """

    org_a: uuid.UUID
    org_b: uuid.UUID
    job_a: uuid.UUID
    job_b: uuid.UUID
    attempt_a: uuid.UUID
    attempt_b: uuid.UUID


async def seed_two_organisation_jobs(owner_url: str) -> JobsIsolationSeed:
    """Seed two organisations, one job and one open attempt each (owner role).

    The owner credential is used deliberately: the jobs group is direct tenant
    data, so a seed must be able to write foreign rows the restricted runtime
    role is denied, and it must run with RLS bypassed the way a migration does.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    job_a, job_b = uuid.uuid4(), uuid.uuid4()
    dispatch_a, dispatch_b = uuid.uuid4(), uuid.uuid4()
    attempt_a, attempt_b = uuid.uuid4(), uuid.uuid4()
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:a, :an), (:b, :bn)"),
                {"a": org_a, "an": "Jobs Group A", "b": org_b, "bn": "Jobs Group B"},
            )
            await connection.execute(
                text(
                    "INSERT INTO jobs "
                    "(id, organisation_id, job_type, status, progress, input_reference, "
                    "dispatch_id, owner_token) "
                    "VALUES (:id, :org, 'file.processing', 'running', 0, 'ref', :dispatch, :owner)"
                ),
                [
                    {
                        "id": job_a,
                        "org": org_a,
                        "dispatch": dispatch_a,
                        "owner": uuid.uuid4(),
                    },
                    {
                        "id": job_b,
                        "org": org_b,
                        "dispatch": dispatch_b,
                        "owner": uuid.uuid4(),
                    },
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO job_attempts "
                    "(id, job_id, organisation_id, dispatch_id, owner_token, "
                    "attempt_number, status, lease_expires_at, started_at, taken_over) "
                    "VALUES (:id, :job, :org, :dispatch, :owner, 1, 'running', "
                    "now() + interval '1 hour', now(), false)"
                ),
                [
                    {
                        "id": attempt_a,
                        "job": job_a,
                        "org": org_a,
                        "dispatch": dispatch_a,
                        "owner": uuid.uuid4(),
                    },
                    {
                        "id": attempt_b,
                        "job": job_b,
                        "org": org_b,
                        "dispatch": dispatch_b,
                        "owner": uuid.uuid4(),
                    },
                ],
            )
    finally:
        await engine.dispose()
    return JobsIsolationSeed(
        org_a=org_a,
        org_b=org_b,
        job_a=job_a,
        job_b=job_b,
        attempt_a=attempt_a,
        attempt_b=attempt_b,
    )


async def seed_representative_jobs(
    owner_url: str,
    *,
    organisations: int = 40,
    rows_per_organisation: int = 30,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a multi-tenant ``jobs`` table for the representative plan review.

    The org-scoped list is driven by the ``(organisation_id, created_at)``
    index, so the table needs enough rows across organisations for the planner
    to prefer the index over a sequential scan. Returns organisation A and one
    of its job ids.
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
                    {"id": org, "name": f"Jobs plan {i}"}
                    for i, org in enumerate([org_a, *other_orgs])
                ],
            )
            rows = [{"id": sample_id, "org": org_a}]
            for org in [org_a, *other_orgs]:
                rows.extend({"id": uuid.uuid4(), "org": org} for _ in range(rows_per_organisation))
            await connection.execute(
                text(
                    "INSERT INTO jobs "
                    "(id, organisation_id, job_type, status, progress, input_reference) "
                    "VALUES (:id, :org, 'file.processing', 'succeeded', 100, 'ref')"
                ),
                rows,
            )
            await connection.execute(text("ANALYZE jobs"))
    finally:
        await engine.dispose()
    return org_a, sample_id


def provision_coordinator_login(owner_url: str) -> None:
    """Grant the coordinator role a throwaway login credential (idempotent)."""

    async def _run() -> None:
        engine = create_async_engine(owner_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f"ALTER ROLE {COORDINATOR_ROLE} LOGIN PASSWORD '{COORDINATOR_PASSWORD}'")
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def coordinator_url(owner_url: str) -> str:
    """Return the test database URL for the restricted coordinator login."""
    return (
        make_url(owner_url)
        .set(username=COORDINATOR_ROLE, password=COORDINATOR_PASSWORD)
        .render_as_string(hide_password=False)
    )


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
    "COORDINATOR_PASSWORD",
    "COORDINATOR_ROLE",
    "METRICS_PASSWORD",
    "METRICS_ROLE",
    "RUNTIME_PASSWORD",
    "RUNTIME_ROLE",
    "AIIsolationSeed",
    "JobsIsolationSeed",
    "NotificationIsolationSeed",
    "SettingsIsolationSeed",
    "alembic_config",
    "coordinator_url",
    "database_reachable",
    "downgrade_to_base",
    "force_drop_runtime_role",
    "metrics_url",
    "provision_coordinator_login",
    "provision_metrics_login",
    "provision_runtime_login",
    "runtime_engine",
    "runtime_url",
    "seed_representative_ai",
    "seed_representative_files",
    "seed_representative_jobs",
    "seed_representative_notifications",
    "seed_representative_records",
    "seed_representative_settings",
    "seed_two_organisation_ai",
    "seed_two_organisation_files",
    "seed_two_organisation_jobs",
    "seed_two_organisation_notifications",
    "seed_two_organisation_records",
    "seed_two_organisation_settings",
    "upgrade_to_head",
]
