"""Integration tests for the files module (Scope §6.3).

The full ASGI stack runs with the fakes from ``context_helpers.py`` and the
in-memory ``FakeObjectStorage`` (pinned by ``STORAGE_PROVIDER=fake`` in
``conftest.py``), so the suite needs neither PostgreSQL nor MinIO; the
real-database scoping and status-filter proof lives in ``test_files_db.py``.
These tests exercise the direct-upload flow: permission gating per route,
intent-time validation (oversized, disallowed type/extension, no smuggled
object key), the intent response shape, completion verification (existence,
size, checksum), the download-url contract, soft delete, and the 404 contract
for cross-organisation files.
"""

from __future__ import annotations

import uuid
from typing import cast as typing_cast

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient, Response
from sqlalchemy import Table
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    ContextState,
    build_context_app_fixture,
    context_client,
    make_file,
    make_membership,
    make_user,
)

from app.core.config import get_settings
from app.db.base import Base
from app.modules.files.models import File, FileStatus
from app.modules.files.service import (
    _EXTENSIONS_BY_CONTENT_TYPE,  # pyright: ignore[reportPrivateUsage]
)
from app.modules.organisations.models import OrganisationMembership
from app.modules.users.models import User
from app.storage import FakeObjectStorage, get_storage


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


# --- Model metadata (BP §7, §10, §17) ---


def test_files_table_registered_on_base_metadata() -> None:
    assert "files" in Base.metadata.tables


def test_file_has_org_and_creator_foreign_keys_and_composite_index() -> None:
    table = typing_cast(Table, File.__table__)
    fk_names = {constraint.name for constraint in table.foreign_key_constraints}
    assert fk_names == {
        "fk_files_organisation_id_organisations",
        "fk_files_created_by_user_id_users",
    }
    index_names = {index.name for index in table.indexes}
    assert index_names == {
        "ix_files_organisation_id",
        "ix_files_organisation_id_created_at",
        "ix_files_created_by_user_id",
    }
    composite = next(
        index for index in table.indexes if index.name == "ix_files_organisation_id_created_at"
    )
    assert [column.name for column in composite.columns] == [
        "organisation_id",
        "created_at",
    ]


# --- Request flow (acceptance §5.3, §5.4, §5.5, §5.7) ---


async def _intent(
    client: AsyncClient,
    token: str,
    org_id: uuid.UUID,
    payload: dict[str, object],
) -> Response:
    return await client.post(
        "/api/v1/files",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "X-Org-Id": str(org_id)},
    )


async def _complete(
    client: AsyncClient,
    token: str,
    org_id: uuid.UUID,
    file_id: uuid.UUID,
    payload: dict[str, object] | None = None,
) -> Response:
    return await client.post(
        f"/api/v1/files/{file_id}/complete",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "X-Org-Id": str(org_id)},
    )


