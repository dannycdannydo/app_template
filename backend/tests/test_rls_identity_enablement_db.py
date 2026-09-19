"""Real-PostgreSQL production enablement suite for the identity group (P4, 5).

Plan P4 / ``docs/rls-rollout.md`` §3 group 5. It proves the production
enablement migration (``f1a2b3c4d5e6``) that promotes the identity and
control-plane tables ``organisation_memberships``, ``membership_roles`` and
``invitations`` into the permanent policy chain:

- the canonical ``<table>_organisation_isolation`` policies are installed with
  matching ``USING``/``WITH CHECK`` and RLS is enabled **and forced** on the
  tables;
- default denial: no bound context returns no rows and fails closed for writes;
- cross-organisation read and write (select/insert/update/delete and a
  tenant-key move) are denied on every table;
- the pre-tenant user-keyed membership read (``app.user_id``) and invitee
  email-keyed invitation access work with no organisation context, and the
  ``membership_roles`` parent-existence policy follows the parent's visibility;
- the verified ``invitation.revoked`` webhook single-row bootstrap reads and
  revokes exactly the one row its provider id names;
- runtime ``UPDATE`` on ``invitations`` is column-restricted, so the invitee
  path cannot rewrite an invitation's organisation, email or role;
- the platform-plane services bind exactly the organisation they target, and
  the teardown deletes run without a bypass (user read + per-organisation
  delete);
- transaction-local context does not survive commit or pooled-connection reuse;
  and
- the migration upgrades, downgrades one revision and re-upgrades cleanly.

The suite runs against real PostgreSQL with the restricted runtime login (the
migration creates the role ``NOLOGIN``; the test grants a throwaway credential).
``migrated_database`` reverts to base at teardown, so the rollout leaves no
residue.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from alembic import command
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request
from tests.rls_helpers import (
    RUNTIME_ROLE,
    alembic_config,
    database_reachable,
    downgrade_to_base,
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_representative_identity,
    seed_two_organisation_identity,
    upgrade_to_head,
)

from app.api.dependencies import get_current_membership
from app.core.exceptions import PermissionDenied
from app.db.rls import (
    bind_invitation_provider_context,
    bind_organisation_context,
    bind_user_context,
)
from app.modules.invitations import service as invitations_service
from app.modules.organisations import service as organisations_service
from app.modules.permissions.queries import permission_codes_for_membership
from app.modules.platform_admin import service as platform_admin_service
from app.modules.users.models import User
from app.modules.users.service import get_me_payload

#: The group 4b revision this migration revises.
GROUP4B_REVISION = "d0e1f2a3b4c5"

#: Every table enabled by the group 5 migration.
IDENTITY_TABLES = ("organisation_memberships", "membership_roles", "invitations")

_MEMBERSHIP_ISOLATION = "organisation_memberships_organisation_isolation"
_MEMBERSHIP_USER = "organisation_memberships_user_isolation"
_MEMBERSHIP_ROLES_PARENT = "membership_roles_parent_isolation"
_MEMBERSHIP_ROLES_WRITE = "membership_roles_organisation_isolation"
_INVITATIONS_ISOLATION = "invitations_organisation_isolation"
_INVITATIONS_INVITEE_SELECT = "invitations_invitee_select"
_INVITATIONS_INVITEE_UPDATE = "invitations_invitee_update"
_INVITATIONS_WEBHOOK_SELECT = "invitations_webhook_provider_select"
_INVITATIONS_WEBHOOK_UPDATE = "invitations_webhook_provider_update"
_INVITATIONS_LOWER_EMAIL_INDEX = "ix_invitations_lower_email"


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Migrate a reachable PostgreSQL to head, and revert to base afterwards."""
    database_url = os.environ["DATABASE_URL"]
    if not database_reachable(database_url):
        pytest.skip("no reachable PostgreSQL at DATABASE_URL")
    upgrade_to_head()
    yield database_url
    downgrade_to_base()


@pytest.fixture
def runtime_database_url(migrated_database: str) -> str:
    """Provision the runtime login and return its database URL."""
    provision_runtime_login(migrated_database)
    return runtime_url(migrated_database)


def _detached_user(user_id: uuid.UUID) -> User:
    return User(
        id=user_id,
        workos_user_id=f"user_identity_{uuid.uuid4().hex[:10]}",
        email=f"identity-{user_id}@example.com",
        name="Identity Actor",
        is_active=True,
    )


