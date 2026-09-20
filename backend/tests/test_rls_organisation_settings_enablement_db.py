"""Real-PostgreSQL production enablement suite for organisation settings (P3, 4a).

Plan P3 / ``docs/rls-rollout.md`` §3 group 4a. It proves the production
enablement migration (``c9d0e1f2a3b4``) that promotes the two direct
organisation-owned configuration tables ``organisation_features`` and
``organisation_ai_settings`` into the permanent policy chain:

- the canonical ``<table>_organisation_isolation`` policy is installed with
  matching ``USING``/``WITH CHECK`` and RLS is enabled **and forced** on both
  tables;
- default denial: no bound context returns no rows and fails closed for writes;
- read and write policies deny select, insert, update and delete of another
  organisation's rows, including through an unscoped query and a tenant-key
  move;
- the platform-plane services (feature-flag management and AI-settings
  management) bind exactly the organisation they target, so the explicit
  per-organisation platform path satisfies the policy without a bypass;
- every organisation-creation path (tenant ``create_organisation``, platform
  ``create_platform_organisation`` and the platform bootstrap) writes its
  default settings row on the restricted role, and the missing-row create and
  update branches of the AI-settings service work under forced RLS;
- representative, realistically sized lookups use the per-organisation indexes
  with no sequential scan; and
- the migration upgrades, downgrades one revision and re-upgrades cleanly.

Error non-disclosure (plan P3 "Verify application errors do not disclose
whether RLS hid a foreign row"): group 4a deliberately has **no tenant
resource-detail error surface**. Both tables are only reached through the
platform plane, whose operations name exactly one organisation and are
authorised for any organisation by the platform permission dependency; the one
``404`` is ``organisation_not_found`` from the unprotected ``organisations``
table, before any policy can hide a settings row. Group 4a therefore claims no
part of that aggregate P3 checkbox (see ``docs/rls-rollout.md`` §3, group 4a).

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
from types import SimpleNamespace
from typing import Any, cast

import pytest
from alembic import command
from sqlalchemy import CursorResult, delete, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.context_helpers import FakeWorkOSOrganizationsProvider
from tests.rls_helpers import (
    RUNTIME_ROLE,
    SettingsIsolationSeed,
    alembic_config,
    database_reachable,
    downgrade_to_base,
    provision_runtime_login,
    runtime_engine,
    runtime_url,
    seed_representative_settings,
    seed_two_organisation_settings,
    upgrade_to_head,
)

from app.ai.persistence import service as ai_persistence
from app.ai.persistence.models import OrganisationAISettings
from app.core.security import UserProfile, UserProfileClient
from app.db.rls import bind_organisation_context
from app.modules.feature_flags import service as feature_flags
from app.modules.organisations import service as organisations_service
from app.modules.organisations.models import Organisation
from app.modules.platform_admin import service as platform_admin_service
from app.modules.platform_admin.models import BootstrapState
from app.modules.users.models import User

#: The group 3 revision this migration revises.
GROUP3_REVISION = "b8c9d0e1f2a3"

#: Every table enabled by the group 4a migration.
SETTINGS_TABLES = (
    "organisation_features",
    "organisation_ai_settings",
)


def _production_policy(table: str) -> str:
    return f"{table}_organisation_isolation"


class _StubProfileClient(UserProfileClient):
    """Return a fixed verified profile so the bootstrap never calls WorkOS."""

    def __init__(self, *, email: str, email_verified: bool) -> None:
        self._email = email
        self._email_verified = email_verified

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        return UserProfile(
            email=self._email,
            name="Settings Bootstrap",
            email_verified=self._email_verified,
        )


async def _seed_actor(owner_url: str) -> User:
    """Insert one platform actor with the owner credential and return it detached."""
    unique = uuid.uuid4().hex[:10]
    actor = User(
        id=uuid.uuid4(),
        workos_user_id=f"user_settings_{unique}",
        email=f"settings-{unique}@example.com",
        name="Settings Admin",
        is_active=True,
    )
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, workos_user_id, email, name, is_active) "
                    "VALUES (:id, :workos, :email, :name, true)"
                ),
                {
                    "id": actor.id,
                    "workos": actor.workos_user_id,
                    "email": actor.email,
                    "name": actor.name,
                },
            )
    finally:
        await engine.dispose()
    return actor


async def _seed_blank_organisations(owner_url: str, *, count: int) -> list[uuid.UUID]:
    """Insert organisations with no settings rows and return their ids (owner role)."""
    ids = [uuid.uuid4() for _ in range(count)]
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO organisations (id, name) VALUES (:id, :name)"),
                [{"id": org, "name": f"Settings blank {i}"} for i, org in enumerate(ids)],
            )
    finally:
        await engine.dispose()
    return ids


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


# --- Installed production policy --------------------------------------------


async def test_production_policies_are_installed_and_forced(migrated_database: str) -> None:
    """RLS is enabled and forced on both settings tables with the canonical policy."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            for table in SETTINGS_TABLES:
                row = (
                    await connection.execute(
                        text(
                            "SELECT qual, with_check FROM pg_policies "
                            "WHERE tablename = :table AND policyname = :policy"
                        ),
                        {"table": table, "policy": _production_policy(table)},
                    )
                ).one_or_none()
                assert row is not None, f"missing production policy on {table}"
                assert "app_current_tenant_id()" in row.qual
                assert "app_current_tenant_id()" in row.with_check

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
    """Both settings lookups use their per-organisation index, with no sequential scan.

    The two tables are one-row-per-organisation configuration tables, so their
    **total** cardinality grows with the organisation count; a two-row seed
    cannot substantiate rollout principle 4 ("representative query plans and
    supporting indexes ... no sequential-scan regression"). A realistically
    sized multi-tenant fixture therefore seeds the feature-override and
    AI-settings lookups the platform plane runs, and the restricted-role
    ``EXPLAIN`` must resolve each through its index.
    """
    org_a, feature_a, ai_settings_a = await seed_representative_settings(migrated_database)
    assert feature_a is not None
    assert ai_settings_a is not None
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    feature_lookup_sql = (
        "SELECT * FROM organisation_features WHERE organisation_id = :org AND feature_key = :key"
    )
    ai_settings_lookup_sql = "SELECT * FROM organisation_ai_settings WHERE organisation_id = :org"
    try:
        async with factory() as session:
            await bind_organisation_context(session, org_a)

            feature_plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(
                        text("EXPLAIN " + feature_lookup_sql),
                        {"org": org_a, "key": "records.deletion"},
                    )
                ).all()
            )
            assert "Seq Scan" not in feature_plan
            assert "organisation_features_organisation_id" in feature_plan, feature_plan

            ai_settings_plan = "\n".join(
                row[0]
                for row in (
                    await session.execute(text("EXPLAIN " + ai_settings_lookup_sql), {"org": org_a})
                ).all()
            )
            assert "Seq Scan" not in ai_settings_plan
            assert "organisation_ai_settings_organisation_id" in ai_settings_plan, ai_settings_plan
            await session.rollback()
    finally:
        await engine.dispose()


