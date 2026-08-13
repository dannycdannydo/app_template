"""Mandatory reusable security tests (blueprint §31, v0.2 Scope §6.6).

One table-driven suite encodes the security properties every protected
endpoint must satisfy: unauthenticated requests rejected, invalid sessions
rejected, disabled users rejected, missing or malformed organisation context
rejected, cross-organisation access denied, viewer writes denied, and the
standard error envelope never exposing stack traces (acceptance §5.1, §5.4,
§5.5, §5.6, §5.8).

The suite is reusable by construction: ``PROTECTED_ROUTES`` lists every
protected route exactly once, and ``test_no_protected_route_is_left_out``
fails when a new ``/api/v1`` route is registered without being added to the
table. A module that adds endpoints therefore inherits the whole security
matrix by extending the table.

Runs against the full ASGI stack with the fakes from ``context_helpers.py``
(the same philosophy as ``test_records.py``): the real WorkOS session
validator backed by a local RSA key, and in-memory stand-ins for the database
session and the WorkOS profile client, so the suite needs neither PostgreSQL
nor a network connection. The signature/issuer/audience/expiry matrix for
``/me`` stays in ``test_auth.py``; this suite proves the same properties are
enforced on every protected route.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Annotated, Any, cast

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.routing import Route
from tests.auth_helpers import generate_key_pair, make_token, webhook_signature_header
from tests.context_helpers import (
    ContextState,
    build_context_app,
    make_membership,
    make_owner_role,
    make_user,
)

import app.api.dependencies as dependencies_module
from app.api.dependencies import get_current_membership
from app.main import create_app
from app.modules.organisations.models import OrganisationMembership

# Markers that must never appear in a serialised error response (BP §13,
# acceptance §5.8 "stack traces not exposed").
_TRACEBACK_MARKERS = ("Traceback", 'File "', "line ", "internal secret detail")

_RECORD_ID = str(uuid.uuid4())
_ORG_ID = str(uuid.uuid4())
_INVITATION_ID = str(uuid.uuid4())
_MEMBERSHIP_ID = str(uuid.uuid4())
_FEATURE_FLAG_ORG_ID = str(uuid.uuid4())
_AI_SETTINGS_ORG_ID = str(uuid.uuid4())
_FILE_ID = str(uuid.uuid4())
_JOB_ID = str(uuid.uuid4())
_NOTIFICATION_ID = str(uuid.uuid4())
_AI_REQUEST_ID = uuid.uuid4().hex

_PRIVATE_KEY, _ = generate_key_pair()
_OTHER_KEY, _ = generate_key_pair()

# Tokens that must be rejected with 401 on every protected route: signed by
# the wrong key, or with wrong issuer, audience or client id, or expired.
BAD_TOKENS: list[tuple[str, str]] = [
    ("tampered-signature", make_token(_OTHER_KEY)),
    ("wrong-issuer", make_token(_PRIVATE_KEY, issuer="https://evil.example.com/")),
    ("wrong-client-id", make_token(_PRIVATE_KEY, client_id="client_other")),
    ("wrong-audience", make_token(_PRIVATE_KEY, aud="client_other")),
    ("expired", make_token(_PRIVATE_KEY, seconds_valid=-3600)),
]


@dataclass(frozen=True)
class RouteSpec:
    """One protected route, with everything needed to exercise it."""

    method: str
    path: str  # FastAPI path syntax, e.g. /api/v1/records/{record_id}
    org_scoped: bool  # requires the X-Org-Id header
    request_body: dict[str, object] | None = None  # valid body if the route reads one
    path_values: dict[str, str] = field(default_factory=dict[str, str])  # concrete path params


def _route(
    method: str,
    path: str,
    *,
    org_scoped: bool,
    request_body: dict[str, object] | None = None,
    path_values: dict[str, str] | None = None,
) -> RouteSpec:
    values = {"record_id": _RECORD_ID}
    if path_values:
        values.update(path_values)
    return RouteSpec(
        method=method,
        path=path,
        org_scoped=org_scoped,
        request_body=request_body,
        path_values=values,
    )


# The whole protected surface of v0.2, listed once so the security matrix and
# the completeness guard stay in one place. Endpoints outside /api/v1
# (/health, /ready, /docs) are intentionally public (acceptance §5.4).
# Platform routes (Scope §6.2) are protected like every other route and never
# take X-Org-Id; the non-platform-admin 403 case is parametrised below.
PROTECTED_ROUTES: list[RouteSpec] = [
    _route("GET", "/api/v1/me", org_scoped=False),
    _route("POST", "/api/v1/organisations", org_scoped=False, request_body={"name": "Acme"}),
    _route("GET", "/api/v1/records", org_scoped=True),
    _route(
        "POST",
        "/api/v1/records",
        org_scoped=True,
        request_body={"title": "A record", "body": ""},
    ),
    _route("GET", "/api/v1/records/{record_id}", org_scoped=True),
    _route(
        "PATCH",
        "/api/v1/records/{record_id}",
        org_scoped=True,
        request_body={"title": "Renamed"},
    ),
    _route("DELETE", "/api/v1/records/{record_id}", org_scoped=True),
    # Files (Scope §6.3): the direct-upload surface. Intent and completion are
    # gated by documents.upload, list/detail/download-url by documents.read and
    # delete by documents.delete; every route is org-scoped (X-Org-Id).
    _route(
        "POST",
        "/api/v1/files",
        org_scoped=True,
        request_body={
            "original_filename": "report.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1024,
        },
    ),
    _route(
        "POST",
        "/api/v1/files/{file_id}/complete",
        org_scoped=True,
        path_values={"file_id": _FILE_ID},
    ),
    _route("GET", "/api/v1/files", org_scoped=True),
    _route(
        "GET",
        "/api/v1/files/{file_id}",
        org_scoped=True,
        path_values={"file_id": _FILE_ID},
    ),
    _route(
        "GET",
        "/api/v1/files/{file_id}/download-url",
        org_scoped=True,
        path_values={"file_id": _FILE_ID},
    ),
    _route(
        "DELETE",
        "/api/v1/files/{file_id}",
        org_scoped=True,
        path_values={"file_id": _FILE_ID},
    ),
    # Jobs (Scope §6.5): status/progress polling is gated by documents.read —
    # the files module is the only job producer in v0.5, so the job endpoints
    # reuse its gate. Both routes are org-scoped (X-Org-Id); a foreign job id
    # is a 404. There is no job write surface: the durable row is written by
    # the service, never by a client.
    _route("GET", "/api/v1/jobs", org_scoped=True),
    _route(
        "GET",
        "/api/v1/jobs/{job_id}",
        org_scoped=True,
        path_values={"job_id": _JOB_ID},
    ),
    # Notifications (Scope §6.3): the four org-scoped notification routes.
    # List and unread-count need notifications.read; mark-read also
    # notifications.read (a foreign or other-user id is a 404); test-send needs
    # notifications.manage and takes no body (the content is server-owned).
    _route("GET", "/api/v1/notifications", org_scoped=True),
    _route("GET", "/api/v1/notifications/unread-count", org_scoped=True),
    _route(
        "PATCH",
        "/api/v1/notifications/{notification_id}/read",
        org_scoped=True,
        path_values={"notification_id": _NOTIFICATION_ID},
    ),
    _route("PATCH", "/api/v1/notifications/read-all", org_scoped=True),
    _route("POST", "/api/v1/notifications/test", org_scoped=True),
    _route("GET", "/api/v1/platform/audit-events", org_scoped=False),
    _route("GET", "/api/v1/platform/admins", org_scoped=False),
    _route("GET", "/api/v1/platform/users", org_scoped=False),
    _route(
        "POST",
        "/api/v1/platform/admins",
        org_scoped=False,
        request_body={"user_id": str(uuid.uuid4())},
    ),
    _route(
        "DELETE",
        "/api/v1/platform/admins/{platform_membership_id}",
        org_scoped=False,
        path_values={"platform_membership_id": _MEMBERSHIP_ID},
    ),
    _route(
        "POST",
        "/api/v1/platform/organisations",
        org_scoped=False,
        request_body={"name": "Acme"},
    ),
    # Organisation administration (Scope §6.9): list, view and rename are
    # platform-gated and never org-scoped; the organisation id comes from the
    # path, never from a request body.
    _route("GET", "/api/v1/platform/organisations", org_scoped=False),
    _route(
        "GET",
        "/api/v1/platform/organisations/{organisation_id}",
        org_scoped=False,
        path_values={"organisation_id": _ORG_ID},
    ),
    _route(
        "PATCH",
        "/api/v1/platform/organisations/{organisation_id}",
        org_scoped=False,
        request_body={"name": "Renamed"},
        path_values={"organisation_id": _ORG_ID},
    ),
    # Invitation endpoints (Scope §6.5): platform-gated, never org-scoped;
    # the organisation and invitation ids come from the path.
    _route(
        "POST",
        "/api/v1/platform/organisations/{organisation_id}/invitations",
        org_scoped=False,
        request_body={"email": "invitee@example.com", "role_code": "member"},
        path_values={"organisation_id": _ORG_ID},
    ),
    _route(
        "GET",
        "/api/v1/platform/organisations/{organisation_id}/invitations",
        org_scoped=False,
        path_values={"organisation_id": _ORG_ID},
    ),
    _route(
        "DELETE",
        "/api/v1/platform/organisations/{organisation_id}/invitations/{invitation_id}",
        org_scoped=False,
        path_values={"organisation_id": _ORG_ID, "invitation_id": _INVITATION_ID},
    ),
    # Membership administration (Scope §6.6): platform-gated, never org-scoped;
    # the organisation, membership and role ids come from the path.
    _route(
        "GET",
        "/api/v1/platform/organisations/{organisation_id}/memberships",
        org_scoped=False,
        path_values={"organisation_id": _ORG_ID},
    ),
    _route(
        "POST",
        "/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}/roles",
        org_scoped=False,
        request_body={"role_code": "member"},
        path_values={"organisation_id": _ORG_ID, "membership_id": _MEMBERSHIP_ID},
    ),
    _route(
        "DELETE",
        "/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}/roles/{role_code}",
        org_scoped=False,
        path_values={
            "organisation_id": _ORG_ID,
            "membership_id": _MEMBERSHIP_ID,
            "role_code": "member",
        },
    ),
    _route(
        "PATCH",
        "/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}/status",
        org_scoped=False,
        request_body={"status": "suspended"},
        path_values={"organisation_id": _ORG_ID, "membership_id": _MEMBERSHIP_ID},
    ),
    _route(
        "DELETE",
        "/api/v1/platform/organisations/{organisation_id}/memberships/{membership_id}",
        org_scoped=False,
        path_values={"organisation_id": _ORG_ID, "membership_id": _MEMBERSHIP_ID},
    ),
    # Feature flags (Scope §6.7): platform-gated, never org-scoped; the
    # feature key comes from the path and the organisation id from the PUT body
    # (the platform plane has no X-Org-Id).
    _route("GET", "/api/v1/platform/feature-flags", org_scoped=False),
    _route(
        "PUT",
        "/api/v1/platform/feature-flags/{feature_key}",
        org_scoped=False,
        request_body={"organisation_id": _FEATURE_FLAG_ORG_ID, "enabled": True},
        path_values={"feature_key": "records.deletion"},
    ),
    # AI organisation settings (Scope §6.5, v0.8 Scope §6.2): platform-gated,
    # never org-scoped; the organisation id comes from the path and the policy
    # from the PUT body. The PUT body carries only management fields (enabled,
    # allowlists, overrides, budget, retention, transfer policy) — no provider
    # credentials, transfer-mode provider identifiers or raw provider
    # configuration ever crosses the API (ADR-0017, BP §27).
    _route(
        "GET",
        "/api/v1/platform/organisations/{organisation_id}/ai-settings",
        org_scoped=False,
        path_values={"organisation_id": _AI_SETTINGS_ORG_ID},
    ),
    _route(
        "PUT",
        "/api/v1/platform/organisations/{organisation_id}/ai-settings",
        org_scoped=False,
        request_body={
            "version": 1,
            "enabled": True,
            "allowed_provider_ids": ["fake"],
            "allowed_model_ids": [],
            "provider_override": None,
            "model_override": None,
            "monthly_budget": None,
            "retention_policy_days": None,
            "allowed_transfer_modes": ["inline"],
            "max_large_attachment_bytes": 50000000,
        },
        path_values={"organisation_id": _AI_SETTINGS_ORG_ID},
    ),
    # AI classification demonstration (Scope §6.6): the demo endpoint is
    # org-scoped and gated by existing document permissions — triggering a
    # classification is a document action (documents.upload, member and above;
    # a read-only viewer is denied), and reading a durable result is a read
    # (documents.read). There is no generic arbitrary-prompt surface: the only
    # exposed task is the checked-in document.classify demonstration. The async
    # acknowledgement is returned by the same POST based on the input form.
    _route(
        "POST",
        "/api/v1/ai/classify",
        org_scoped=True,
        request_body={
            "storage_reference": "organisations/00000000-0000-7000-8000-000000000000/ai/scratch/doc.txt"
        },
    ),
    _route(
        "GET",
        "/api/v1/ai/classify/requests/{request_id}",
        org_scoped=True,
        path_values={"request_id": _AI_REQUEST_ID},
    ),
    # AI document QA demonstration (v0.8 Scope §2.2/§6.4): synchronous only,
    # org-scoped and gated by the existing documents.upload permission like
    # every document action; there is no arbitrary-prompt surface.
    _route(
        "POST",
        "/api/v1/ai/ask",
        org_scoped=True,
        request_body={
            "storage_reference": "organisations/00000000-0000-7000-8000-000000000000/ai/scratch/doc.txt",
            "question": "What is this document about?",
        },
    ),
    # AI demo transient upload surface (v0.8 Scope §2.2/§6.5): the scratch
    # upload intent and completion are org-scoped document actions gated by
    # documents.upload; the completion path value is a server-generated id.
    _route(
        "POST",
        "/api/v1/ai/scratch/uploads",
        org_scoped=True,
        request_body={
            "original_filename": "lease.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1024,
        },
    ),
    _route(
        "POST",
        "/api/v1/ai/scratch/uploads/{upload_id}/complete",
        org_scoped=True,
        path_values={"upload_id": "019ff4f4-add6-759e-aa21-7227a3609da8"},
    ),
]

# Signature-gated surface (Scope §6.8): the WorkOS webhook route is protected
# by the HMAC-SHA256 ``workos-signature`` header, not by a session token, so it
# is deliberately absent from PROTECTED_ROUTES and the session-based matrix
# below. It is still counted by the completeness guard and gets its own
# signature security tests (missing/invalid/replayed signature -> 401, no
# stack-trace exposure, a Bearer token never substitutes for a signature).
SIGNATURE_GATED_ROUTES: list[RouteSpec] = [
    _route("POST", "/api/v1/webhooks/workos", org_scoped=False),
]

_ORG_SCOPED_ROUTES = [route for route in PROTECTED_ROUTES if route.org_scoped]
# Platform routes: every route under /api/v1/platform. They are never
# org-scoped — the platform plane operates across organisations and takes no
# X-Org-Id (Scope §6.2).
_PLATFORM_ROUTES = [
    route for route in PROTECTED_ROUTES if route.path.startswith("/api/v1/platform")
]
# Tenant-scoped write routes: the ones a viewer (read-only bundle) must be
# denied from. POST /organisations is a bootstrap endpoint (auth-only) and
# intentionally not in this set.
_WRITE_ROUTES = [
    route for route in _ORG_SCOPED_ROUTES if route.method in {"POST", "PATCH", "DELETE"}
]


def _url(spec: RouteSpec) -> str:
    path = spec.path
    for name, value in spec.path_values.items():
        path = path.replace("{" + name + "}", value)
    return path


def _headers(token: str | None, org_id: uuid.UUID | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if org_id is not None:
        headers["X-Org-Id"] = str(org_id)
    return headers


def _assert_standard_error(response: Response, status: int, code: str) -> dict[str, Any]:
    """Assert the standard envelope (BP §13) and that it leaks nothing."""
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]
    assert body["details"] is None or isinstance(body["details"], list)
    assert body["request_id"]
    assert not any(marker in response.text for marker in _TRACEBACK_MARKERS)
    return body


@pytest.fixture
def app_with_state() -> tuple[FastAPI, ContextState]:
    """A fresh app with in-memory context fakes, sharing the module RSA key."""
    state = ContextState(owner_role=make_owner_role())
    app = build_context_app(private_key=_PRIVATE_KEY, state=state)
    return app, state


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def _request(
    client: AsyncClient,
    spec: RouteSpec,
    *,
    token: str | None,
    org_id: uuid.UUID | None = None,
) -> Response:
    return await client.request(
        spec.method,
        _url(spec),
        json=spec.request_body,
        headers=_headers(token, org_id),
    )


# --- Completeness guard: the table and the registered surface stay in sync ---


def _iter_http_routes(app: FastAPI) -> Iterator[Route]:
    """Yield every concrete HTTP route, unwrapping FastAPI's deferred inclusion.

    FastAPI 0.141+ registers ``include_router`` calls as lazy ``_IncludedRouter``
    objects instead of flattening their routes into ``app.routes``, so a plain
    walk would miss all ``/api/v1`` endpoints.
    """
    for route in app.routes:
        if isinstance(route, Route):
            yield route
            continue
        included = cast(Any, route)
        # Lazy inclusion on FastAPI 0.141+; fall back to plain router objects
        # holding ``routes`` in case the internal attribute is ever renamed.
        router = getattr(included, "original_router", None) or getattr(included, "routes", None)
        if router is None:
            continue
        yield from (sub for sub in router.routes if isinstance(sub, Route))


def test_no_protected_route_is_left_out() -> None:
    """Every /api/v1 route must appear in PROTECTED_ROUTES (BP §31 reusability)."""
    # Public endpoints (e.g. /health, /ready) must stay outside /api/v1 so this
    # prefix filter remains the single source of truth for what is protected.
    # Signature-gated webhook routes are counted too, though they are held in a
    # separate table because the session-based matrix does not apply to them.
    registered: set[tuple[str, str]] = set()
    for route in _iter_http_routes(create_app()):
        if not route.path.startswith("/api/v1"):
            continue
        for method in route.methods or ():
            if method in {"GET", "POST", "PATCH", "DELETE", "PUT"}:
                registered.add((method, route.path))
    expected = {(spec.method, spec.path) for spec in PROTECTED_ROUTES} | {
        (spec.method, spec.path) for spec in SIGNATURE_GATED_ROUTES
    }
    assert registered == expected


# --- Mandatory properties, parametrised over the whole protected surface ---


@pytest.mark.parametrize("spec", PROTECTED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_unauthenticated_rejected(
    app_with_state: tuple[FastAPI, ContextState], spec: RouteSpec
) -> None:
    """BP §31 / acceptance §5.8: no token on a protected route is a 401."""
    app, _state = app_with_state
    async with _client(app) as client:
        response = await _request(client, spec, token=None)
    _assert_standard_error(response, 401, "invalid_token")


@pytest.mark.parametrize("spec", PROTECTED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_garbage_token_rejected(
    app_with_state: tuple[FastAPI, ContextState], spec: RouteSpec
) -> None:
    """A token that is not a JWT is rejected with 401 on every route."""
    app, _state = app_with_state
    async with _client(app) as client:
        response = await _request(client, spec, token="not-a-jwt")
    _assert_standard_error(response, 401, "invalid_session")


@pytest.mark.parametrize("spec", PROTECTED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
@pytest.mark.parametrize(("token_id", "token"), BAD_TOKENS, ids=[label for label, _ in BAD_TOKENS])
async def test_invalid_sessions_rejected(
    app_with_state: tuple[FastAPI, ContextState],
    spec: RouteSpec,
    token_id: str,
    token: str,
) -> None:
    """Acceptance §5.1/§5.8: wrong signature, issuer, audience or expiry is a 401."""
    app, _state = app_with_state
    async with _client(app) as client:
        response = await _request(client, spec, token=token)
    _assert_standard_error(response, 401, "invalid_session")


@pytest.mark.parametrize("spec", PROTECTED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_disabled_user_rejected(
    app_with_state: tuple[FastAPI, ContextState], spec: RouteSpec
) -> None:
    """Acceptance §5.6: a disabled user is blocked even with a valid session."""
    app, state = app_with_state
    disabled = make_user(is_active=False)
    state.users[disabled.workos_user_id] = disabled
    state.lookup_queue = [disabled]

    async with _client(app) as client:
        response = await _request(client, spec, token=make_token(_PRIVATE_KEY))
    _assert_standard_error(response, 403, "user_disabled")


@pytest.mark.parametrize("spec", _ORG_SCOPED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_missing_org_context_rejected(
    app_with_state: tuple[FastAPI, ContextState], spec: RouteSpec
) -> None:
    """Acceptance §5.4: a tenant-scoped route without X-Org-Id is a 400."""
    app, state = app_with_state
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]

    async with _client(app) as client:
        response = await _request(client, spec, token=make_token(_PRIVATE_KEY))
    _assert_standard_error(response, 400, "org_context_required")


@pytest.mark.parametrize("spec", _ORG_SCOPED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_malformed_org_context_rejected(
    app_with_state: tuple[FastAPI, ContextState], spec: RouteSpec
) -> None:
    """Acceptance §5.4: a malformed X-Org-Id is a 400, never a 500."""
    app, state = app_with_state
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]

    async with _client(app) as client:
        response = await client.request(
            spec.method,
            _url(spec),
            json=spec.request_body,
            headers=_headers(make_token(_PRIVATE_KEY)) | {"X-Org-Id": "not-a-uuid"},
        )
    _assert_standard_error(response, 400, "invalid_org_id")


@pytest.mark.parametrize("spec", _ORG_SCOPED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_cross_organisation_access_denied(
    app_with_state: tuple[FastAPI, ContextState], spec: RouteSpec
) -> None:
    """Acceptance §5.4/§5.8: an org the user does not belong to is a 403."""
    app, state = app_with_state
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user, None]  # user found, no membership for the org

    other_org_id = uuid.uuid4()
    async with _client(app) as client:
        response = await _request(client, spec, token=make_token(_PRIVATE_KEY), org_id=other_org_id)
    _assert_standard_error(response, 403, "not_a_member")


@pytest.mark.parametrize("spec", _WRITE_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_viewer_writes_denied(
    app_with_state: tuple[FastAPI, ContextState], spec: RouteSpec
) -> None:
    """Acceptance §5.5/§5.8: a viewer (read-only bundle) gets 403 on every write."""
    app, state = app_with_state
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    state.lookup_queue = [user, membership]
    state.granted_permissions = {"records.read"}  # the viewer bundle

    async with _client(app) as client:
        response = await _request(client, spec, token=make_token(_PRIVATE_KEY), org_id=org_id)
    _assert_standard_error(response, 403, "permission_denied")


# --- Platform plane (Scope §6.2): a separate authorisation layer, not a bypass ---


@pytest.mark.parametrize("spec", _PLATFORM_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_non_platform_admin_rejected(
    app_with_state: tuple[FastAPI, ContextState], spec: RouteSpec
) -> None:
    """Acceptance §5.2: an org owner without platform membership gets 403.

    The granted bundle here is the organisation-owner bundle (org codes only);
    the platform dependency must reject it with ``platform_admin_required`` —
    org authorisation never grants platform access, even to an owner.
    """
    app, state = app_with_state
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    state.granted_permissions = {"organisation.manage", "users.manage_roles", "records.read"}

    async with _client(app) as client:
        response = await _request(client, spec, token=make_token(_PRIVATE_KEY))
    _assert_standard_error(response, 403, "platform_admin_required")


async def test_platform_admin_without_org_membership_denied_on_org_routes(
    app_with_state: tuple[FastAPI, ContextState],
) -> None:
    """Acceptance §5.2: platform access never implies organisation access.

    A platform admin with no organisation membership is rejected by an
    org-scoped route with the standard ``not_a_member`` 403 — the platform
    plane is not a global bypass of the organisation permission system.
    """
    app, state = app_with_state
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user, None]  # user found, no membership for the org
    state.granted_permissions = {"platform.admin"}  # the platform bundle only

    other_org_id = uuid.uuid4()
    async with _client(app) as client:
        response = await client.get(
            "/api/v1/records",
            headers=_headers(make_token(_PRIVATE_KEY), other_org_id),
        )
    _assert_standard_error(response, 403, "not_a_member")


# --- Signature-gated webhook surface (Scope §6.8) ---
#
# The WorkOS webhook route is protected by signature, not by a session token,
# so the session-based matrix above does not apply. These tests pin its
# security contract: missing/invalid/replayed signatures and an unset secret
# are 401, a Bearer token never substitutes for a signature, and the standard
# error envelope leaks nothing.


def _patched_webhook_settings(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    """Point the dependencies module's settings accessor at a webhook secret."""
    monkeypatch.setattr(
        dependencies_module,
        "get_settings",
        lambda: SimpleNamespace(workos_webhook_secret=secret),
    )