# --- Installed production policy --------------------------------------------


async def test_production_policies_are_installed_and_forced(migrated_database: str) -> None:
    """RLS is enabled and forced with the canonical, user, parent and webhook policies."""
    expected = {
        "organisation_memberships": (_MEMBERSHIP_ISOLATION, _MEMBERSHIP_USER),
        "membership_roles": (_MEMBERSHIP_ROLES_PARENT, _MEMBERSHIP_ROLES_WRITE),
        "invitations": (
            _INVITATIONS_ISOLATION,
            _INVITATIONS_INVITEE_SELECT,
            _INVITATIONS_INVITEE_UPDATE,
            _INVITATIONS_WEBHOOK_SELECT,
            _INVITATIONS_WEBHOOK_UPDATE,
        ),
    }
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            for table, policies in expected.items():
                for policy in policies:
                    row = (
                        await connection.execute(
                            text(
                                "SELECT qual, with_check FROM pg_policies "
                                "WHERE tablename = :table AND policyname = :policy"
                            ),
                            {"table": table, "policy": policy},
                        )
                    ).one_or_none()
                    assert row is not None, f"missing {policy} on {table}"
                flags = (
                    await connection.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname = :table"
                        ),
                        {"table": table},
                    )
                ).one()
                assert flags.relrowsecurity is True, table
                assert flags.relforcerowsecurity is True, table

            # The canonical and user policies reference the fail-closed helpers.
            membership_qual = (
                await connection.execute(
                    text(
                        "SELECT qual FROM pg_policies "
                        "WHERE tablename = 'organisation_memberships' "
                        "AND policyname = :policy"
                    ),
                    {"policy": _MEMBERSHIP_ISOLATION},
                )
            ).scalar_one()
            assert "app_current_tenant_id()" in membership_qual
            user_qual = (
                await connection.execute(
                    text(
                        "SELECT qual FROM pg_policies "
                        "WHERE tablename = 'organisation_memberships' "
                        "AND policyname = :policy"
                    ),
                    {"policy": _MEMBERSHIP_USER},
                )
            ).scalar_one()
            assert "app_current_user_id()" in user_qual

            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role"),
                    {"role": RUNTIME_ROLE},
                )
            ).one()
            assert role.rolsuper is False
            assert role.rolbypassrls is False
    finally:
        await engine.dispose()