# --- Default denial ----------------------------------------------------------


async def test_default_denial_without_context(
    migrated_database: str, runtime_database_url: str
) -> None:
    """No bound context: zero settings rows and every write is rejected."""
    await seed_two_organisation_settings(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            for table in SETTINGS_TABLES:
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO organisation_features "
                        "(id, organisation_id, feature_key, enabled) "
                        "VALUES (:id, :org, 'no.context', true)"
                    ),
                    {"id": uuid.uuid4(), "org": uuid.uuid4()},
                )
            await session.rollback()
            with pytest.raises(ProgrammingError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO organisation_ai_settings (id, organisation_id, enabled) "
                        "VALUES (:id, :org, true)"
                    ),
                    {"id": uuid.uuid4(), "org": uuid.uuid4()},
                )
            await session.rollback()
    finally:
        await engine.dispose()


# --- Cross-organisation read and write ---------------------------------------


async def test_select_is_organisation_scoped(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Own rows are visible; a foreign row is invisible even through an unscoped query."""
    seed = await seed_two_organisation_settings(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            for table in SETTINGS_TABLES:
                orgs = (await session.execute(text(f"SELECT organisation_id FROM {table}"))).all()
                assert {row.organisation_id for row in orgs} == {seed.org_a}, table
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM organisation_features WHERE id = :id"),
                    {"id": seed.feature_b},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM organisation_ai_settings WHERE id = :id"),
                    {"id": seed.ai_settings_b},
                )
                == 0
            )
            await session.rollback()
    finally:
        await engine.dispose()


async def test_insert_update_delete_and_tenant_key_move_are_denied(
    migrated_database: str, runtime_database_url: str
) -> None:
    """Same-tenant writes succeed; every cross-tenant write is denied.

    Plan P3 completion evidence ("every enabled table has real
    select/insert/update/delete policy tests"): on each group-4a table a real
    own-tenant insert/update/delete succeeds, a mismatched insert is rejected by
    ``WITH CHECK``, a foreign-tenant update/delete changes no row and a
    tenant-key move into another organisation is rejected.
    """
    seed = await seed_two_organisation_settings(migrated_database)
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # A mismatched insert is rejected by WITH CHECK on both tables. The
        # failed statement aborts its transaction, so roll back and rebind the
        # tenant before the next one.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            for _table, statement, params in _foreign_inserts(seed):
                with pytest.raises(ProgrammingError, match="row-level security"):
                    await session.execute(text(statement), params)
                await session.rollback()
                await bind_organisation_context(session, seed.org_a)

        # Same-tenant inserts succeed on both tables.
        async with factory() as session:
            await bind_organisation_context(session, seed.org_a)
            await session.execute(
                text(
                    "INSERT INTO organisation_features "
                    "(id, organisation_id, feature_key, enabled) "
                    "VALUES (:id, :org, 'test.own', true)"
                ),
                {"id": uuid.uuid4(), "org": seed.org_a},
            )
            await session.commit()
        async with factory() as session:
            await bind_organisation_context(session, seed.org_c)
            await session.execute(
                text(
                    "INSERT INTO organisation_ai_settings (id, organisation_id, enabled) "
                    "VALUES (:id, :org, true)"
                ),
                {"id": uuid.uuid4(), "org": seed.org_c},
            )
            await session.commit()

        # Same-tenant update and delete succeed; the matching foreign writes
        # affect no row. Rolled back so the seeded rows survive for later checks.
        write_targets = (
            (
                "organisation_features",
                "UPDATE organisation_features SET enabled = false WHERE id = :id",
                seed.feature_a,
                seed.feature_b,
            ),
            (
                "organisation_ai_settings",
                "UPDATE organisation_ai_settings SET version = version + 1 WHERE id = :id",
                seed.ai_settings_a,
                seed.ai_settings_b,
            ),
        )
        for table, update_sql, own_id, foreign_id in write_targets:
            async with factory() as session:
                await bind_organisation_context(session, seed.org_a)
                for target_id, expected in ((own_id, 1), (foreign_id, 0)):
                    updated = cast(
                        "CursorResult[Any]",
                        await session.execute(text(update_sql), {"id": target_id}),
                    )
                    assert updated.rowcount == expected, f"{table} update {target_id}"
                    deleted = cast(
                        "CursorResult[Any]",
                        await session.execute(
                            text(f"DELETE FROM {table} WHERE id = :id"), {"id": target_id}
                        ),
                    )
                    assert deleted.rowcount == expected, f"{table} delete {target_id}"
                await session.rollback()

        # Moving a same-tenant row into another tenant is rejected.
        for table, own_id in (
            ("organisation_features", seed.feature_a),
            ("organisation_ai_settings", seed.ai_settings_a),
        ):
            async with factory() as session:
                await bind_organisation_context(session, seed.org_a)
                with pytest.raises(ProgrammingError, match="row-level security"):
                    await session.execute(
                        text(f"UPDATE {table} SET organisation_id = :org WHERE id = :id"),
                        {"org": seed.org_b, "id": own_id},
                    )
                await session.rollback()
    finally:
        await engine.dispose()


def _foreign_inserts(seed: SettingsIsolationSeed) -> list[tuple[str, str, dict[str, object]]]:
    """Return one cross-tenant insert per group-4a table for the WITH CHECK proof."""
    return [
        (
            "organisation_features",
            "INSERT INTO organisation_features "
            "(id, organisation_id, feature_key, enabled) "
            "VALUES (:id, :org, 'foreign.key', true)",
            {"id": uuid.uuid4(), "org": seed.org_c},
        ),
        (
            "organisation_ai_settings",
            "INSERT INTO organisation_ai_settings (id, organisation_id, enabled) "
            "VALUES (:id, :org, true)",
            {"id": uuid.uuid4(), "org": seed.org_c},
        ),
    ]


# --- Platform-plane service binding ------------------------------------------


async def test_platform_services_bind_the_target_organisation(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The platform-plane services satisfy the policy without a bypass.

    ``list_feature_flags``/``get_ai_settings``/``set_feature_flag``/
    ``update_ai_settings`` are called on the restricted runtime role with no
    caller-bound context; each binds exactly the organisation it targets, so the
    explicit per-organisation platform path reads and writes the protected rows.
    """
    seed = await seed_two_organisation_settings(migrated_database)
    actor_id = uuid.uuid4()
    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, workos_user_id, email, name, is_active) "
                    "VALUES (:id, :workos, :email, :name, true)"
                ),
                {
                    "id": actor_id,
                    "workos": f"user_settings_{uuid.uuid4().hex[:10]}",
                    "email": f"settings-{uuid.uuid4().hex[:10]}@example.com",
                    "name": "Settings Admin",
                },
            )
    finally:
        await owner_engine.dispose()
    actor = User(
        id=actor_id,
        workos_user_id=f"user_settings_{uuid.uuid4().hex[:10]}",
        email=f"settings-{uuid.uuid4().hex[:10]}@example.com",
        name="Settings Admin",
        is_active=True,
    )
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            states = await feature_flags.list_feature_flags(session, organisation_id=seed.org_a)
            deletion = next(state for state in states if state.definition.key == "records.deletion")
            assert deletion.overridden is True
            assert deletion.enabled is True

        async with factory() as session:
            settings_row = await ai_persistence.get_ai_settings(session, organisation_id=seed.org_a)
            assert settings_row.organisation_id == seed.org_a

        async with factory() as session:
            state = await feature_flags.set_feature_flag(
                session,
                actor=actor,
                feature_key="records.deletion",
                organisation_id=seed.org_a,
                enabled=False,
                configuration_json=None,
            )
            assert state.enabled is False
            assert state.override is not None
            assert state.override.organisation_id == seed.org_a

        async with factory() as session:
            settings_row = await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=seed.org_a,
                expected_version=1,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=None,
                retention_policy_days=None,
            )
            assert settings_row.organisation_id == seed.org_a
            assert settings_row.version == 2
    finally:
        await engine.dispose()


