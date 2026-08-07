"""Tests for platform-controlled organisation feature flags (Scope §6.7).

Mirrors the split of the neighbouring platform modules:

- metadata, catalogue and query-construction checks are pure Python and run
  everywhere;
- request-flow tests drive the full ASGI stack with the in-memory fakes from
  ``context_helpers.py``, proving the catalogue listing (with and without an
  organisation filter), the override PUT (including the 404s and the audit
  write) and the default-off enforcement of ``records.deletion`` against the
  records delete path — all without PostgreSQL or a network;
- the real-database proofs (persistence, the unique pair, the enforcement
  helper against real rows) live in ``test_feature_flags_db.py``.

Queue notes for the fakes: the platform permission check consumes the
``scalars_queue`` if it is non-empty, so platform-route tests keep it empty
and stage the actor and organisation in ``lookup_queue`` / ``state``, letting
the entity-based answers in ``FakeSession.scalars`` serve the feature-flag
queries (which re-filter in Python, exactly like the membership and
invitation services).
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from sqlalchemy import Table, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from tests.auth_helpers import generate_key_pair, make_token
from tests.context_helpers import (
    ContextState,
    FakeSession,
    build_context_app,
    context_client,
    make_membership,
    make_organisation,
    make_organisation_feature,
    make_record,
    make_user,
)

from app.core.exceptions import PermissionDenied
from app.core.feature_flags import (
    FEATURE_FLAG_CATALOGUE,
    FEATURE_RECORDS_DELETION,
    feature_flag_definition,
    is_feature_enabled,
)
from app.modules.audit.service import ACTION_FEATURE_FLAG_CHANGED
from app.modules.feature_flags.models import OrganisationFeature
from app.modules.feature_flags.queries import (
    organisation_feature_statement,
    organisation_features_statement,
)
from app.modules.records import service as records_service
from app.modules.users.models import User


def _make_platform_admin_user(state: ContextState) -> User:
    user = make_user(workos_user_id="user_platform_admin")
    state.users[user.workos_user_id] = user
    return user


# --- Model, catalogue and query metadata (Scope §6.7) ---


def test_organisation_features_unique_pair() -> None:
    """The unique (organisation_id, feature_key) pair is the model invariant."""
    table = cast(Table, OrganisationFeature.__table__)
    constraint = next(
        constraint for constraint in table.constraints if isinstance(constraint, UniqueConstraint)
    )
    assert set(constraint.columns.keys()) == {"organisation_id", "feature_key"}


def test_catalogue_records_deletion_is_default_off() -> None:
    """Scope §6.7 / acceptance §5.8: every v0.4 flag defaults to off."""
    definition = feature_flag_definition(FEATURE_RECORDS_DELETION)
    assert definition is not None
    assert definition.key == "records.deletion"
    assert definition.default_enabled is False
    # The catalogue is the closed set of known flags: unknown keys are absent.
    assert feature_flag_definition("no.such_flag") is None
    assert all(item.default_enabled is False for item in FEATURE_FLAG_CATALOGUE)


def test_feature_flag_action_is_stable() -> None:
    """Blueprint §29 / design plan §3.2: the audited action code is exact."""
    assert ACTION_FEATURE_FLAG_CHANGED == "feature_flag.changed"


def test_feature_statements_carry_the_where_clauses() -> None:
    org_id = uuid.uuid4()
    single = organisation_feature_statement(organisation_id=org_id, feature_key="records.deletion")
    compiled = str(single.compile(dialect=postgresql.dialect())).lower()
    assert "organisation_features.organisation_id" in compiled
    assert "organisation_features.feature_key" in compiled
    all_rows = organisation_features_statement(organisation_id=org_id)
    compiled_all = str(all_rows.compile(dialect=postgresql.dialect())).lower()
    assert "organisation_features.organisation_id" in compiled_all


# --- The enforcement helper (blueprint §27) ---


async def test_is_feature_enabled_defaults_off_without_override() -> None:
    """No override row means the flag is off (default deny)."""
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    enabled = await is_feature_enabled(
        session,
        organisation_id=uuid.uuid4(),
        feature_key=FEATURE_RECORDS_DELETION,
    )
    assert enabled is False


async def test_is_feature_enabled_follows_the_override_row() -> None:
    state = ContextState()
    org_id = uuid.uuid4()
    state.feature_flags = [
        make_organisation_feature(org_id, feature_key=FEATURE_RECORDS_DELETION, enabled=True)
    ]
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    assert (
        await is_feature_enabled(
            session, organisation_id=org_id, feature_key=FEATURE_RECORDS_DELETION
        )
    ) is True
    # An explicit row with enabled=false is still off (fresh session, so the
    # per-session memo does not hide the change).
    state.feature_flags = [
        make_organisation_feature(org_id, feature_key=FEATURE_RECORDS_DELETION, enabled=False)
    ]
    fresh: AsyncSession = cast(AsyncSession, FakeSession(state))
    assert (
        await is_feature_enabled(
            fresh, organisation_id=org_id, feature_key=FEATURE_RECORDS_DELETION
        )
    ) is False


async def test_is_feature_enabled_is_org_isolated() -> None:
    """Acceptance §5.8: one organisation's override never leaks to another."""
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    state.feature_flags = [
        make_organisation_feature(org_a, feature_key=FEATURE_RECORDS_DELETION, enabled=True)
    ]
    assert (
        await is_feature_enabled(
            session, organisation_id=org_a, feature_key=FEATURE_RECORDS_DELETION
        )
    ) is True
    assert (
        await is_feature_enabled(
            session, organisation_id=org_b, feature_key=FEATURE_RECORDS_DELETION
        )
    ) is False