async def test_representative_query_plans_use_the_indexes(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The membership, ``/me`` and invitee lookups use their indexes, no seq scan.

    The identity tables are looked up pre-tenant by user/email and per tenant by
    organisation, so rollout principle 4 requires a realistically sized world:
    a two-row seed cannot prove the planner prefers the indexes. This seeds a
    multi-tenant identity world and runs the restricted-role ``EXPLAIN`` for
    each representative read.
    """
    seed = await seed_representative_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            plans = {
                "membership_by_user_org": (
                    "SELECT id FROM organisation_memberships "
                    "WHERE user_id = :uid AND organisation_id = :org",
                    {"uid": seed.user_a, "org": seed.org_a},
                    "organisation_memberships",
                ),
                "memberships_by_user": (
                    "SELECT id FROM organisation_memberships WHERE user_id = :uid",
                    {"uid": seed.user_a},
                    "organisation_memberships",
                ),
                "invitee_by_email": (
                    "SELECT id FROM invitations WHERE status = 'sent' AND lower(email) = :email",
                    {"email": seed.email_a},
                    _INVITATIONS_LOWER_EMAIL_INDEX,
                ),
            }
            for name, (sql, params, index_hint) in plans.items():
                plan = "\n".join(
                    row[0] for row in (await session.execute(text("EXPLAIN " + sql), params)).all()
                )
                assert "Seq Scan" not in plan, f"{name}: {plan}"
                assert index_hint in plan, f"{name}: {plan}"
            await session.rollback()

        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "EXPLAIN SELECT id FROM organisation_memberships "
                            "WHERE organisation_id = :org ORDER BY created_at DESC"
                        ),
                        {"org": seed.org_a},
                    )
                ).all()
            )
            assert "Seq Scan" not in plan, plan
            assert "organisation_memberships" in plan, plan
            await session.rollback()
    finally:
        await engine.dispose()


# --- Default denial ----------------------------------------------------------


async def test_default_denial_without_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """No bound context: zero identity rows and every write is rejected."""
    await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for table in IDENTITY_TABLES:
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO organisation_memberships "
                        "(id, user_id, organisation_id, status) "
                        "VALUES (:id, :user, :org, 'active')"
                    ),
                    {"id": uuid.uuid4(), "user": uuid.uuid4(), "org": uuid.uuid4()},
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Pre-tenant user-keyed access --------------------------------------------


async def test_user_keyed_membership_read_is_pre_tenant(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Binding only ``app.user_id`` resolves the user's memberships, roles and invitations.

    This is the ADR-0022 decision 8 pre-tenant path: ``/me``, context resolution
    and login-time linking read the user's own rows before any organisation
    exists. The parent-existence role policy follows the membership's
    visibility, and the invitee invitation policy follows the verified email.
    """
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            memberships = (
                await session.execute(text("SELECT id FROM organisation_memberships"))
            ).all()
            assert {row.id for row in memberships} == {seed.membership_a}
            grants = (await session.execute(text("SELECT id FROM membership_roles"))).all()
            assert {row.id for row in grants} == {seed.role_grant_a}
            invitations = (await session.execute(text("SELECT id FROM invitations"))).all()
            assert {row.id for row in invitations} == {seed.invitation_a}
            await session.rollback()
    finally:
        await engine.dispose()


async def test_me_and_permission_reads_work_under_rls(
    migrated_database: str, runtime_database_url: str
) -> None:
    """``/me`` and permission resolution read the identity rows under forced RLS.

    Both run on the ordinary runtime path: ``/me`` with only ``app.user_id``
    bound (the pre-tenant user-keyed policies), and permission resolution with
    the organisation and user bound by ``get_current_membership``.
    """
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            payload = await get_me_payload(session, _detached_user(seed.user_a))
            assert seed.membership_a in {entry.membership.id for entry in payload.memberships}
            assert seed.membership_b not in {entry.membership.id for entry in payload.memberships}
            own = next(
                entry for entry in payload.memberships if entry.membership.id == seed.membership_a
            )
            assert own.roles == ["owner"]
            await session.rollback()

        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await bind_user_context(session, seed.user_a)
            granted = await permission_codes_for_membership(session, seed.membership_a)
            # The owner role carries the seeded permission catalogue.
            assert granted, "owner role should grant at least one permission"
            foreign = await permission_codes_for_membership(session, seed.membership_b)
            assert foreign == set()
            await session.rollback()
    finally:
        await engine.dispose()


async def test_membership_context_dependency_works_under_rls(
    migrated_database: str, runtime_database_url: str
) -> None:
    """``get_current_membership`` resolves the pre-tenant lookup on the restricted role.

    The dependency binds ``app.user_id`` before the membership query (the
    user-keyed policy) and then binds the validated organisation. A foreign
    organisation is still rejected with the application ``403``.
    """
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            request = Request({"type": "http", "headers": [], "method": "GET", "path": "/"})
            membership = await get_current_membership(
                request,
                session,
                _detached_user(seed.user_a),
                str(seed.org_a),
            )
            assert membership.id == seed.membership_a
            await session.rollback()

        async with factory() as session:
            request = Request({"type": "http", "headers": [], "method": "GET", "path": "/"})
            with pytest.raises(PermissionDenied):
                await get_current_membership(
                    request,
                    session,
                    _detached_user(seed.user_b),
                    str(seed.org_a),
                )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_organisation_context_isolation(
    migrated_database: str, runtime_database_url: str
) -> None:
    """An organisation context admits only that organisation's rows."""
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            memberships = (
                await session.execute(text("SELECT id FROM organisation_memberships"))
            ).all()
            assert {row.id for row in memberships} == {seed.membership_a}
            grants = (await session.execute(text("SELECT id FROM membership_roles"))).all()
            assert {row.id for row in grants} == {seed.role_grant_a}
            invitations = (await session.execute(text("SELECT id FROM invitations"))).all()
            assert {row.id for row in invitations} == {seed.invitation_a}
            await session.rollback()
    finally:
        await engine.dispose()


# --- Cross-organisation read and write ---------------------------------------


async def test_cross_tenant_writes_are_denied(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Same-tenant writes succeed; every cross-tenant write is denied.

    Plan P3/P4 completion evidence ("every enabled table has real
    select/insert/update/delete policy tests"): a mismatched insert is rejected
    by ``WITH CHECK``, a foreign update/delete affects no row, a role grant for
    a foreign parent is rejected, and a tenant-key move is refused.
    """
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # A cross-tenant membership insert is rejected by WITH CHECK.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO organisation_memberships "
                        "(id, user_id, organisation_id, status) "
                        "VALUES (:id, :user, :org, 'active')"
                    ),
                    {"id": uuid.uuid4(), "user": seed.user_b, "org": seed.org_b},
                )
            await session.rollback()

        # A role grant for a foreign parent is rejected (parent not visible).
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            role_id = await session.scalar(text("SELECT id FROM roles WHERE code = 'owner'"))
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO membership_roles (id, membership_id, role_id) "
                        "VALUES (:id, :membership, :role)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "membership": seed.membership_b,
                        "role": role_id,
                    },
                )
            await session.rollback()

        # A same-tenant membership insert succeeds; its role grant too.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            new_membership = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO organisation_memberships "
                    "(id, user_id, organisation_id, status) "
                    "VALUES (:id, :user, :org, 'active')"
                ),
                {"id": new_membership, "user": seed.user_b, "org": seed.org_a},
            )
            role_id = await session.scalar(text("SELECT id FROM roles WHERE code = 'owner'"))
            await session.execute(
                text(
                    "INSERT INTO membership_roles (id, membership_id, role_id) "
                    "VALUES (:id, :membership, :role)"
                ),
                {"id": uuid.uuid4(), "membership": new_membership, "role": role_id},
            )
            await session.commit()

        # Foreign update/delete affect no row.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            updated = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE organisation_memberships SET status = 'suspended' WHERE id = :id"),
                    {"id": seed.membership_b},
                ),
            )
            assert updated.rowcount == 0
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM organisation_memberships WHERE id = :id"),
                    {"id": seed.membership_b},
                ),
            )
            assert deleted.rowcount == 0
            await session.rollback()

        # Moving a same-tenant membership into another organisation is rejected.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "UPDATE organisation_memberships SET organisation_id = :org WHERE id = :id"
                    ),
                    {"org": seed.org_b, "id": seed.membership_a},
                )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_organisation_context_own_and_cross_tenant_identity_writes(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A validated organisation may write its own grants/invitations only.

    Plan P3/P4 completion evidence for the indirect role-grant table and the
    canonical invitation policy: own-tenant ``membership_roles``/
    ``invitations`` inserts and deletes succeed, while a cross-tenant insert is
    rejected by ``WITH CHECK`` and a cross-tenant delete affects no row.
    """
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Cross-tenant role-grant delete affects no row.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM membership_roles WHERE id = :id"),
                    {"id": seed.role_grant_b},
                ),
            )
            assert deleted.rowcount == 0
            await session.rollback()

        # A cross-tenant invitation insert is rejected by WITH CHECK.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO invitations "
                        "(id, organisation_id, email, role_code, workos_invitation_id, "
                        "workos_organisation_id, invited_by_user_id, status, expires_at) "
                        "VALUES (:id, :org, :email, 'owner', :provider, :workos_org, "
                        ":inviter, 'sent', now() + interval '7 days')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org": seed.org_b,
                        "email": "cross-tenant@example.com",
                        "provider": f"invitation_{uuid.uuid4().hex}",
                        "workos_org": "org_workos_cross",
                        "inviter": seed.user_a,
                    },
                )
            await session.rollback()

        # A cross-tenant invitation delete affects no row.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM invitations WHERE id = :id"),
                    {"id": seed.invitation_b},
                ),
            )
            assert deleted.rowcount == 0
            await session.rollback()

        # Own-tenant role-grant insert/delete succeeds.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            role_id = await session.scalar(
                text("SELECT id FROM roles WHERE code = 'administrator'")
            )
            new_grant = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO membership_roles (id, membership_id, role_id) "
                    "VALUES (:id, :membership, :role)"
                ),
                {"id": new_grant, "membership": seed.membership_a, "role": role_id},
            )
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM membership_roles WHERE id = :id"), {"id": new_grant}
                ),
            )
            assert deleted.rowcount == 1
            await session.rollback()

        # Own-tenant invitation insert/delete succeeds.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            new_invitation = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO invitations "
                    "(id, organisation_id, email, role_code, workos_invitation_id, "
                    "workos_organisation_id, invited_by_user_id, status, expires_at) "
                    "VALUES (:id, :org, :email, 'owner', :provider, :workos_org, "
                    ":inviter, 'sent', now() + interval '7 days')"
                ),
                {
                    "id": new_invitation,
                    "org": seed.org_a,
                    "email": "new-member@example.com",
                    "provider": f"invitation_{uuid.uuid4().hex}",
                    "workos_org": "org_workos_new",
                    "inviter": seed.user_a,
                },
            )
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM invitations WHERE id = :id"), {"id": new_invitation}
                ),
            )
            assert deleted.rowcount == 1
            await session.commit()
    finally:
        await engine.dispose()


