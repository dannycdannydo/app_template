"""Full-stack HTTP tests for the platform AI-settings GET/PUT pair (v0.8 Scope §6.2).

The capability-traceability table requires API integration evidence for the
extended settings endpoint, not just persistence-service and rejection-matrix
coverage: a platform admin must be able to GET the default-off row with the two
v0.8 transfer-policy fields, PUT a new policy that persists and is returned,
and a stale PUT must surface the standard 409 envelope without changing either
field. These tests run the real ASGI application against the real migrated
PostgreSQL — the two tiers the rest of the suite keeps separate — with the
session dependency overridden to the migrated database and the session
validator backed by a local RSA key (no network).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.auth_helpers import build_validator, generate_key_pair, make_token

from app.api.dependencies import get_db
from app.core.security import UserProfile, get_session_validator, get_user_profile_client
from app.main import create_app
from app.modules.organisations.models import Organisation
from app.modules.permissions.constants import PLATFORM_ADMIN_ROLE_CODE
from app.modules.platform_admin.models import PlatformMembership, PlatformRole
from app.modules.users.models import User

BACKEND_ROOT = Path(__file__).resolve().parents[1]

_AI_SETTINGS_PATH = "/api/v1/platform/organisations/{organisation_id}/ai-settings"


def _database_reachable(database_url: str) -> bool:
    """Probe the configured database with a short async engine connect."""

    async def _probe() -> bool:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(select(1))
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


class _FakeProfileClient:
    """Profile client answering the seeded platform admin's verified profile."""

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        return UserProfile(
            email="platform-admin@example.com",
            name="Platform Admin",
            email_verified=True,
        )


def _build_app(migrated_database: str, private_key: rsa.RSAPrivateKey) -> FastAPI:
    """Assemble the real app with the migrated database and a local-RSA session
    validator, exactly like the request-flow suites do for their fakes."""
    session_factory = async_sessionmaker(
        create_async_engine(migrated_database, poolclass=NullPool), expire_on_commit=False
    )
    app = create_app()

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_validator] = lambda: build_validator(private_key)
    app.dependency_overrides[get_user_profile_client] = lambda: _FakeProfileClient()
    return app


async def _seed_platform_admin(
    session: AsyncSession, *, workos_user_id: str
) -> tuple[User, Organisation]:
    """Seed the platform admin (role/permission/grant come from the migration)."""
    user = User(
        workos_user_id=workos_user_id,
        email="platform-admin@example.com",
        name="Platform Admin",
    )
    session.add(user)
    await session.flush()
    role = await session.scalar(
        select(PlatformRole).where(PlatformRole.code == PLATFORM_ADMIN_ROLE_CODE)
    )
    assert role is not None, "the platform_admin role must be seeded by the migration"
    session.add(PlatformMembership(user_id=user.id, platform_role_id=role.id))
    organisation = Organisation(name="AI Org")
    session.add(organisation)
    await session.commit()
    await session.refresh(user)
    return user, organisation


async def _ai_settings_row(session: AsyncSession, organisation_id: uuid.UUID) -> dict[str, Any]:
    from app.ai.persistence.models import OrganisationAISettings

    row = await session.scalar(
        select(OrganisationAISettings).where(
            OrganisationAISettings.organisation_id == organisation_id
        )
    )
    assert row is not None
    return {
        "version": row.version,
        "allowed_transfer_modes": list(row.allowed_transfer_modes),
        "max_large_attachment_bytes": row.max_large_attachment_bytes,
    }


def _headers(private_key: rsa.RSAPrivateKey, *, workos_user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(private_key, sub=workos_user_id)}"}