def _auth_headers(token: str, org_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": str(org_id)}


async def test_list_requires_token(context_app: ContextApp) -> None:
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.get("/api/v1/files")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_intent_requires_org_context(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]

    async with context_client(app) as client:
        response = await client.post(
            "/api/v1/files",
            json={"original_filename": "x.pdf", "content_type": "application/pdf", "size_bytes": 1},
            headers={"Authorization": f"Bearer {make_token(private_key)}"},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "org_context_required"


async def test_list_returns_pagination_envelope(context_app: ContextApp) -> None:
    """Acceptance §5.4: the list returns the documented pagination envelope."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.files = [
        make_file(org_id, original_filename="alpha.pdf"),
        make_file(org_id, original_filename="beta.pdf", status=FileStatus.UPLOADED),
    ]
    state.lookup_queue = [user, membership, 2]  # user, membership, then total count
    state.granted_permissions = {"documents.read"}

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/files", headers=_auth_headers(make_token(private_key), org_id)
        )

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [str(f.id) for f in state.files]
    assert [item["original_filename"] for item in body["items"]] == ["alpha.pdf", "beta.pdf"]
    assert item_is_summary(body["items"][0])


def item_is_summary(item: dict[str, object]) -> bool:
    """List items are summaries: no checksum, no timestamps beyond created_at."""
    return "checksum" not in item and "updated_at" not in item


async def test_viewer_write_is_denied(context_app: ContextApp) -> None:
    """Acceptance §5.5: a viewer can read files but every write returns 403."""
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"documents.read"}  # the viewer bundle

    async with context_client(app) as client:
        token = make_token(private_key)
        response = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "sneaky.pdf",
                "content_type": "application/pdf",
                "size_bytes": 100,
            },
        )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert state.files == []  # nothing was created


# --- Intent step (acceptance §5.3, §5.5) ---


async def _intent_headers(
    state: ContextState,
    private_key: rsa.RSAPrivateKey,
    org_id: uuid.UUID,
) -> tuple[str, User, OrganisationMembership]:
    """Stage an authenticated uploader with the documents.* permissions."""
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"documents.read", "documents.upload"}
    return make_token(private_key), user, membership


def _fake_storage() -> FakeObjectStorage:
    """Return the process-wide fake adapter as its concrete type (has ``put``)."""
    return typing_cast(FakeObjectStorage, get_storage())


async def test_intent_creates_pending_file_and_signed_url(context_app: ContextApp) -> None:
    """Acceptance §5.3: intent returns {file_id, upload_url, expires_at}."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, membership = await _intent_headers(state, private_key, org_id)

    async with context_client(app) as client:
        response = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 2048,
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"file_id", "upload_url", "expires_at"}
    file_id = uuid.UUID(body["file_id"])
    assert body["upload_url"].startswith("https://storage.example.invalid/upload/")
    assert "expires=" in body["upload_url"]

    assert len(state.files) == 1
    file = state.files[0]
    assert file.id == file_id
    assert file.organisation_id == org_id
    assert file.status == FileStatus.PENDING
    assert file.original_filename == "report.pdf"
    assert file.content_type == "application/pdf"
    assert file.size_bytes == 2048
    assert file.created_by_user_id == membership.user_id
    assert file.storage_provider == "fake"
    assert file.object_key == f"organisations/{org_id}/documents/{file_id}/original"
    assert file.object_key in body["upload_url"]

    actions = [event.action for event in state.audit_events]
    assert "file.upload_started" in actions


async def test_intent_rejects_oversized_upload_before_issuing_url(context_app: ContextApp) -> None:
    """Acceptance §5.5: a size above the configured maximum is a 422, no URL."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = await _intent_headers(state, private_key, org_id)
    oversized = get_settings().storage_max_upload_size + 1

    async with context_client(app) as client:
        response = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "huge.pdf",
                "content_type": "application/pdf",
                "size_bytes": oversized,
            },
        )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "file_too_large"
    assert any(detail["field"] == "size_bytes" for detail in body["details"])
    assert state.files == []
    assert state.audit_events == []


async def test_intent_rejects_disallowed_content_type(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = await _intent_headers(state, private_key, org_id)

    async with context_client(app) as client:
        response = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "evil.exe",
                "content_type": "application/x-msdownload",
                "size_bytes": 100,
            },
        )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "unsupported_content_type"
    assert any(detail["field"] == "content_type" for detail in body["details"])
    assert state.files == []


async def test_intent_rejects_extension_that_mismatches_content_type(
    context_app: ContextApp,
) -> None:
    """BP §30: the extension must match the declared content type."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = await _intent_headers(state, private_key, org_id)

    async with context_client(app) as client:
        response = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "report.exe",
                "content_type": "application/pdf",
                "size_bytes": 100,
            },
        )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "unsupported_file_extension"
    assert any(detail["field"] == "original_filename" for detail in body["details"])
    assert state.files == []


async def test_every_allowed_content_type_round_trips_through_intent(
    context_app: ContextApp,
) -> None:
    """Review nit: guard drift between settings and the extension mapping.

    ``STORAGE_ALLOWED_CONTENT_TYPES`` and ``_EXTENSIONS_BY_CONTENT_TYPE`` must
    stay in sync: an allowed type without a configured extension mapping
    silently rejects every filename for it (fail closed). If either side
    drifts, one of the two asserts below fires and the drift is caught here.
    """
    settings = get_settings()
    allowed = set(settings.storage_allowed_content_types)
    mapped = set(_EXTENSIONS_BY_CONTENT_TYPE)
    assert allowed == mapped, (
        "allowed content types and extension mappings have drifted: "
        f"missing from mapping: {sorted(allowed - mapped)}, "
        f"mapped but not allowed: {sorted(mapped - allowed)}"
    )

    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = await _intent_headers(state, private_key, org_id)

    async with context_client(app) as client:
        for content_type in sorted(allowed):
            extension = sorted(_EXTENSIONS_BY_CONTENT_TYPE[content_type])[0]
            # The fake lookup queue is consumed per request: refill it each pass.
            state.lookup_queue.extend([_user, _membership])
            response = await _intent(
                client,
                token,
                org_id,
                {
                    "original_filename": f"drift-guard.{extension}",
                    "content_type": content_type,
                    "size_bytes": 128,
                },
            )
            assert response.status_code == 201, (
                f"intent rejected for allowed content type {content_type!r}: {response.json()}"
            )
    assert len(state.files) == len(allowed)