async def test_pre_tenant_user_context_cannot_mutate_identity_rows(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A user-only context reads its own rows but has no write authority.

    ADR-0022 decision 8's pre-tenant path binds only ``app.user_id``. It admits
    the caller's membership/role reads and the invitee status update, but must
    deny insert and delete on ``membership_roles`` (the parent-existence read
    policy never implies write authority) and on ``invitations`` (binding a
    user id grants no INSERT/DELETE), plan P4 group 5 review.
    """
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # The user-only context still reads the caller's own role grant.
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            grants = (await session.execute(text("SELECT id FROM membership_roles"))).all()
            assert {row.id for row in grants} == {seed.role_grant_a}
            await session.rollback()

        # A pre-tenant user cannot insert a role grant for its own membership.
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            role_id = await session.scalar(
                text("SELECT id FROM roles WHERE code = 'administrator'")
            )
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO membership_roles (id, membership_id, role_id) "
                        "VALUES (:id, :membership, :role)"
                    ),
                    {"id": uuid.uuid4(), "membership": seed.membership_a, "role": role_id},
                )
            await session.rollback()

        # ... and cannot delete its existing grant.
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM membership_roles WHERE id = :id"),
                    {"id": seed.role_grant_a},
                ),
            )
            assert deleted.rowcount == 0
            await session.rollback()

        # ... and cannot insert an invitation.
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO invitations "
                        "(id, organisation_id, email, role_code, workos_invitation_id, "
                        "workos_organisation_id, invited_by_user_id, status, expires_at) "
                        "VALUES (:id, :org, :email, 'owner', :provider, :workos_org, "
                        ":inviter, 'sent', now() + interval '7 days')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org": seed.org_a,
                        "email": "self@example.com",
                        "provider": f"invitation_{uuid.uuid4().hex}",
                        "workos_org": "org_workos_self",
                        "inviter": seed.user_a,
                    },
                )
            await session.rollback()

        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM invitations WHERE id = :id"),
                    {"id": seed.invitation_a},
                ),
            )
            assert deleted.rowcount == 0
            # The invitee status update still works (SELECT + UPDATE policies).
            updated = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE invitations SET status = 'revoked' WHERE id = :id"),
                    {"id": seed.invitation_a},
                ),
            )
            assert updated.rowcount == 1
            await session.rollback()
    finally:
        await engine.dispose()


async def test_provider_bootstrap_is_read_and_status_only(
    migrated_database: str, runtime_database_url: str
) -> None:
    """A provider invitation id admits a read/status flip but never INSERT/DELETE.

    The verified webhook bootstrap must not become mutation authority: binding
    ``app.invitation_provider_id`` authorises the single named row's read/lock
    and update, and ``WITH CHECK``/no-INSERT-policy denies creating or deleting
    rows (ADR-0022 decisions 3 and 4, plan P4 group 5 review).
    """
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_invitation_provider_context(session, seed.provider_invitation_a)
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO invitations "
                        "(id, organisation_id, email, role_code, workos_invitation_id, "
                        "workos_organisation_id, invited_by_user_id, status, expires_at) "
                        "VALUES (:id, :org, :email, 'owner', :provider, :workos_org, "
                        ":inviter, 'sent', now() + interval '7 days')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org": seed.org_a,
                        "email": "provider@example.com",
                        "provider": seed.provider_invitation_a,
                        "workos_org": "org_workos_provider",
                        "inviter": seed.user_a,
                    },
                )
            await session.rollback()

        async with factory() as session:
            await bind_invitation_provider_context(session, seed.provider_invitation_a)
            deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("DELETE FROM invitations WHERE workos_invitation_id = :provider"),
                    {"provider": seed.provider_invitation_a},
                ),
            )
            assert deleted.rowcount == 0
            await session.rollback()
    finally:
        await engine.dispose()


# --- Invitee and webhook invitation paths ------------------------------------


async def test_invitee_can_accept_only_their_own_invitation(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The invitee email policy admits the invitee's own row and the column grant bounds it."""
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            # The invitee cannot move an invitation's organisation: runtime
            # UPDATE is column-restricted to status/updated_at.
            with pytest.raises(ProgrammingError, match="permission denied"):
                await session.execute(
                    text("UPDATE invitations SET organisation_id = :org WHERE id = :id"),
                    {"org": seed.org_b, "id": seed.invitation_a},
                )
            await session.rollback()

        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            locked = (
                await session.execute(
                    text(
                        "SELECT id FROM invitations WHERE email = "
                        "(SELECT lower(email) FROM users WHERE id = :uid) FOR UPDATE"
                    ),
                    {"uid": seed.user_a},
                )
            ).all()
            assert {row.id for row in locked} == {seed.invitation_a}
            updated = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE invitations SET status = 'accepted' WHERE id = :id"),
                    {"id": seed.invitation_a},
                ),
            )
            assert updated.rowcount == 1
            # The foreign invitee's row is not visible to this user.
            foreign = cast(
                "CursorResult[Any]",
                await session.execute(
                    text("UPDATE invitations SET status = 'accepted' WHERE id = :id"),
                    {"id": seed.invitation_b},
                ),
            )
            assert foreign.rowcount == 0
            await session.rollback()
    finally:
        await engine.dispose()


