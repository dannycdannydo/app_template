"""Demonstration endpoint/service tests for ``document.classify`` (v0.7 Scope §6.6).

Two layers:

- the **service layer** runs against a reachable PostgreSQL (migrated to head)
  and proves the synchronous and queued paths end to end: a synchronous
  classification settles an ``ai_requests`` row and returns the validated
  result; a storage reference enqueues a durable job whose request id is
  derived from the job id; the durable record is readable and org-scoped; and
  disabled AI is rejected before dispatch (default-off, v0.7 Scope §6.5).
- the **router layer** runs against the in-memory context app and proves the
  permission gate (``documents.upload`` to trigger, a read-only viewer denied)
  and the request-schema validation (exactly one input form). The execution
  itself is covered by the service layer; the router layer never needs a real
  provider or database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    build_context_app_fixture,
    context_client,
    make_membership,
    make_user,
)

from app.ai.persistence.models import AIRequestRecord, AIRequestStatus
from app.ai.persistence.service import create_default_settings
from app.core.exceptions import NotFoundError, ServiceUnavailableError
from app.modules.ai_demo import service as demo_service
from app.modules.jobs.models import JobStatus
from app.modules.organisations.models import Organisation
from app.modules.users.models import User
from app.storage import FakeObjectStorage, get_storage

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _fake_storage() -> FakeObjectStorage:
    from typing import cast as typing_cast

    return typing_cast(FakeObjectStorage, get_storage())


async def _put_document(
    organisation_id: uuid.UUID, content: str = "A non-sensitive lease fixture."
) -> str:
    key = f"organisations/{organisation_id}/ai/scratch/doc-{uuid.uuid4().hex[:8]}.txt"
    await _fake_storage().put(key, content.encode("utf-8"), content_type="text/plain")
    return key


def _database_reachable(database_url: str) -> bool:
    async def _probe() -> bool:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(__import__("sqlalchemy").text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(_probe())


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    database_url = os.environ["DATABASE_URL"]
    if not _database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


def _session_factory(database_url: str) -> tuple[Any, Any]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _dispose_engine(engine: Any) -> None:
    await engine.dispose()


def _request_statement(organisation_id: uuid.UUID, request_id: str) -> Any:
    from sqlalchemy import select

    return select(AIRequestRecord).where(
        AIRequestRecord.organisation_id == organisation_id,
        AIRequestRecord.request_id == request_id,
        AIRequestRecord.attempt_number == 1,
    )


async def _seed_org_user_and_enable_ai(session: AsyncSession) -> tuple[Organisation, User]:
    organisation = Organisation(name=f"AI Demo Org {uuid.uuid4().hex[:8]}")
    session.add(organisation)
    await session.flush()
    settings_row = await create_default_settings(session, organisation_id=organisation.id)
    settings_row.enabled = True
    user = User(
        workos_user_id=f"demo_user_{uuid.uuid4().hex[:8]}",
        email="demo-classify@example.com",
        name="Demo Classifier",
    )
    session.add(user)
    await session.commit()
    return organisation, user


# --- Service layer (real database) ---


async def test_classify_sync_returns_result_and_records_request(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation, user = await _seed_org_user_and_enable_ai(session)
            storage_key = await _put_document(organisation.id)
            result = await demo_service.classify_sync(
                session,
                organisation_id=organisation.id,
                user=user,
                storage_reference=storage_key,
            )
        assert result.output.category == "lease"
        assert result.routing.provider == "fake"
        assert result.usage.input_tokens >= 0
        async with session_factory() as session:
            record = await session.scalar(_request_statement(organisation.id, result.request_id))
        assert record is not None
        assert record.status == AIRequestStatus.SUCCEEDED
        assert record.task == "document.classify"
        assert record.user_id == user.id
    finally:
        await _dispose_engine(engine)


async def test_ask_sync_returns_answer_and_records_request(migrated_database: str) -> None:
    """A synchronous ``document.ask`` runs through the runtime service (fake
    provider under test), returns the validated text answer and settles a
    durable ``ai_requests`` row for the task (v0.8 Scope §2.2/§6.4)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation, user = await _seed_org_user_and_enable_ai(session)
            storage_key = await _put_document(organisation.id)
            result = await demo_service.ask_sync(
                session,
                organisation_id=organisation.id,
                user=user,
                storage_reference=storage_key,
                question="What kind of document is this?",
            )
        assert isinstance(result.output, str) and result.output
        assert result.routing.provider == "fake"
        assert result.usage.input_tokens >= 0
        async with session_factory() as session:
            record = await session.scalar(_request_statement(organisation.id, result.request_id))
        assert record is not None
        assert record.status == AIRequestStatus.SUCCEEDED
        assert record.task == "document.ask"
        assert record.user_id == user.id
    finally:
        await _dispose_engine(engine)