async def test_get_ai_settings_returns_both_transfer_fields_with_defaults(
    migrated_database: str,
) -> None:
    """GET returns the default-off row including the two v0.8 fields."""
    private_key, _ = generate_key_pair()
    workos_user_id = f"user_{uuid.uuid4().hex}"
    app = _build_app(migrated_database, private_key)
    session_factory = async_sessionmaker(
        create_async_engine(migrated_database, poolclass=NullPool), expire_on_commit=False
    )
    async with session_factory() as session:
        _user, organisation = await _seed_platform_admin(session, workos_user_id=workos_user_id)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get(
            _AI_SETTINGS_PATH.format(organisation_id=organisation.id),
            headers=_headers(private_key, workos_user_id=workos_user_id),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["organisation_id"] == str(organisation.id)
    assert body["version"] == 1
    assert body["enabled"] is False
    assert body["allowed_transfer_modes"] == ["inline"]
    assert body["max_large_attachment_bytes"] == 50_000_000


async def test_put_ai_settings_persists_transfer_fields_and_is_returned(
    migrated_database: str,
) -> None:
    """A platform-admin PUT persists both new fields, bumps the version and
    returns them in the response."""
    private_key, _ = generate_key_pair()
    workos_user_id = f"user_{uuid.uuid4().hex}"
    app = _build_app(migrated_database, private_key)
    session_factory = async_sessionmaker(
        create_async_engine(migrated_database, poolclass=NullPool), expire_on_commit=False
    )
    async with session_factory() as session:
        _user, organisation = await _seed_platform_admin(session, workos_user_id=workos_user_id)

    payload = {
        "version": 1,
        "enabled": True,
        "allowed_provider_ids": ["fake"],
        "allowed_model_ids": [],
        "provider_override": None,
        "model_override": None,
        "monthly_budget": None,
        "retention_policy_days": None,
        "allowed_transfer_modes": ["inline", "provider_upload"],
        "max_large_attachment_bytes": 30_000_000,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.put(
            _AI_SETTINGS_PATH.format(organisation_id=organisation.id),
            headers=_headers(private_key, workos_user_id=workos_user_id),
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["enabled"] is True
    assert body["allowed_transfer_modes"] == ["inline", "provider_upload"]
    assert body["max_large_attachment_bytes"] == 30_000_000

    async with session_factory() as session:
        assert await _ai_settings_row(session, organisation.id) == {
            "version": 2,
            "allowed_transfer_modes": ["inline", "provider_upload"],
            "max_large_attachment_bytes": 30_000_000,
        }


async def test_stale_put_returns_409_without_changing_either_field(
    migrated_database: str,
) -> None:
    """A stale full replacement returns the standard 409 envelope and leaves
    both transfer-policy fields (and the version) untouched (BP §10, v0.8 Scope
    §6.2 checkbox 3 optimistic-concurrency evidence)."""
    private_key, _ = generate_key_pair()
    workos_user_id = f"user_{uuid.uuid4().hex}"
    app = _build_app(migrated_database, private_key)
    session_factory = async_sessionmaker(
        create_async_engine(migrated_database, poolclass=NullPool), expire_on_commit=False
    )
    async with session_factory() as session:
        _user, organisation = await _seed_platform_admin(session, workos_user_id=workos_user_id)

    first_payload = {
        "version": 1,
        "enabled": True,
        "allowed_provider_ids": ["fake"],
        "allowed_model_ids": [],
        "provider_override": None,
        "model_override": None,
        "monthly_budget": None,
        "retention_policy_days": None,
        "allowed_transfer_modes": ["inline", "provider_upload"],
        "max_large_attachment_bytes": 30_000_000,
    }
    stale_payload = {
        **first_payload,
        "version": 1,  # already consumed by the first PUT
        "allowed_transfer_modes": ["inline"],
        "max_large_attachment_bytes": 50_000_000,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        first = await client.put(
            _AI_SETTINGS_PATH.format(organisation_id=organisation.id),
            headers=_headers(private_key, workos_user_id=workos_user_id),
            json=first_payload,
        )
        assert first.status_code == 200
        stale = await client.put(
            _AI_SETTINGS_PATH.format(organisation_id=organisation.id),
            headers=_headers(private_key, workos_user_id=workos_user_id),
            json=stale_payload,
        )

    assert stale.status_code == 409
    envelope = stale.json()
    assert set(envelope) == {"code", "message", "details", "request_id"}
    assert envelope["code"] == "ai_settings_version_conflict"
    assert envelope["request_id"]

    async with session_factory() as session:
        assert await _ai_settings_row(session, organisation.id) == {
            "version": 2,
            "allowed_transfer_modes": ["inline", "provider_upload"],
            "max_large_attachment_bytes": 30_000_000,
        }
