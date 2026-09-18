"""Real-PostgreSQL two-organisation isolation matrix (plan P2).

This is the plan P2 acceptance suite: the application-level organisation
boundary is proven against a real migrated PostgreSQL with the real ASGI stack,
real permission checks and real org-scoped queries. ``org_isolation_helpers.py``
seeds the required world (organisations A and B, an A-only user, a B-only user,
a user who is owner in A and viewer in B, a suspended membership and a
platform-only user) and wires the migrated database into the app.

Coverage, mapping to the P2 checklist:

- list/detail/create/update/delete/action routes with valid foreign ids;
- indirect paths exercised through their real org-scoped parent/service
  boundaries (files/jobs, notifications/deliveries, AI requests/references and
  scratch uploads);
- pagination totals and filters that never include foreign rows;
- downloads that never resolve a foreign object;
- role authority that stays inside one organisation, including for the
  multi-membership user;
- the platform plane granting no tenant-data access and organisation roles
  granting no platform access;
- a deliberate-omission demonstration proving the matrix guard would fail if a
  representative organisation predicate were dropped.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.auth_helpers import generate_key_pair
from tests.org_isolation_helpers import (
    FORBIDDEN,
    NOT_FOUND,
    Identity,
    IsolationWorld,
    assert_org_scoped_row,
    auth_headers,
    build_isolation_app,
    fetch_record_without_org_predicate,
    seed_isolation_world,
)
from tests.test_security_suite import PROTECTED_ROUTES, iter_http_routes

from app.ai.persistence.references import SQLTransferReferenceStore
from app.ai.transfer import TransferMode
from app.main import create_app
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatus
from app.modules.notifications import tasks as notifications_tasks
from app.modules.notifications.models import NotificationDelivery, NotificationDeliveryStatus

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Routes this matrix exercises, tied back to the mandatory protected-route table
# so a new endpoint cannot silently bypass the org-isolation work unit.
_EXERCISED_ROUTES: set[tuple[str, str]] = {
    ("GET", "/api/v1/me"),
    ("GET", "/api/v1/records"),
    ("POST", "/api/v1/records"),
    ("GET", "/api/v1/records/{record_id}"),
    ("PATCH", "/api/v1/records/{record_id}"),
    ("DELETE", "/api/v1/records/{record_id}"),
    ("GET", "/api/v1/files"),
    ("POST", "/api/v1/files"),
    ("GET", "/api/v1/files/{file_id}"),
    ("GET", "/api/v1/files/{file_id}/download-url"),
    ("DELETE", "/api/v1/files/{file_id}"),
    ("POST", "/api/v1/files/{file_id}/complete"),
    ("GET", "/api/v1/jobs"),
    ("GET", "/api/v1/jobs/{job_id}"),
    ("GET", "/api/v1/notifications"),
    ("GET", "/api/v1/notifications/unread-count"),
    ("PATCH", "/api/v1/notifications/{notification_id}/read"),
    ("PATCH", "/api/v1/notifications/read-all"),
    ("POST", "/api/v1/notifications/test"),
    ("GET", "/api/v1/ai/classify/requests/{request_id}"),
    ("POST", "/api/v1/ai/classify"),
    ("POST", "/api/v1/ai/ask"),
    ("POST", "/api/v1/ai/scratch/uploads"),
    ("POST", "/api/v1/ai/scratch/uploads/{upload_id}/complete"),
    ("GET", "/api/v1/platform/organisations"),
}


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
    """One module-local RSA key for minting the matrix's WorkOS-style tokens."""
    key, _ = generate_key_pair()
    return key


@pytest.fixture
def world(migrated_database: str) -> IsolationWorld:
    """Seed a fresh two-organisation world for each test (no cross-test bleed)."""
    return asyncio.run(seed_isolation_world(migrated_database))


@pytest.fixture
async def app(migrated_database: str, private_key: rsa.RSAPrivateKey) -> AsyncIterator[FastAPI]:
    """Build the real app per test and deterministically dispose its engine.

    ``build_isolation_app`` wires a per-test NullPool engine into the app;
    ``httpx.ASGITransport`` does not run the app lifespan, so this fixture's
    teardown hook disposes the engine rather than relying on a lifespan event.
    """
    built = build_isolation_app(migrated_database, private_key)
    try:
        yield built
    finally:
        await built.state.isolation_engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as async_client:
        yield async_client


