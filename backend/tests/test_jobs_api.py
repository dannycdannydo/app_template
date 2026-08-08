"""Integration tests for the job endpoints (Scope §6.5).

The full ASGI stack runs with the fakes from ``context_helpers.py``, so the
suite needs neither PostgreSQL nor Redis (the real-broker journeys live in
``test_jobs_broker.py`` and ``test_files_jobs.py``). These tests exercise the
polling contract: permission gating (documents.read), the pagination envelope,
the status/job_type filter wiring, the detail payload (status + progress +
error surface), and the 404 contract for cross-organisation jobs.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from tests.auth_helpers import make_token
from tests.context_helpers import (
    ContextApp,
    ContextState,
    build_context_app_fixture,
    context_client,
    make_job,
    make_membership,
    make_user,
)

from app.modules.jobs.models import JobStatus
from app.modules.organisations.models import OrganisationMembership


@pytest.fixture
def context_app() -> ContextApp:
    return build_context_app_fixture()


def _auth_headers(token: str, org_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": str(org_id)}


def _reader_headers(
    state: ContextState,
    private_key: rsa.RSAPrivateKey,
    org_id: uuid.UUID,
) -> tuple[str, object, OrganisationMembership]:
    """Stage an authenticated reader with the documents.read permission."""
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"documents.read"}
    return make_token(private_key), user, membership


async def test_list_requires_token(context_app: ContextApp) -> None:
    app, _state, _private_key = context_app
    async with context_client(app) as client:
        response = await client.get("/api/v1/jobs")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_list_requires_org_context(context_app: ContextApp) -> None:
    app, state, private_key = context_app
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/jobs", headers={"Authorization": f"Bearer {make_token(private_key)}"}
        )
    assert response.status_code == 400
    assert response.json()["code"] == "org_context_required"


async def test_list_returns_pagination_envelope(context_app: ContextApp) -> None:
    """Acceptance §5.7: the list returns the documented pagination envelope."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = _reader_headers(state, private_key, org_id)
    queued = make_job(org_id, job_type="file.processing", status=JobStatus.QUEUED)
    succeeded = make_job(
        org_id,
        job_type="file.processing",
        status=JobStatus.SUCCEEDED,
        progress=100,
    )
    state.jobs = [queued, succeeded]
    state.lookup_queue.append(2)  # the total count

    async with context_client(app) as client:
        response = await client.get("/api/v1/jobs", headers=_auth_headers(token, org_id))

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [str(queued.id), str(succeeded.id)]
    assert [item["status"] for item in body["items"]] == ["queued", "succeeded"]
    # List items are summaries: no error surface, no input reference.
    assert "error_code" not in body["items"][0]
    assert "input_reference" not in body["items"][0]


async def test_list_accepts_status_and_job_type_filters(context_app: ContextApp) -> None:
    """The status/job_type query parameters are wired to the service call.

    The WHERE clauses themselves are proven at the SQL level in
    ``test_files_jobs.py``; here the endpoint accepts the parameters and still
    returns the documented envelope.
    """
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = _reader_headers(state, private_key, org_id)
    state.jobs = [make_job(org_id, status=JobStatus.SUCCEEDED, progress=100)]
    state.lookup_queue.append(1)

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/jobs",
            params={"status": "succeeded", "job_type": "file.processing"},
            headers=_auth_headers(token, org_id),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "succeeded"


async def test_list_rejects_unknown_status_value(context_app: ContextApp) -> None:
    """Only the closed status set is an approved filter value (BP §12)."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = _reader_headers(state, private_key, org_id)
    state.jobs = []

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/jobs",
            params={"status": "not-a-status"},
            headers=_auth_headers(token, org_id),
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_get_job_detail_returns_status_and_progress(context_app: ContextApp) -> None:
    """Acceptance §5.7: the polling payload is status + progress + error."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = _reader_headers(state, private_key, org_id)
    running = make_job(
        org_id,
        status=JobStatus.RUNNING,
        progress=50,
        input_reference="file-1",
    )
    state.lookup_queue.append(running)

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/jobs/{running.id}", headers=_auth_headers(token, org_id)
        )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(running.id)
    assert body["status"] == "running"
    assert body["progress"] == 50
    assert body["job_type"] == "file.processing"
    assert body["input_reference"] == "file-1"
    assert body["error_code"] is None
    assert body["error_message"] is None


async def test_get_job_detail_includes_error_surface(context_app: ContextApp) -> None:
    """A failed job exposes the error code/message the UI and audits read."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = _reader_headers(state, private_key, org_id)
    failed = make_job(
        org_id,
        status=JobStatus.FAILED,
        error_code="file_verification_failed",
        error_message="The stored object could not be verified while processing.",
    )
    state.lookup_queue.append(failed)

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/jobs/{failed.id}", headers=_auth_headers(token, org_id)
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "file_verification_failed"


async def test_get_job_cross_org_is_404(context_app: ContextApp) -> None:
    """Acceptance §5.7: another org's job id is a 404, never a leak."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    token, _user, _membership = _reader_headers(state, private_key, org_id)
    other_job = make_job(uuid.uuid4())
    state.lookup_queue.append(None)  # the org-scoped lookup finds nothing

    async with context_client(app) as client:
        response = await client.get(
            f"/api/v1/jobs/{other_job.id}", headers=_auth_headers(token, org_id)
        )

    assert response.status_code == 404
    assert response.json()["code"] == "job_not_found"


async def test_reader_without_documents_read_is_denied(context_app: ContextApp) -> None:
    """The job endpoints reuse the documents.read gate (rule of three)."""
    app, state, private_key = context_app
    org_id = uuid.uuid4()
    user = make_user()
    state.users[user.workos_user_id] = user
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"records.read"}  # some other permission

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/jobs", headers=_auth_headers(make_token(private_key), org_id)
        )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