async def test_is_feature_enabled_unknown_key_is_off() -> None:
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    assert (
        await is_feature_enabled(session, organisation_id=uuid.uuid4(), feature_key="no.such_flag")
    ) is False


async def test_is_feature_enabled_memoises_per_session() -> None:
    """The cache-friendly property: one request reads each flag once."""
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    org_id = uuid.uuid4()
    # First check resolves to off and is memoised...
    assert (
        await is_feature_enabled(
            session, organisation_id=org_id, feature_key=FEATURE_RECORDS_DELETION
        )
    ) is False
    # ...so a row appearing later in the same session does not change the
    # answer (a request never flips flags mid-flight)...
    state.feature_flags = [
        make_organisation_feature(org_id, feature_key=FEATURE_RECORDS_DELETION, enabled=True)
    ]
    assert (
        await is_feature_enabled(
            session, organisation_id=org_id, feature_key=FEATURE_RECORDS_DELETION
        )
    ) is False
    # ...while a fresh session sees the committed state.
    fresh: AsyncSession = cast(AsyncSession, FakeSession(state))
    assert (
        await is_feature_enabled(
            fresh, organisation_id=org_id, feature_key=FEATURE_RECORDS_DELETION
        )
    ) is True


# --- Default-off enforcement inside a service (blueprint §27) ---


async def test_delete_record_blocked_when_flag_is_off() -> None:
    """Acceptance §5.8: without an override the destructive path is denied."""
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    org_id = uuid.uuid4()
    record = make_record(org_id)
    state.records = [record]
    # get_record resolves the record first; the flag check then denies.
    state.lookup_queue = [record]

    with pytest.raises(PermissionDenied) as excinfo:
        await records_service.delete_record(
            session,
            organisation_id=org_id,
            record_id=record.id,
        )
    assert excinfo.value.code == "feature_disabled"


async def test_delete_record_allowed_once_platform_enables_flag() -> None:
    state = ContextState()
    session: AsyncSession = cast(AsyncSession, FakeSession(state))
    org_id = uuid.uuid4()
    record = make_record(org_id)
    state.records = [record]
    state.feature_flags = [
        make_organisation_feature(org_id, feature_key=FEATURE_RECORDS_DELETION, enabled=True)
    ]
    state.lookup_queue = [record]

    await records_service.delete_record(
        session,
        organisation_id=org_id,
        record_id=record.id,
    )
    assert state.records == []


# --- Platform catalogue listing endpoint ---