# --- Missing-row creation paths ----------------------------------------------


async def test_platform_services_create_missing_settings_rows(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The restricted role creates missing settings rows, including post-commit refresh.

    Three new paths ship with group 4a and the existing-row suite never touched
    them: ``create_default_settings`` (the shared organisation-creation insert),
    the ``get_ai_settings`` missing-row branch (create, commit, rebind, refresh)
    and the ``update_ai_settings`` missing-row branch (create at version 1, then
    update to 2). Each must work under forced RLS on ``app_runtime`` with no
    bypass, and the owner read-back proves the commit/rebind/refresh sequence
    actually persisted the protected row.
    """
    actor = await _seed_actor(migrated_database)
    creator_org, getter_org, updater_org = await _seed_blank_organisations(
        migrated_database, count=3
    )
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # 1. create_default_settings directly: the shared creation insert.
        async with factory() as session:
            created = await ai_persistence.create_default_settings(
                session, organisation_id=creator_org
            )
            await session.commit()
            assert created.organisation_id == creator_org
            assert created.enabled is False

        # 2. get_ai_settings missing row: create + commit + rebind + refresh.
        async with factory() as session:
            fetched = await ai_persistence.get_ai_settings(session, organisation_id=getter_org)
            assert fetched.organisation_id == getter_org
            assert fetched.enabled is False
            assert fetched.version == 1

        # 3. update_ai_settings missing row: create at version 1 and update to 2.
        async with factory() as session:
            updated = await ai_persistence.update_ai_settings(
                session,
                actor=actor,
                organisation_id=updater_org,
                expected_version=1,
                enabled=True,
                allowed_provider_ids=[],
                allowed_model_ids=[],
                provider_override=None,
                model_override=None,
                monthly_budget=None,
                retention_policy_days=None,
                allowed_transfer_modes=["inline"],
                max_large_attachment_bytes=50_000_000,
            )
            assert updated.organisation_id == updater_org
            assert updated.enabled is True
            assert updated.version == 2
    finally:
        await engine.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    try:
        async with owner_factory() as session:
            for organisation_id in (creator_org, getter_org, updater_org):
                persisted = await session.scalar(
                    select(OrganisationAISettings).where(
                        OrganisationAISettings.organisation_id == organisation_id
                    )
                )
                assert persisted is not None, organisation_id
    finally:
        await owner_engine.dispose()


async def test_organisation_creation_paths_write_settings_under_rls(
    migrated_database: str,
    runtime_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every production organisation-creation path writes its settings row under RLS.

    Group 4a's claim is that all three production creation paths — the tenant
    ``create_organisation``, the platform ``create_platform_organisation`` and
    the platform bootstrap — bind the new organisation before inserting its
    default settings row. This runs each one on the restricted ``app_runtime``
    login with the policy forced on and verifies the rows by owner read-back.
    """
    actor = await _seed_actor(migrated_database)
    bootstrap_name = f"RLS Bootstrap {uuid.uuid4().hex[:8]}"
    bootstrap_email = f"bootstrap-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(
        platform_admin_service,
        "get_settings",
        lambda: SimpleNamespace(
            bootstrap_platform_admin_email=bootstrap_email,
            bootstrap_platform_admin_org=bootstrap_name,
        ),
    )
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # 1. Tenant creation.
        async with factory() as session:
            tenant_org = (
                await organisations_service.create_organisation(session, actor, "RLS Tenant Ltd")
            ).id

        # 2. Platform creation with its WorkOS mapping.
        async with factory() as session:
            platform_org = (
                await platform_admin_service.create_platform_organisation(
                    session,
                    actor=actor,
                    name="RLS Platform Ltd",
                    workos=FakeWorkOSOrganizationsProvider(),
                )
            ).id

        # 3. Platform bootstrap. The bootstrap sentinel is a global singleton,
        # so clear it first to make this run's grant fire. The cleanup runs on
        # the owner credential: group 7 (plan P4) forces RLS on
        # ``bootstrap_states``, whose insert/delete are gated to the validated
        # platform/service context, and this fixture reset is neither.
        owner_reset_engine = create_async_engine(migrated_database, poolclass=NullPool)
        try:
            async with owner_reset_engine.begin() as connection:
                await connection.execute(delete(BootstrapState))
        finally:
            await owner_reset_engine.dispose()
        async with factory() as session:
            membership = await platform_admin_service.maybe_grant_bootstrap_platform_admin(
                session,
                actor,
                _StubProfileClient(email=bootstrap_email, email_verified=True),
            )
            assert membership is not None
    finally:
        await engine.dispose()

    owner_engine = create_async_engine(migrated_database, poolclass=NullPool)
    owner_factory = async_sessionmaker(owner_engine, expire_on_commit=False)
    try:
        async with owner_factory() as session:
            bootstrap_org = await session.scalar(
                select(Organisation.id).where(Organisation.name == bootstrap_name)
            )
            assert bootstrap_org is not None
            for organisation_id in (tenant_org, platform_org, bootstrap_org):
                settings_row = await session.scalar(
                    select(OrganisationAISettings).where(
                        OrganisationAISettings.organisation_id == organisation_id
                    )
                )
                assert settings_row is not None, organisation_id
                assert settings_row.enabled is False
    finally:
        await owner_engine.dispose()


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


