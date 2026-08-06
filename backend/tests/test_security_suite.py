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

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Annotated, Any, cast

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from starlette.routing import Route
from tests.auth_helpers import generate_key_pair, make_token
from tests.context_helpers import (
    ContextState,
    build_context_app,
    make_membership,
    make_owner_role,
    make_user,
)

from app.api.dependencies import get_current_membership
from app.main import create_app
from app.modules.organisations.models import OrganisationMembership

# Markers that must never appear in a serialised error response (BP §13,
# acceptance §5.8 "stack traces not exposed").
_TRACEBACK_MARKERS = ("Traceback", 'File "', "line ", "internal secret detail")

_RECORD_ID = str(uuid.uuid4())

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
]

_ORG_SCOPED_ROUTES = [route for route in PROTECTED_ROUTES if route.org_scoped]
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
    registered: set[tuple[str, str]] = set()
    for route in _iter_http_routes(create_app()):
        if not route.path.startswith("/api/v1"):
            continue
        for method in route.methods or ():
            if method in {"GET", "POST", "PATCH", "DELETE", "PUT"}:
                registered.add((method, route.path))
    expected = {(spec.method, spec.path) for spec in PROTECTED_ROUTES}
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