@pytest.fixture
async def task_session_factory(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Point the notification worker's session factories at a NullPool engine.

    The email task opens its own sessions through ``async_session_factory`` and
    drives its domain work through ``jobs.execution``; both module-level
    singletons pool connections across event loops, so this fixture binds them
    to a NullPool engine on the test's loop (mirroring ``test_notifications_db``).
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(notifications_tasks, "async_session_factory", factory)
    from app.modules.jobs import execution as jobs_execution

    monkeypatch.setattr(jobs_execution, "async_session_factory", factory)
    yield factory
    await engine.dispose()


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


def _assert_error(response: Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert body["request_id"]


async def _org_ai_side_effect_counts(
    database_url: str, organisation_id: uuid.UUID
) -> tuple[int, int, int]:
    """Count the durable rows a rejected AI request could have created."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            jobs = await connection.scalar(
                text("SELECT count(*) FROM jobs WHERE organisation_id = CAST(:org AS uuid)"),
                {"org": str(organisation_id)},
            )
            requests = await connection.scalar(
                text("SELECT count(*) FROM ai_requests WHERE organisation_id = CAST(:org AS uuid)"),
                {"org": str(organisation_id)},
            )
            references = await connection.scalar(
                text(
                    "SELECT count(*) FROM ai_attachment_references "
                    "WHERE organisation_id = CAST(:org AS uuid)"
                ),
                {"org": str(organisation_id)},
            )
        return int(jobs), int(requests), int(references)
    finally:
        await engine.dispose()


# --- Route coverage tie-in (plan P2: PROTECTED_ROUTES stays complete) -------


def test_exercised_routes_are_all_in_the_protected_route_table() -> None:
    """Every route the matrix exercises is registered in the mandatory table."""
    protected = {(spec.method, spec.path) for spec in PROTECTED_ROUTES}
    assert protected >= _EXERCISED_ROUTES


#: The indirect tables (registry class ``INDIRECT``) and the real route prefix
#: that is their only API entry point. The mapping is explicit so a new
#: direct route such as ``/api/v1/job-attempts`` cannot slip through.
_INDIRECT_PARENT_ROUTES: dict[str, str] = {
    "job_attempts": "/api/v1/jobs",
    "notification_deliveries": "/api/v1/notifications",
    "membership_roles": "/api/v1/me",
}


def test_indirect_tables_have_no_direct_api_surface() -> None:
    """Indirect tables are reachable only through their org-scoped parent.

    This inspects the real FastAPI route surface (not a joined string of route
    templates) and uses an explicit resource-to-parent mapping: every indirect
    table must have a live parent route, and no route may expose the table as a
    path segment (underscored or hyphenated), so a direct
    ``/api/v1/job-attempts`` surface fails here. The reachability of each
    indirect path through its parent's organisation check is exercised by the
    reference-store and notification-worker tests below.
    """
    paths = {route.path for route in iter_http_routes(create_app())}
    segments = {segment for path in paths for segment in path.split("/")}
    for indirect_table, parent_prefix in _INDIRECT_PARENT_ROUTES.items():
        assert any(path.startswith(parent_prefix) for path in paths), (
            f"{indirect_table} must be reachable through {parent_prefix}"
        )
        assert indirect_table not in segments, f"{indirect_table} must not have a direct route"
        assert indirect_table.replace("_", "-") not in segments, (
            f"{indirect_table} must not have a direct hyphenated route"
        )


# --- Direct tenant routes: own id works, foreign id is not found ------------


async def test_owner_reads_and_lists_only_own_org_records(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    own = await _call(
        client,
        "GET",
        f"/api/v1/records/{world.record_a}",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_a,
    )
    assert own.status_code == 200
    assert own.json()["title"] == "A record"

    listing = await _call(
        client,
        "GET",
        "/api/v1/records",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_a,
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert [item["title"] for item in body["items"]] == ["A record"]


async def test_cross_org_record_detail_update_and_delete_are_not_found(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    detail = await _call(
        client,
        "GET",
        f"/api/v1/records/{world.record_a}",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    _assert_error(detail, NOT_FOUND, "record_not_found")

    update = await _call(
        client,
        "PATCH",
        f"/api/v1/records/{world.record_a}",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
        json={"version": 1, "title": "Hijacked"},
    )
    _assert_error(update, NOT_FOUND, "record_not_found")

    delete = await _call(
        client,
        "DELETE",
        f"/api/v1/records/{world.record_a}",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
        params={"version": 1},
    )
    _assert_error(delete, NOT_FOUND, "record_not_found")


async def test_cross_org_file_detail_download_and_delete_are_not_found(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    detail = await _call(
        client,
        "GET",
        f"/api/v1/files/{world.file_a}",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    _assert_error(detail, NOT_FOUND, "file_not_found")

    download = await _call(
        client,
        "GET",
        f"/api/v1/files/{world.file_a}/download-url",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    _assert_error(download, NOT_FOUND, "file_not_found")

    delete = await _call(
        client,
        "DELETE",
        f"/api/v1/files/{world.file_a}",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    _assert_error(delete, NOT_FOUND, "file_not_found")

    complete = await _call(
        client,
        "POST",
        f"/api/v1/files/{world.file_a}/complete",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    _assert_error(complete, NOT_FOUND, "file_not_found")


async def test_owner_can_create_a_file_upload_intent_in_own_org(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "POST",
        "/api/v1/files",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_a,
        json={
            "original_filename": "lease.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1024,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["file_id"]
    # The signed PUT target is minted under the caller's organisation only.
    assert f"organisations/{world.org_a}/" in body["upload_url"]


async def test_viewer_in_b_cannot_create_a_file_upload_intent(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "POST",
        "/api/v1/files",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_b,
        json={
            "original_filename": "lease.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1024,
        },
    )
    _assert_error(response, FORBIDDEN, "permission_denied")


async def test_cross_org_job_detail_is_not_found(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "GET",
        f"/api/v1/jobs/{world.job_a}",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    _assert_error(response, NOT_FOUND, "job_not_found")


async def test_list_totals_and_filters_do_not_include_foreign_rows(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    jobs = await _call(
        client,
        "GET",
        "/api/v1/jobs",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_a,
    )
    assert jobs.status_code == 200
    assert jobs.json()["total"] == 1

    files = await _call(
        client,
        "GET",
        "/api/v1/files",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_a,
        params={"status": "uploaded"},
    )
    assert files.status_code == 200
    assert files.json()["total"] == 1

    # The same filter under B sees only B's file; foreign rows never surface.
    files_b = await _call(
        client,
        "GET",
        "/api/v1/files",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
        params={"status": "uploaded"},
    )
    assert files_b.status_code == 200
    assert files_b.json()["total"] == 1


# --- User-private rows: recipient scoping inside one tenant -----------------


async def test_other_users_notification_is_not_found(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    # The multi user is an owner in A, but the seeded notification belongs to
    # the A-only user; recipient scoping makes it a 404.
    response = await _call(
        client,
        "PATCH",
        f"/api/v1/notifications/{world.notification_a}/read",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_a,
    )
    _assert_error(response, NOT_FOUND, "notification_not_found")


async def test_cross_org_notification_is_not_found(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "PATCH",
        f"/api/v1/notifications/{world.notification_a}/read",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    _assert_error(response, NOT_FOUND, "notification_not_found")


async def test_notification_listing_and_unread_count_are_recipient_scoped(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    listing = await _call(
        client,
        "GET",
        "/api/v1/notifications",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_a,
    )
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    other_user = await _call(
        client,
        "GET",
        "/api/v1/notifications/unread-count",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_a,
    )
    assert other_user.status_code == 200
    assert other_user.json()["unread_count"] == 0

    # read-all is recipient-scoped too: another A member marks nothing.
    marked = await _call(
        client,
        "PATCH",
        "/api/v1/notifications/read-all",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_a,
    )
    assert marked.status_code == 200
    assert marked.json()["marked_count"] == 0


async def test_viewer_cannot_send_a_test_notification(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "POST",
        "/api/v1/notifications/test",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_b,
    )
    _assert_error(response, FORBIDDEN, "permission_denied")


async def test_owner_can_send_a_test_notification(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "POST",
        "/api/v1/notifications/test",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_a,
    )
    assert response.status_code == 201, response.text
    # The notification is created for the caller, never another recipient.
    assert response.json()["title"]


# --- Indirect notification-delivery path ------------------------------------


async def test_notification_delivery_worker_enforces_the_parent_org_boundary(
    migrated_database: str,
    world: IsolationWorld,
    task_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery is reachable only through its org-scoped parent notification.

    ``notification_deliveries`` has no direct route; the durable email worker is
    the only reader. This drives the real worker task with a job owned by
    organisation B whose ``input_reference`` is organisation A's seeded delivery
    and proves the parent's organisation check fails the job permanently before
    any provider call: the delivery stays queued and no email is sent.
    """
    provider_called = False

    class _ProviderMustNotRun:
        async def send_email(self, **kwargs: object) -> object:
            nonlocal provider_called
            provider_called = True
            raise AssertionError("provider must not run for a mismatched tenant context")

    monkeypatch.setattr(notifications_tasks, "get_email_provider", lambda: _ProviderMustNotRun())
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            job = await jobs_service.schedule_job(
                session,
                organisation_id=world.org_b,
                job_type=notifications_tasks.JOB_TYPE_NOTIFICATION_EMAIL,
                input_reference=str(world.delivery_a),
                actor_user_id=world.b_owner.user_id,
            )
            job_id = job.id

        with pytest.raises(jobs_service.JobPermanentError):
            await notifications_tasks.send_notification_email(str(job_id))

        assert provider_called is False
        async with session_factory() as session:
            delivery = await session.get(NotificationDelivery, world.delivery_a)
            assert delivery is not None
            assert delivery.status == NotificationDeliveryStatus.QUEUED
            assert delivery.attempt_count == 0
            failed_job = await session.get(Job, job_id)
            assert failed_job is not None
            assert failed_job.status == JobStatus.FAILED
            assert failed_job.error_code == notifications_tasks.ERROR_CODE_INVALID_JOB_CONTEXT
    finally:
        await engine.dispose()


# --- Indirect AI paths ------------------------------------------------------


async def test_ai_result_is_org_scoped(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    own = await _call(
        client,
        "GET",
        f"/api/v1/ai/classify/requests/{world.ai_request_a}",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_a,
    )
    assert own.status_code == 200
    assert own.json()["request_id"] == world.ai_request_a

    foreign = await _call(
        client,
        "GET",
        f"/api/v1/ai/classify/requests/{world.ai_request_a}",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    _assert_error(foreign, NOT_FOUND, "ai_request_not_found")


async def test_foreign_scratch_upload_completion_is_indistinguishable_from_missing(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    foreign = await _call(
        client,
        "POST",
        f"/api/v1/ai/scratch/uploads/{world.scratch_upload_a}/complete",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    missing = await _call(
        client,
        "POST",
        f"/api/v1/ai/scratch/uploads/{uuid.uuid4()}/complete",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
    )
    _assert_error(foreign, 422, "upload_not_found")
    foreign_body = {k: v for k, v in foreign.json().items() if k != "request_id"}
    missing_body = {k: v for k, v in missing.json().items() if k != "request_id"}
    assert foreign_body == missing_body


async def test_owner_can_start_scratch_upload_within_own_org(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "POST",
        "/api/v1/ai/scratch/uploads",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_a,
        json={
            "original_filename": "lease.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1024,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["upload_id"]
    # The signed URL is minted for the caller's own scratch namespace only.
    assert f"organisations/{world.org_a}/" in body["upload_url"]


async def test_cross_org_ai_classify_reference_is_denied_without_side_effects(
    client: AsyncClient,
    world: IsolationWorld,
    private_key: rsa.RSAPrivateKey,
    migrated_database: str,
) -> None:
    """B cannot classify with A's private storage reference.

    The organisation-A reference is denied exactly like an unauthorised
    reference (same safe validation error, no cross-organisation distinction),
    and no B job, AI request or transfer reference is created — the check runs
    before any provider work.
    """
    foreign_reference = f"organisations/{world.org_a}/documents/{world.file_a}/original"
    before = await _org_ai_side_effect_counts(migrated_database, world.org_b)
    foreign = await _call(
        client,
        "POST",
        "/api/v1/ai/classify",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
        json={"storage_reference": foreign_reference, "sync": False},
    )
    unauthorised = await _call(
        client,
        "POST",
        "/api/v1/ai/classify",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
        json={"storage_reference": "not-an-organisation-reference", "sync": False},
    )
    _assert_error(foreign, 422, "invalid_storage_reference")
    _assert_error(unauthorised, 422, "invalid_storage_reference")
    assert {k: v for k, v in foreign.json().items() if k != "request_id"} == {
        k: v for k, v in unauthorised.json().items() if k != "request_id"
    }
    assert await _org_ai_side_effect_counts(migrated_database, world.org_b) == before


async def test_cross_org_ai_ask_reference_is_denied_without_side_effects(
    client: AsyncClient,
    world: IsolationWorld,
    private_key: rsa.RSAPrivateKey,
    migrated_database: str,
) -> None:
    """B cannot ask about a document through A's private storage reference."""
    foreign_reference = f"organisations/{world.org_a}/documents/{world.file_a}/original"
    before = await _org_ai_side_effect_counts(migrated_database, world.org_b)
    foreign = await _call(
        client,
        "POST",
        "/api/v1/ai/ask",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
        json={"storage_reference": foreign_reference, "question": "What is this document?"},
    )
    unauthorised = await _call(
        client,
        "POST",
        "/api/v1/ai/ask",
        private_key=private_key,
        identity=world.b_owner,
        org_id=world.org_b,
        json={"storage_reference": "not-an-organisation-reference", "question": "What is this?"},
    )
    _assert_error(foreign, 422, "invalid_storage_reference")
    _assert_error(unauthorised, 422, "invalid_storage_reference")
    assert {k: v for k, v in foreign.json().items() if k != "request_id"} == {
        k: v for k, v in unauthorised.json().items() if k != "request_id"
    }
    assert await _org_ai_side_effect_counts(migrated_database, world.org_b) == before


async def test_ai_attachment_references_are_org_scoped(
    migrated_database: str, world: IsolationWorld
) -> None:
    """The real transfer-reference store can never reach a foreign organisation.

    ``ai_attachment_references`` is read only through the production
    organisation-scoped :class:`SQLTransferReferenceStore` (the AI execution and
    reconciliation seam): listing, reuse and deletion for organisation B cannot
    see organisation A's seeded live row, and A's row is left untouched.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            store = SQLTransferReferenceStore(session)
            own = await store.list_for_request(
                organisation_id=world.org_a, logical_request_id=world.reference_request_a
            )
            assert len(own) == 1
            assert_org_scoped_row(own[0].organisation_id, world.org_a)
            assert own[0].idempotency_key == world.reference_key_a

            assert (
                await store.list_for_request(
                    organisation_id=world.org_b, logical_request_id=world.reference_request_a
                )
                == []
            )
            assert (
                await store.find_live(
                    organisation_id=world.org_b,
                    logical_request_id=world.reference_request_a,
                    provider_id="fake",
                    mode=TransferMode.PROVIDER_UPLOAD,
                    source_digest=world.reference_digest_a,
                    region="eu-west-1",
                )
                is None
            )
            assert (
                await store.claim_for_deletion(
                    organisation_id=world.org_b, idempotency_key=world.reference_key_a
                )
                is None
            )

            untouched = await store.list_for_request(
                organisation_id=world.org_a, logical_request_id=world.reference_request_a
            )
            assert len(untouched) == 1
            assert untouched[0].status.value == "live"
    finally:
        await engine.dispose()


# --- Role matrix: authority inside one organisation only --------------------


async def test_multi_membership_user_owner_in_a_can_create_in_a(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "POST",
        "/api/v1/records",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_a,
        json={"title": "Created in A", "body": ""},
    )
    assert response.status_code == 201, response.text
    assert response.json()["title"] == "Created in A"


async def test_multi_membership_user_viewer_in_b_cannot_create_in_b(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "POST",
        "/api/v1/records",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_b,
        json={"title": "Should be denied", "body": ""},
    )
    _assert_error(response, FORBIDDEN, "permission_denied")


async def test_multi_membership_user_viewer_in_b_can_read_b(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "GET",
        f"/api/v1/records/{world.record_b}",
        private_key=private_key,
        identity=world.multi,
        org_id=world.org_b,
    )
    assert response.status_code == 200
    assert response.json()["title"] == "B record"


async def test_per_membership_roles_do_not_cross_organisations(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "GET",
        "/api/v1/me",
        private_key=private_key,
        identity=world.multi,
    )
    assert response.status_code == 200
    body = response.json()
    roles_by_org = {entry["organisation_id"]: set(entry["roles"]) for entry in body["memberships"]}
    assert roles_by_org[str(world.org_a)] == {"owner"}
    assert roles_by_org[str(world.org_b)] == {"viewer"}
    # Organisation roles never surface as platform authority.
    assert body["platform_roles"] == []


async def test_suspended_membership_is_rejected(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "GET",
        "/api/v1/records",
        private_key=private_key,
        identity=world.suspended,
        org_id=world.org_a,
    )
    _assert_error(response, FORBIDDEN, "not_a_member")


async def test_a_only_user_cannot_select_the_b_org_context(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "GET",
        "/api/v1/records",
        private_key=private_key,
        identity=world.a_owner,
        org_id=world.org_b,
    )
    _assert_error(response, FORBIDDEN, "not_a_member")


# --- Cross-plane: platform authority is not tenant authority ----------------


async def test_org_owner_without_platform_membership_is_rejected_on_platform_route(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "GET",
        "/api/v1/platform/organisations",
        private_key=private_key,
        identity=world.a_owner,
    )
    _assert_error(response, FORBIDDEN, "platform_admin_required")


async def test_org_owner_role_does_not_grant_platform_access(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "GET",
        "/api/v1/platform/organisations",
        private_key=private_key,
        identity=world.multi,
    )
    _assert_error(response, FORBIDDEN, "platform_admin_required")


async def test_platform_admin_can_use_the_platform_plane(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "GET",
        "/api/v1/platform/organisations",
        private_key=private_key,
        identity=world.platform_only,
    )
    assert response.status_code == 200
    assert "items" in response.json()


async def test_platform_authority_alone_does_not_grant_tenant_data(
    client: AsyncClient, world: IsolationWorld, private_key: rsa.RSAPrivateKey
) -> None:
    response = await _call(
        client,
        "GET",
        "/api/v1/records",
        private_key=private_key,
        identity=world.platform_only,
        org_id=world.org_a,
    )
    _assert_error(response, FORBIDDEN, "not_a_member")


# --- The matrix catches an omitted organisation predicate -------------------


async def test_matrix_guard_detects_an_omitted_organisation_predicate(
    migrated_database: str, world: IsolationWorld
) -> None:
    """A test-only unscoped query must be caught by the shared row guard.

    This is plan P2 checkbox 12: it demonstrates the shared
    :func:`assert_org_scoped_row` guard is load-bearing by deliberately dropping
    the ``organisation_id`` predicate in test-only code and showing the guard
    rejects the leaked foreign row. The guard is used by the direct-row
    assertions above (for example the transfer-reference reads); the
    API-response assertions rely on the not-found error envelope instead.
    """
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            leaked = await fetch_record_without_org_predicate(session, record_id=world.record_a)
            assert leaked is not None
            # The unscoped query returns A's row even though the caller selected
            # B; the shared guard must reject it.
            with pytest.raises(AssertionError, match="cross-organisation row leaked"):
                assert_org_scoped_row(leaked.organisation_id, world.org_b)
    finally:
        await engine.dispose()