async def _rls_flags(database_url: str, table: str) -> tuple[bool, bool]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = :table"
                    ),
                    {"table": table},
                )
            ).one()
        return bool(row.relrowsecurity), bool(row.relforcerowsecurity)
    finally:
        await engine.dispose()


def test_group4a_migration_downgrade_and_reupgrade(migrated_database: str) -> None:
    """One revision down removes only the settings policies; re-upgrade re-installs."""
    config = alembic_config()
    try:
        command.downgrade(config, GROUP3_REVISION)
        for table in SETTINGS_TABLES:
            assert (
                asyncio.run(_policy_count(migrated_database, table, _production_policy(table))) == 0
            ), table
            assert asyncio.run(_rls_flags(migrated_database, table)) == (False, False), table
        # The earlier group-3 AI-data policies stay intact.
        assert (
            asyncio.run(
                _policy_count(
                    migrated_database, "ai_requests", "ai_requests_organisation_isolation"
                )
            )
            == 1
        )
        assert asyncio.run(_rls_flags(migrated_database, "ai_requests")) == (True, True)

        command.upgrade(config, "head")
        for table in SETTINGS_TABLES:
            assert (
                asyncio.run(_policy_count(migrated_database, table, _production_policy(table))) == 1
            ), table
            assert asyncio.run(_rls_flags(migrated_database, table)) == (True, True), table
    finally:
        command.upgrade(alembic_config(), "head")


# --- Restricted runtime credential ------------------------------------------


async def test_runtime_credential_cannot_disable_policy_or_alter_schema(
    migrated_database: str, runtime_database_url: str
) -> None:
    """The runtime role is non-owner and cannot weaken the settings policies."""
    engine = runtime_engine(runtime_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            statements = ["ALTER ROLE app_runtime BYPASSRLS"]
            for table in SETTINGS_TABLES:
                statements.extend(
                    [
                        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
                        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
                        f"DROP POLICY {_production_policy(table)} ON {table}",
                        f"ALTER TABLE {table} ADD COLUMN hacked integer",
                    ]
                )
            for statement in statements:
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
                await session.rollback()
    finally:
        await engine.dispose()