async def test_ask_sync_disabled_ai_is_rejected(migrated_database: str) -> None:
    """A disabled organisation is rejected before dispatch (default-off)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = Organisation(name=f"Disabled Ask Org {uuid.uuid4().hex[:8]}")
            session.add(organisation)
            await session.flush()
            await create_default_settings(session, organisation_id=organisation.id)
            user = User(
                workos_user_id=f"disabled_ask_{uuid.uuid4().hex[:8]}",
                email="disabled-ask@example.com",
                name="Disabled Ask",
            )
            session.add(user)
            await session.commit()
            storage_key = await _put_document(organisation.id)
            with pytest.raises(ServiceUnavailableError):
                await demo_service.ask_sync(
                    session,
                    organisation_id=organisation.id,
                    user=user,
                    storage_reference=storage_key,
                    question="Is this allowed?",
                )
    finally:
        await _dispose_engine(engine)


async def test_classify_sync_disabled_ai_is_rejected(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation = Organisation(name=f"Disabled AI Org {uuid.uuid4().hex[:8]}")
            session.add(organisation)
            await session.flush()
            await create_default_settings(session, organisation_id=organisation.id)
            user = User(
                workos_user_id=f"disabled_{uuid.uuid4().hex[:8]}",
                email="disabled@example.com",
                name="Disabled",
            )
            session.add(user)
            await session.commit()
            # Put a real object so the resolver succeeds and the disabled-AI
            # policy check is what rejects the request (the service resolves
            # attachments before the policy gate).
            storage_key = await _put_document(organisation.id)
            with pytest.raises(ServiceUnavailableError):
                await demo_service.classify_sync(
                    session,
                    organisation_id=organisation.id,
                    user=user,
                    storage_reference=storage_key,
                )
    finally:
        await _dispose_engine(engine)


async def test_enqueue_creates_job_and_queued_request(migrated_database: str) -> None:
    """The durable job and the ``queued`` AI request row are persisted before enqueue (v0.7 Scope §5.8)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation, user = await _seed_org_user_and_enable_ai(session)
            storage_key = f"organisations/{organisation.id}/ai/scratch/doc.txt"
            accepted = await demo_service.enqueue_classify(
                session,
                organisation_id=organisation.id,
                user=user,
                storage_reference=storage_key,
            )
        assert accepted.status == "queued"
        assert accepted.request_id == uuid.UUID(accepted.job_id).hex
        async with session_factory() as session:
            from app.modules.jobs.models import Job

            job = await session.get(Job, uuid.UUID(accepted.job_id))
        assert job is not None
        assert job.job_type == "ai.execute"
        assert job.input_reference == storage_key
        assert job.status == JobStatus.QUEUED
        # The pre-enqueue queued AI request row exists and is org-scoped.
        async with session_factory() as session:
            record = await session.scalar(_request_statement(organisation.id, accepted.request_id))
        assert record is not None
        assert record.status == AIRequestStatus.QUEUED
        assert record.task == "document.classify"
        assert record.input_reference == storage_key
    finally:
        await _dispose_engine(engine)