async def test_webhook_provider_bootstrap_reads_one_row(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The verified webhook single-row bootstrap admits exactly its provider invitation."""
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_invitation_provider_context(session, seed.provider_invitation_a)
            rows = (await session.execute(text("SELECT id FROM invitations"))).all()
            assert {row.id for row in rows} == {seed.invitation_a}
            updated = cast(
                "CursorResult[Any]",
                await session.execute(
                    text(
                        "UPDATE invitations SET status = 'revoked' "
                        "WHERE workos_invitation_id = :provider"
                    ),
                    {"provider": seed.provider_invitation_a},
                ),
            )
            assert updated.rowcount == 1
            await session.rollback()
    finally:
        await engine.dispose()


# --- Login-time retry context rebinding ---------------------------------------


async def test_login_retry_rebinds_user_context_under_rls(
    migrated_database: str, runtime_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lost-race retry rebinds ``app.user_id`` before re-reading invitations.

    The ``IntegrityError`` recovery rolls back, which clears the
    transaction-local context. Without rebinding, the retry's invitee-keyed read
    matches no row and acceptance is silently skipped (plan P4 group 5 review).
    This runs the real service against PostgreSQL with the restricted role,
    forcing only the first acceptance attempt to lose the race.
    """
    seed = await seed_two_organisation_identity(migrated_database)
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT workos_organisation_id, expires_at, email "
                        "FROM invitations WHERE id = :id"
                    ),
                    {"id": seed.invitation_a},
                )
            ).one()
    finally:
        await owner_engine.dispose()

    provider = _StaticInvitationsProvider(
        _GrantableInvitation(
            id=seed.provider_invitation_a,
            email=row.email,
            organisation_id=row.workos_organisation_id,
            expires_at=row.expires_at,
        )
    )
    profiles = _StaticProfileClient(row.email)

    real_accept = invitations_service._accept_invitations  # type: ignore[reportPrivateUsage]
    calls = {"count": 0}

    async def _lose_first_race(session: Any, user: Any, profile_email: Any, grantable: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            raise IntegrityError("insert", {}, Exception("unique violation"))
        return await real_accept(session, user, profile_email, grantable)

    monkeypatch.setattr(invitations_service, "_accept_invitations", _lose_first_race)

    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user = await session.get(User, seed.user_a)
            assert user is not None
            accepted = await invitations_service.link_invitation_on_login(
                session, user, profiles, provider
            )
            assert calls["count"] == 2
            assert [invitation.id for invitation in accepted] == [seed.invitation_a]
    finally:
        await engine.dispose()

    # The retry actually granted the membership (and its role) under the runtime role.
    verify_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with verify_engine.connect() as connection:
            membership_status = await connection.scalar(
                text(
                    "SELECT status FROM organisation_memberships "
                    "WHERE user_id = :user AND organisation_id = :org"
                ),
                {"user": seed.user_a, "org": seed.org_a},
            )
            assert membership_status == "active"
            invitation_status = await connection.scalar(
                text("SELECT status FROM invitations WHERE id = :id"),
                {"id": seed.invitation_a},
            )
            assert invitation_status == "accepted"
    finally:
        await verify_engine.dispose()


# --- Platform-plane binding and teardown -------------------------------------


async def test_platform_services_bind_the_target_organisation(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The platform membership/invitation services satisfy the policies without a bypass."""
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            details, total = await platform_admin_service.list_memberships(
                session, organisation_id=seed.org_a, page=1, page_size=50
            )
            assert total >= 1
            assert seed.membership_a in {detail.membership.id for detail in details}

        async with factory() as session:
            invitations, total = await invitations_service.list_invitations(
                session, organisation_id=seed.org_a, page=1, page_size=50
            )
            assert total >= 1
            assert seed.invitation_a in {invitation.id for invitation in invitations}

        async with factory() as session:
            actor = _detached_user(seed.user_a)
            detail = await platform_admin_service.assign_role(
                session,
                actor,
                organisation_id=seed.org_a,
                membership_id=seed.membership_a,
                role_code="administrator",
            )
            assert "administrator" in detail.roles

        async with factory() as session:
            actor = _detached_user(seed.user_a)
            detail = await platform_admin_service.set_membership_status(
                session,
                actor,
                organisation_id=seed.org_a,
                membership_id=seed.membership_a,
                status=platform_admin_service.MembershipStatus.SUSPENDED,
                workos_invitations=_NoOpInvitations(),
            )
            assert detail.membership.status is platform_admin_service.MembershipStatus.SUSPENDED
    finally:
        await engine.dispose()


async def test_tenant_creation_writes_membership_under_rls(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The tenant creation path writes the membership and owner role under forced RLS."""
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, workos_user_id, email, name, is_active) "
                    "VALUES (:id, :workos, :email, :name, true)"
                ),
                {
                    "id": user_id,
                    "workos": f"user_identity_{uuid.uuid4().hex[:10]}",
                    "email": f"creator-{user_id}@example.com",
                    "name": "Creator",
                },
            )
    finally:
        await owner_engine.dispose()

    async with factory() as session:
        organisation = await organisations_service.create_organisation(
            session, _detached_user(user_id), "Identity Created Ltd"
        )
        organisation_id = organisation.id

    async with factory() as session:
        await bind_organisation_context(session, organisation_id)
        membership_id = await session.scalar(
            text(
                "SELECT id FROM organisation_memberships "
                "WHERE user_id = :user AND organisation_id = :org"
            ),
            {"user": user_id, "org": organisation_id},
        )
        assert membership_id is not None
        grant = await session.scalar(
            text("SELECT count(*) FROM membership_roles WHERE membership_id = :id"),
            {"id": membership_id},
        )
        assert grant == 1
        await session.rollback()
    await engine.dispose()