def _platform_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_list_feature_flags_returns_catalogue_defaults() -> None:
    state = ContextState()
    actor = _make_platform_admin_user(state)
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/platform/feature-flags", headers=_platform_headers(make_token(private_key))
        )

    assert response.status_code == 200
    body = response.json()
    items = {item["feature_key"]: item for item in body["items"]}
    deletion = items["records.deletion"]
    assert deletion["default_enabled"] is False
    assert deletion["enabled"] is False
    assert deletion["overridden"] is False
    assert deletion["configuration_json"] is None
    assert "name" in deletion and "description" in deletion


async def test_list_feature_flags_merges_org_overrides() -> None:
    state = ContextState()
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, organisation]  # actor, then the org lookup
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)
    state.feature_flags = [
        make_organisation_feature(
            organisation.id, feature_key=FEATURE_RECORDS_DELETION, enabled=True
        )
    ]

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/platform/feature-flags",
            params={"organisation_id": str(organisation.id)},
            headers=_platform_headers(make_token(private_key)),
        )

    assert response.status_code == 200
    items = {item["feature_key"]: item for item in response.json()["items"]}
    deletion = items["records.deletion"]
    assert deletion["enabled"] is True
    assert deletion["overridden"] is True
    assert deletion["configuration_json"] == {}


async def test_list_feature_flags_unknown_organisation_is_404() -> None:
    state = ContextState()
    actor = _make_platform_admin_user(state)
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, None]  # organisation not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/platform/feature-flags",
            params={"organisation_id": str(uuid.uuid4())},
            headers=_platform_headers(make_token(private_key)),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "organisation_not_found"


async def test_list_feature_flags_requires_platform_admin() -> None:
    """Scope §6.2: an org owner without platform membership is denied."""
    state = ContextState()
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    state.granted_permissions = {"organisation.manage", "users.manage_roles", "records.read"}
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.get(
            "/api/v1/platform/feature-flags", headers=_platform_headers(make_token(private_key))
        )

    assert response.status_code == 403
    assert response.json()["code"] == "platform_admin_required"


# --- Override PUT endpoint ---


async def test_put_feature_flag_sets_override_and_audits() -> None:
    """Scope §6.7: the upsert round-trips and writes feature_flag.changed."""
    state = ContextState()
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, organisation]  # actor, then the org lookup
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.put(
            f"/api/v1/platform/feature-flags/{FEATURE_RECORDS_DELETION}",
            json={
                "organisation_id": str(organisation.id),
                "enabled": True,
                "configuration_json": {"require_confirmation": True},
            },
            headers=_platform_headers(make_token(private_key)),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["feature_key"] == FEATURE_RECORDS_DELETION
    assert body["enabled"] is True
    assert body["overridden"] is True
    assert body["configuration_json"] == {"require_confirmation": True}

    # The override row was persisted and the audit event written.
    assert len(state.feature_flags) == 1
    assert state.feature_flags[0].enabled is True
    assert state.audit_events[-1].action == ACTION_FEATURE_FLAG_CHANGED
    assert state.audit_events[-1].resource_type == "feature_flag"
    assert state.audit_events[-1].resource_id == FEATURE_RECORDS_DELETION
    assert state.audit_events[-1].actor_user_id == actor.id


async def test_put_feature_flag_unknown_key_is_404() -> None:
    state = ContextState()
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, organisation]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.put(
            "/api/v1/platform/feature-flags/no.such_flag",
            json={"organisation_id": str(organisation.id), "enabled": True},
            headers=_platform_headers(make_token(private_key)),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "feature_flag_unknown"
    assert state.feature_flags == []


async def test_put_feature_flag_unknown_organisation_is_404() -> None:
    state = ContextState()
    actor = _make_platform_admin_user(state)
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, None]  # organisation not found
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.put(
            f"/api/v1/platform/feature-flags/{FEATURE_RECORDS_DELETION}",
            json={"organisation_id": str(uuid.uuid4()), "enabled": True},
            headers=_platform_headers(make_token(private_key)),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "organisation_not_found"


async def test_put_feature_flag_requires_platform_admin() -> None:
    state = ContextState()
    user = make_user()
    state.users[user.workos_user_id] = user
    state.lookup_queue = [user]
    state.granted_permissions = {"organisation.manage", "users.manage_roles", "records.read"}
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.put(
            f"/api/v1/platform/feature-flags/{FEATURE_RECORDS_DELETION}",
            json={"organisation_id": str(uuid.uuid4()), "enabled": True},
            headers=_platform_headers(make_token(private_key)),
        )

    assert response.status_code == 403
    assert response.json()["code"] == "platform_admin_required"


async def test_put_feature_flag_rejects_extra_fields() -> None:
    """The pair is extra=forbid: no server-controlled field can be smuggled in."""
    state = ContextState()
    actor = _make_platform_admin_user(state)
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor]
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.put(
            f"/api/v1/platform/feature-flags/{FEATURE_RECORDS_DELETION}",
            json={
                "organisation_id": str(uuid.uuid4()),
                "enabled": True,
                "feature_key": "smuggled",
            },
            headers=_platform_headers(make_token(private_key)),
        )

    assert response.status_code == 422
    assert state.feature_flags == []