async def test_queued_result_available_immediately_after_enqueue(
    migrated_database: str,
) -> None:
    """The result endpoint returns ``queued`` immediately after the 202 (v0.7 Scope §5.8)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation, user = await _seed_org_user_and_enable_ai(session)
            storage_key = f"organisations/{organisation.id}/ai/scratch/doc.txt"
            accepted = await demo_service.enqueue_classify(
                session,
                organisation_id=organisation.id,
                user=user,
                storage_reference=storage_key,
            )
        async with session_factory() as session:
            result = await demo_service.get_classify_result(
                session, organisation_id=organisation.id, request_id=accepted.request_id
            )
        assert result.status == "queued"
        assert result.output is None
        assert result.routing is None
        assert result.completed_at is None
    finally:
        await _dispose_engine(engine)


async def test_result_reports_winning_attempt_after_multi_attempt(
    migrated_database: str,
) -> None:
    """The result endpoint reports the winning (succeeded) attempt, not attempt 1 (v0.7 Scope §6.4/§6.6)."""
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation, user = await _seed_org_user_and_enable_ai(session)
        request_id = uuid.uuid4().hex
        storage_key = f"organisations/{organisation.id}/ai/scratch/doc.txt"
        async with session_factory() as session:
            session.add_all(
                [
                    AIRequestRecord(
                        organisation_id=organisation.id,
                        user_id=user.id,
                        request_id=request_id,
                        attempt_number=1,
                        task="document.classify",
                        provider="fake",
                        model="fake-model-document.classify",
                        prompt_name="document.classify",
                        prompt_version=1,
                        status=AIRequestStatus.FAILED,
                        error_code="provider_unavailable",
                        input_reference=storage_key,
                    ),
                    AIRequestRecord(
                        organisation_id=organisation.id,
                        user_id=user.id,
                        request_id=request_id,
                        attempt_number=2,
                        task="document.classify",
                        provider="fake",
                        model="fake-model-document.classify",
                        prompt_name="document.classify",
                        prompt_version=1,
                        status=AIRequestStatus.SUCCEEDED,
                        input_reference=storage_key,
                    ),
                ]
            )
            await session.commit()
        async with session_factory() as session:
            result = await demo_service.get_classify_result(
                session, organisation_id=organisation.id, request_id=request_id
            )
        assert result.status == "succeeded"
        assert result.error_code is None
    finally:
        await _dispose_engine(engine)


async def test_enqueue_rejects_foreign_storage_reference(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation, user = await _seed_org_user_and_enable_ai(session)
            from app.core.exceptions import ValidationError

            with pytest.raises(ValidationError):
                await demo_service.enqueue_classify(
                    session,
                    organisation_id=organisation.id,
                    user=user,
                    storage_reference="organisations/00000000-0000-7000-8000-000000000000/x",
                )
    finally:
        await _dispose_engine(engine)


async def test_get_classify_result_is_org_scoped(migrated_database: str) -> None:
    engine, session_factory = _session_factory(migrated_database)
    try:
        async with session_factory() as session:
            organisation, user = await _seed_org_user_and_enable_ai(session)
            storage_key = await _put_document(organisation.id, "Another non-sensitive fixture.")
            result = await demo_service.classify_sync(
                session,
                organisation_id=organisation.id,
                user=user,
                storage_reference=storage_key,
            )
        # Same organisation sees the record.
        async with session_factory() as session:
            record = await demo_service.get_classify_result(
                session, organisation_id=organisation.id, request_id=result.request_id
            )
        assert record.status == "succeeded"
        # A different organisation does not (404, indistinguishable from missing).
        async with session_factory() as session:
            with pytest.raises(NotFoundError):
                await demo_service.get_classify_result(
                    session, organisation_id=uuid.uuid4(), request_id=result.request_id
                )
    finally:
        await _dispose_engine(engine)


# --- Router layer (in-memory context app) ---


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


def _auth_headers(token: str, org_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": str(org_id)}


async def test_post_requires_documents_upload(context_app: ContextApp) -> None:
    """A read-only viewer (documents.read, no documents.upload) is denied (403)."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"documents.read"}  # viewer-style: read only
    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/ai/classify",
            json={"storage_reference": f"organisations/{org_id}/ai/scratch/doc.txt"},
            headers=_auth_headers(make_token(private_key), org_id),
        )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


async def test_post_validates_request_schema(context_app: ContextApp) -> None:
    """A missing storage_reference or an unknown field is a 422 (BP §12)."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()

    def _stage() -> str:
        # Dependencies resolve before body validation, so a valid member must be
        # staged for each request to reach the schema check.
        user = make_user()
        state.users[user.workos_user_id] = user
        membership = make_membership(user, org_id)
        state.lookup_queue = [user, membership]
        state.granted_permissions = {"documents.upload"}
        return make_token(private_key)

    async with context_client(app) as client:
        missing = await client.post(
            "/api/v1/ai/classify",
            json={},
            headers=_auth_headers(_stage(), org_id),
        )
        extra = await client.post(
            "/api/v1/ai/classify",
            json={"storage_reference": "organisations/x/y", "text": "x"},
            headers=_auth_headers(_stage(), org_id),
        )
    assert missing.status_code == 422
    assert extra.status_code == 422


async def test_scratch_upload_intent_requires_documents_upload(context_app: ContextApp) -> None:
    """A read-only viewer is denied the scratch upload intent (403)."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"documents.read"}  # viewer-style: read only
    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/ai/scratch/uploads",
            json={
                "original_filename": "lease.pdf",
                "content_type": "application/pdf",
                "size_bytes": 4096,
            },
            headers=_auth_headers(make_token(private_key), org_id),
        )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