async def test_teardown_deletes_without_a_bypass(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The cross-tenant teardown reads the user's memberships and deletes per organisation."""
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            removed = await platform_admin_service.delete_provisioned_user(
                session, user_id=seed.user_b, source="test"
            )
            assert removed is True
    finally:
        await engine.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.connect() as connection:
            memberships = await connection.scalar(
                text("SELECT count(*) FROM organisation_memberships WHERE user_id = :user"),
                {"user": seed.user_b},
            )
            assert memberships == 0
            users = await connection.scalar(
                text("SELECT count(*) FROM users WHERE id = :user"), {"user": seed.user_b}
            )
            assert users == 0
    finally:
        await owner_engine.dispose()


# --- Context lifetime ---------------------------------------------------------


async def test_context_does_not_survive_commit_or_pool_reuse(
    migrated_database: str, runtime_database_url: str
) -> None:
    """After a commit the transaction-local context is gone on the pooled connection."""
    seed = await seed_two_organisation_identity(migrated_database)
    engine = runtime_engine(runtime_database_url, pooled=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_user_context(session, seed.user_a)
            visible = await session.scalar(text("SELECT count(*) FROM organisation_memberships"))
            assert visible >= 1
            await session.commit()
            # A new transaction on the same pooled connection has no context.
            hidden = await session.scalar(text("SELECT count(*) FROM organisation_memberships"))
            assert hidden == 0
            await session.rollback()
    finally:
        await engine.dispose()


# --- Restricted runtime credential -------------------------------------------


async def test_runtime_credential_cannot_disable_policy_or_alter_schema(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The runtime role is non-owner and cannot weaken the identity policies."""
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            statements = [
                "ALTER ROLE app_runtime BYPASSRLS",
                "ALTER TABLE organisation_memberships DISABLE ROW LEVEL SECURITY",
                "ALTER TABLE membership_roles NO FORCE ROW LEVEL SECURITY",
                "DROP POLICY invitations_organisation_isolation ON invitations",
                "ALTER TABLE invitations ADD COLUMN hacked integer",
            ]
            for statement in statements:
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
                await session.rollback()
    finally:
        await engine.dispose()


# --- Migration reversibility -------------------------------------------------


async def _policy_count(database_url: str, table: str, policy: str) -> int:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_policies WHERE tablename = :table "
                    "AND policyname = :policy"
                ),
                {"table": table, "policy": policy},
            )
        return int(value or 0)
    finally:
        await engine.dispose()


def test_group5_migration_downgrade_and_reupgrade(migrated_database: str) -> None:
    """One revision down removes only the identity policies; re-upgrade re-installs."""
    config = alembic_config()
    try:
        command.downgrade(config, GROUP4B_REVISION)
        for table, policy in (
            ("organisation_memberships", _MEMBERSHIP_ISOLATION),
            ("organisation_memberships", _MEMBERSHIP_USER),
            ("membership_roles", _MEMBERSHIP_ROLES_PARENT),
            ("membership_roles", _MEMBERSHIP_ROLES_WRITE),
            ("invitations", _INVITATIONS_ISOLATION),
            ("invitations", _INVITATIONS_INVITEE_SELECT),
            ("invitations", _INVITATIONS_INVITEE_UPDATE),
            ("invitations", _INVITATIONS_WEBHOOK_SELECT),
            ("invitations", _INVITATIONS_WEBHOOK_UPDATE),
        ):
            assert asyncio.run(_policy_count(migrated_database, table, policy)) == 0, policy
        # The earlier group-4b jobs policies stay intact.
        assert (
            asyncio.run(_policy_count(migrated_database, "jobs", "jobs_organisation_isolation"))
            == 1
        )

        command.upgrade(config, "head")
        for table, policy in (
            ("organisation_memberships", _MEMBERSHIP_ISOLATION),
            ("membership_roles", _MEMBERSHIP_ROLES_PARENT),
            ("membership_roles", _MEMBERSHIP_ROLES_WRITE),
            ("invitations", _INVITATIONS_ISOLATION),
            ("invitations", _INVITATIONS_WEBHOOK_SELECT),
            ("invitations", _INVITATIONS_WEBHOOK_UPDATE),
        ):
            assert asyncio.run(_policy_count(migrated_database, table, policy)) == 1, policy
    finally:
        command.upgrade(alembic_config(), "head")


# --- Test doubles -------------------------------------------------------------


class _VerifiedProfile:
    """A verified WorkOS profile for the login-time linking test."""

    def __init__(self, email: str) -> None:
        self.email = email
        self.email_verified = True


class _StaticProfileClient:
    """A profile client that always returns one verified email."""

    def __init__(self, email: str) -> None:
        self._email = email

    async def get_profile(self, workos_user_id: str) -> Any:  # pragma: no cover
        return _VerifiedProfile(self._email)


class _GrantableInvitation:
    """One live pending provider invitation the revalidation accepts."""

    def __init__(self, *, id: str, email: str, organisation_id: str, expires_at: Any) -> None:
        self.id = id
        self.state = "pending"
        self.email = email
        self.organisation_id = organisation_id
        self.expires_at = expires_at


class _StaticInvitationsProvider:
    """A WorkOS invitations double returning one grantable invitation."""

    def __init__(self, invitation: _GrantableInvitation) -> None:
        self._invitation = invitation

    async def send_invitation(self, *, email: str, organisation_id: str) -> Any:  # pragma: no cover
        raise AssertionError("send_invitation is not expected in this test")

    async def revoke_invitation(self, workos_invitation_id: str) -> None:  # pragma: no cover
        return None

    async def get_invitation(self, workos_invitation_id: str) -> Any:  # pragma: no cover
        return self._invitation


class _NoOpInvitations:
    """A WorkOS invitations double: suspension revokes nothing in this seed."""

    async def send_invitation(self, *, email: str, organisation_id: str) -> Any:  # pragma: no cover
        raise AssertionError("send_invitation is not expected in this test")

    async def revoke_invitation(self, workos_invitation_id: str) -> None:  # pragma: no cover
        return None

    async def get_invitation(self, workos_invitation_id: str) -> Any:  # pragma: no cover
        return None