@pytest.mark.parametrize("spec", SIGNATURE_GATED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_webhook_missing_signature_rejected(
    app_with_state: tuple[FastAPI, ContextState],
    spec: RouteSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery without a workos-signature header is a 401 (acceptance §5.9)."""
    _patched_webhook_settings(monkeypatch, "whsec_suite")
    app, _state = app_with_state
    async with _client(app) as client:
        response = await client.request(spec.method, _url(spec), json={"event": "unknown.event"})
    _assert_standard_error(response, 401, "invalid_webhook_signature")


@pytest.mark.parametrize("spec", SIGNATURE_GATED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_webhook_invalid_signature_rejected(
    app_with_state: tuple[FastAPI, ContextState],
    spec: RouteSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery signed with the wrong secret is a 401 (acceptance §5.9)."""
    _patched_webhook_settings(monkeypatch, "whsec_suite")
    app, _state = app_with_state
    payload = b'{"event":"invitation.revoked","data":{"id":"inv_1"}}'
    header = webhook_signature_header(payload, "whsec_wrong", int(time.time() * 1000))
    async with _client(app) as client:
        response = await client.post(
            _url(spec), content=payload, headers={"workos-signature": header}
        )
    _assert_standard_error(response, 401, "invalid_webhook_signature")


@pytest.mark.parametrize("spec", SIGNATURE_GATED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_webhook_replayed_signature_rejected(
    app_with_state: tuple[FastAPI, ContextState],
    spec: RouteSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid signature from beyond the 300s tolerance is a 401 (replay defence)."""
    _patched_webhook_settings(monkeypatch, "whsec_suite")
    app, _state = app_with_state
    payload = b'{"event":"invitation.revoked","data":{"id":"inv_1"}}'
    stale_ms = int(time.time() * 1000) - 10 * 60 * 1000  # ten minutes ago
    header = webhook_signature_header(payload, "whsec_suite", stale_ms)
    async with _client(app) as client:
        response = await client.post(
            _url(spec), content=payload, headers={"workos-signature": header}
        )
    _assert_standard_error(response, 401, "invalid_webhook_signature")


@pytest.mark.parametrize("spec", SIGNATURE_GATED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_webhook_rejects_unset_secret_fail_closed(
    app_with_state: tuple[FastAPI, ContextState], spec: RouteSpec
) -> None:
    """Without WORKOS_WEBHOOK_SECRET every delivery is rejected (fail-closed)."""
    app, _state = app_with_state
    payload = b'{"event":"unknown.event"}'
    header = webhook_signature_header(payload, "any-secret", int(time.time() * 1000))
    async with _client(app) as client:
        response = await client.post(
            _url(spec), content=payload, headers={"workos-signature": header}
        )
    _assert_standard_error(response, 401, "invalid_webhook_signature")


@pytest.mark.parametrize("spec", SIGNATURE_GATED_ROUTES, ids=lambda s: f"{s.method} {s.path}")
async def test_webhook_bearer_token_never_substitutes_for_signature(
    app_with_state: tuple[FastAPI, ContextState],
    spec: RouteSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session token in the Authorization header is not a webhook signature."""
    _patched_webhook_settings(monkeypatch, "whsec_suite")
    app, _state = app_with_state
    async with _client(app) as client:
        response = await client.request(
            spec.method,
            _url(spec),
            json={"event": "unknown.event"},
            headers={"Authorization": f"Bearer {make_token(_PRIVATE_KEY)}"},
        )
    _assert_standard_error(response, 401, "invalid_webhook_signature")


# --- Stack traces are never exposed (BP §13, acceptance §5.8) ---


async def test_unexpected_exception_on_protected_route_is_not_leaked() -> None:
    """A handler crash after full context resolution returns the generic 500."""
    state = ContextState(owner_role=make_owner_role())
    app = build_context_app(private_key=_PRIVATE_KEY, state=state)

    async def _broken(
        membership: Annotated[OrganisationMembership, Depends(get_current_membership)],
    ) -> None:
        raise RuntimeError("internal secret detail")

    app.add_api_route("/api/v1/_test/broken", _broken, methods=["GET"])

    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    state.lookup_queue = [user, make_membership(user, org_id)]

    async with _client(app) as client:
        response = await client.get(
            "/api/v1/_test/broken",
            headers=_headers(make_token(_PRIVATE_KEY), org_id),
        )
    body = _assert_standard_error(response, 500, "internal_error")
    assert body["message"] == "An unexpected error occurred."