async def test_scratch_upload_intent_and_complete_flow(context_app: ContextApp) -> None:
    """The transient upload journey signs a PUT URL into ``ai/scratch/`` and the
    completion returns the verified storage reference; a never-stored upload is
    rejected and a non-PDF intent is a 422."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()

    def _stage() -> str:
        # Dependencies resolve per request: stage a valid member each time.
        user = make_user()
        state.users[user.workos_user_id] = user
        membership = make_membership(user, org_id)
        state.lookup_queue = [user, membership]
        state.granted_permissions = {"documents.upload"}
        return make_token(private_key)

    async with context_client(app) as client:
        # A non-PDF declaration is rejected before any URL is signed.
        rejected = await client.post(
            "/api/v1/ai/scratch/uploads",
            json={
                "original_filename": "notes.txt",
                "content_type": "text/plain",
                "size_bytes": 1024,
            },
            headers=_auth_headers(_stage(), org_id),
        )
        assert rejected.status_code == 422

        size = 4096
        intent = await client.post(
            "/api/v1/ai/scratch/uploads",
            json={
                "original_filename": "lease.pdf",
                "content_type": "application/pdf",
                "size_bytes": size,
            },
            headers=_auth_headers(_stage(), org_id),
        )
        assert intent.status_code == 201
        body = intent.json()
        assert body["upload_id"]
        assert body["upload_url"].startswith("http")

        # Completing before the bytes exist is rejected.
        missing = await client.post(
            f"/api/v1/ai/scratch/uploads/{body['upload_id']}/complete",
            headers=_auth_headers(_stage(), org_id),
        )
        assert missing.status_code == 422

        # Simulate the browser PUT, then complete.
        key = f"organisations/{org_id}/ai/scratch/{body['upload_id']}.pdf"
        await _fake_storage().put(key, b"%PDF-1.4" + b"x" * (size - len(b"%PDF-1.4")))
        completed = await client.post(
            f"/api/v1/ai/scratch/uploads/{body['upload_id']}/complete",
            headers=_auth_headers(_stage(), org_id),
        )
        assert completed.status_code == 200
        assert completed.json()["storage_reference"] == key


async def test_scratch_upload_complete_rejects_stored_objects_outside_the_contract(
    context_app: ContextApp,
) -> None:
    """Completion validates the *stored* object against the same PDF/ceiling
    contract the intent declared, so a wrong-MIME object is never returned as
    a "verified" reference that the next AI read would immediately reject."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()

    def _stage() -> str:
        user = make_user()
        state.users[user.workos_user_id] = user
        membership = make_membership(user, org_id)
        state.lookup_queue = [user, membership]
        state.granted_permissions = {"documents.upload"}
        return make_token(private_key)

    async with context_client(app) as client:
        size = 5
        intent = await client.post(
            "/api/v1/ai/scratch/uploads",
            json={
                "original_filename": "lease.pdf",
                "content_type": "application/pdf",
                "size_bytes": size,
            },
            headers=_auth_headers(_stage(), org_id),
        )
        assert intent.status_code == 201
        body = intent.json()

        # The browser PUTs a wrong-MIME object: completion must reject it.
        key = f"organisations/{org_id}/ai/scratch/{body['upload_id']}.pdf"
        await _fake_storage().put(key, b"hello", content_type="text/plain")
        rejected = await client.post(
            f"/api/v1/ai/scratch/uploads/{body['upload_id']}/complete",
            headers=_auth_headers(_stage(), org_id),
        )
        assert rejected.status_code == 422


async def test_get_result_requires_documents_read(context_app: ContextApp) -> None:
    """A caller without documents.read is denied the result read (403)."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = set()  # no permissions at all
    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/ai/classify/requests/{uuid.uuid4().hex}",
            headers=_auth_headers(make_token(private_key), org_id),
        )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


async def test_ask_requires_documents_upload(context_app: ContextApp) -> None:
    """A read-only viewer is denied the ask endpoint (403)."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"documents.read"}  # viewer-style: read only
    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/ai/ask",
            json={
                "storage_reference": f"organisations/{org_id}/ai/scratch/doc.txt",
                "question": "What is this?",
            },
            headers=_auth_headers(make_token(private_key), org_id),
        )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


async def test_ask_validates_request_schema(context_app: ContextApp) -> None:
    """A missing question, an empty question or an unknown field is a 422."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()

    def _stage() -> str:
        user = make_user()
        state.users[user.workos_user_id] = user
        membership = make_membership(user, org_id)
        state.lookup_queue = [user, membership]
        state.granted_permissions = {"documents.upload"}
        return make_token(private_key)

    async with context_client(app) as client:
        missing = await client.post(
            "/api/v1/ai/ask",
            json={"storage_reference": f"organisations/{org_id}/ai/scratch/doc.txt"},
            headers=_auth_headers(_stage(), org_id),
        )
        empty = await client.post(
            "/api/v1/ai/ask",
            json={
                "storage_reference": f"organisations/{org_id}/ai/scratch/doc.txt",
                "question": "",
            },
            headers=_auth_headers(_stage(), org_id),
        )
        extra = await client.post(
            "/api/v1/ai/ask",
            json={
                "storage_reference": f"organisations/{org_id}/ai/scratch/doc.txt",
                "question": "What is this?",
                "sync": True,
            },
            headers=_auth_headers(_stage(), org_id),
        )
    assert missing.status_code == 422
    assert empty.status_code == 422
    assert extra.status_code == 422