async def test_intent_rejects_smuggled_object_key(context_app: ContextApp) -> None:
    """Acceptance §5.3: extra="forbid" — the client never supplies a key."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = await _intent_headers(state, private_key, org_id)

    async with context_client(app) as client:
        response = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 100,
                "object_key": "organisations/other/documents/x/original",
            },
        )

    assert response.status_code == 422
    assert state.files == []


# --- Completion step (acceptance §5.3, §5.5) ---


async def test_complete_verifies_object_and_marks_uploaded(context_app: ContextApp) -> None:
    """Acceptance §5.3: intent -> PUT -> complete transitions pending -> uploaded."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, membership = await _intent_headers(state, private_key, org_id)

    async with context_client(app) as client:
        intent = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 11,
            },
        )
    file_id = uuid.UUID(intent.json()["file_id"])
    object_key = f"organisations/{org_id}/documents/{file_id}/original"
    await _fake_storage().put(object_key, b"hello world")  # the browser's direct PUT

    # The completion request re-authenticates: queue user, membership, file.
    state.lookup_queue = [_user, membership, state.files[0]]
    async with context_client(app) as client:
        response = await _complete(client, token, org_id, file_id)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(file_id)
    assert body["status"] == "uploaded"
    assert body["checksum"] is not None
    assert "processing_job_id" in body
    assert body["processing_job_id"] is None  # the job foundation is Scope §6.4/§6.5

    file = state.files[0]
    assert file.status == FileStatus.UPLOADED
    assert file.checksum == body["checksum"]
    actions = [event.action for event in state.audit_events]
    assert actions.count("file.uploaded") == 1
    uploaded = next(event for event in state.audit_events if event.action == "file.uploaded")
    assert uploaded.event_metadata["object_key"] == object_key


async def test_complete_with_checksum_verifies_equality(context_app: ContextApp) -> None:
    """A supplied checksum is compared for equality with the provider's."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, user, membership = await _intent_headers(state, private_key, org_id)

    async with context_client(app) as client:
        intent = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 11,
            },
        )
    file_id = uuid.UUID(intent.json()["file_id"])
    object_key = f"organisations/{org_id}/documents/{file_id}/original"
    await _fake_storage().put(object_key, b"hello world")

    state.lookup_queue = [user, membership, state.files[0]]
    async with context_client(app) as client:
        response = await _complete(
            client, token, org_id, file_id, payload={"checksum": "right-checksum"}
        )

    # A checksum the provider never produced fails verification (acceptance §5.5).
    assert response.status_code == 422
    assert response.json()["code"] == "upload_verification_failed"
    file = state.files[0]
    assert file.status == FileStatus.FAILED
    actions = [event.action for event in state.audit_events]
    assert "file.upload_failed" in actions


async def test_complete_missing_object_fails_file(context_app: ContextApp) -> None:
    """Acceptance §5.5: completing without the object ever stored is a failure."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, user, membership = await _intent_headers(state, private_key, org_id)

    async with context_client(app) as client:
        intent = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 11,
            },
        )
    file_id = uuid.UUID(intent.json()["file_id"])
    # No PUT: the browser never uploaded the object.

    state.lookup_queue = [user, membership, state.files[0]]
    async with context_client(app) as client:
        response = await _complete(client, token, org_id, file_id)

    assert response.status_code == 422
    assert response.json()["code"] == "upload_verification_failed"
    assert state.files[0].status == FileStatus.FAILED
    assert "file.upload_failed" in [e.action for e in state.audit_events]