async def test_put_feature_flag_recovers_from_lost_race() -> None:
    """A concurrent toggle's commit wins; ours retries against the new row."""
    state = ContextState()
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, organisation]  # actor, then the org lookup
    # The concurrent writer's committed row is already visible; our first
    # commit loses the race (fail_commits) and the retry updates the row.
    state.feature_flags = [
        make_organisation_feature(
            organisation.id, feature_key=FEATURE_RECORDS_DELETION, enabled=False
        )
    ]
    state.fail_commits = 1
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.put(
            f"/api/v1/platform/feature-flags/{FEATURE_RECORDS_DELETION}",
            json={"organisation_id": str(organisation.id), "enabled": True},
            headers=_platform_headers(make_token(private_key)),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["overridden"] is True
    # Still exactly one row and exactly one audit event: the failed
    # transaction's insert and audit row were rolled back with it.
    assert len(state.feature_flags) == 1
    assert state.feature_flags[0].enabled is True
    assert [event.action for event in state.audit_events] == [ACTION_FEATURE_FLAG_CHANGED]


async def test_put_feature_flag_second_collision_is_503() -> None:
    """A second collision surfaces as a 503, never a silent no-op or a 500."""
    state = ContextState()
    actor = _make_platform_admin_user(state)
    organisation = make_organisation(workos_organisation_id="org_workos_acme")
    state.granted_permissions = {"platform.admin"}
    state.lookup_queue = [actor, organisation]
    state.fail_commits = 2  # both the initial commit and the retry collide
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.put(
            f"/api/v1/platform/feature-flags/{FEATURE_RECORDS_DELETION}",
            json={"organisation_id": str(organisation.id), "enabled": True},
            headers=_platform_headers(make_token(private_key)),
        )

    assert response.status_code == 503
    assert response.json()["code"] == "feature_flag_update_failed"
    assert state.feature_flags == []  # nothing was persisted


# --- Default-off enforcement through the records API ---


async def test_delete_record_endpoint_blocked_by_default() -> None:
    """Acceptance §5.8: no override row -> the API returns 403 feature_disabled."""
    state = ContextState()
    user = make_user()
    state.users[user.workos_user_id] = user
    org_id = uuid.uuid4()
    membership = make_membership(user, org_id)
    record = make_record(org_id)
    state.records = [record]
    state.lookup_queue = [user, membership, record]
    state.granted_permissions = {"records.read", "records.delete"}
    private_key, _ = generate_key_pair()
    app = build_context_app(private_key=private_key, state=state)

    async with context_client(app) as client:
        response = await client.delete(
            f"/api/v1/records/{record.id}",
            headers={
                "Authorization": f"Bearer {make_token(private_key)}",
                "X-Org-Id": str(org_id),
            },
        )

    assert response.status_code == 403
    assert response.json()["code"] == "feature_disabled"
    assert state.records == [record]  # nothing was deleted