async def test_complete_size_mismatch_fails_file(context_app: ContextApp) -> None:
    """Acceptance §5.5: an object whose size differs from the declaration fails."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, user, membership = await _intent_headers(state, private_key, org_id)

    async with context_client(app) as client:
        intent = await _intent(
            client,
            token,
            org_id,
            {
                "original_filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 11,
            },
        )
    file_id = uuid.UUID(intent.json()["file_id"])
    object_key = f"organisations/{org_id}/documents/{file_id}/original"
    # The fake refuses a PUT whose size differs from the declared size; store
    # through the interface's declared-size enforcement by changing the
    # declaration first — this proves the verification seam end to end.
    await _fake_storage().create_upload_url(
        file_id=file_id,
        object_key=object_key,
        content_type="application/pdf",
        size_bytes=6,
    )
    await _fake_storage().put(object_key, b"six...")

    state.lookup_queue = [user, membership, state.files[0]]
    async with context_client(app) as client:
        response = await _complete(client, token, org_id, file_id)

    assert response.status_code == 422
    assert state.files[0].status == FileStatus.FAILED
    failed = next(e for e in state.audit_events if e.action == "file.upload_failed")
    assert failed.event_metadata["reason"] == "size_mismatch"


async def test_complete_rejects_non_pending_file(context_app: ContextApp) -> None:
    """A file that is no longer pending cannot be completed again (409)."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    uploaded = make_file(org_id, status=FileStatus.UPLOADED)
    state.files = [uploaded]
    state.lookup_queue = [user, membership, uploaded]
    state.granted_permissions = {"documents.read", "documents.upload"}

    async with context_client(app) as client:
        response = await _complete(client, make_token(private_key), org_id, uploaded.id)

    assert response.status_code == 409
    assert response.json()["code"] == "file_not_pending"


async def test_complete_cross_org_file_is_404(context_app: ContextApp) -> None:
    """Acceptance §5.7: another org's file id is a 404, never a leak."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    other_file = make_file(uuid.uuid4())
    state.lookup_queue = [user, membership, None]  # the org-scoped lookup finds nothing
    state.granted_permissions = {"documents.read", "documents.upload"}

    async with context_client(app) as client:
        response = await _complete(client, make_token(private_key), org_id, other_file.id)

    assert response.status_code == 404
    assert response.json()["code"] == "file_not_found"


# --- Detail, download and delete ---


async def test_get_file_detail(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    file = make_file(org_id, size_bytes=2048, status=FileStatus.READY)
    state.files = [file]
    state.lookup_queue = [user, membership, file]
    state.granted_permissions = {"documents.read"}

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/files/{file.id}",
            headers=_auth_headers(make_token(private_key), org_id),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(file.id)
    assert body["original_filename"] == file.original_filename
    assert body["status"] == "ready"
    assert body["size_bytes"] == 2048


async def test_download_url_returns_signed_get(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    file = make_file(org_id, status=FileStatus.READY)
    state.files = [file]
    state.lookup_queue = [user, membership, file]
    state.granted_permissions = {"documents.read"}

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/files/{file.id}/download-url",
            headers=_auth_headers(make_token(private_key), org_id),
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"download_url", "expires_at"}
    assert body["download_url"].startswith("https://storage.example.invalid/download/")
    assert file.object_key in body["download_url"]


async def test_download_url_rejects_pending_file(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    file = make_file(org_id, status=FileStatus.PENDING)
    state.files = [file]
    state.lookup_queue = [user, membership, file]
    state.granted_permissions = {"documents.read"}

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/files/{file.id}/download-url",
            headers=_auth_headers(make_token(private_key), org_id),
        )

    assert response.status_code == 409
    assert response.json()["code"] == "file_not_downloadable"


async def test_delete_soft_deletes_and_removes_object(context_app: ContextApp) -> None:
    """Acceptance §5.4: delete removes the object and soft-deletes the row."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    file = make_file(org_id, status=FileStatus.UPLOADED)
    state.files = [file]
    state.lookup_queue = [user, membership, file]
    state.granted_permissions = {"documents.read", "documents.delete"}

    await _fake_storage().put(file.object_key, b"x" * file.size_bytes)

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/files/{file.id}",
            headers=_auth_headers(make_token(private_key), org_id),
        )

    assert response.status_code == 204
    assert file.deleted_at is not None
    assert file.status == FileStatus.DELETED
    assert "document.deleted" in [event.action for event in state.audit_events]
    # The object is gone from storage (idempotent delete proved by the adapter).
    assert await get_storage().head_object(file.object_key) is None


async def test_delete_requires_delete_permission(context_app: ContextApp) -> None:
    """A documents.read-only caller cannot delete (default deny)."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    file = make_file(org_id)
    state.files = [file]
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"documents.read"}

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/files/{file.id}",
            headers=_auth_headers(make_token(private_key), org_id),
        )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert file.deleted_at is None
