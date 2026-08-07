"""Tests for the bootstrap platform admin provisioning command (Scope §6.4).

The script is the operational counterpart to the login-time bootstrap grant:
it pre-creates the verified password user in WorkOS when signups are
disabled. These tests cover the decision logic with a fake provisioner (no
network) and the CLI error paths; the WorkOS SDK wrapper is exercised with a
stubbed SDK client to pin the contract (``email_verified`` true, password
forwarded, existing email reported without a create call).
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from app.integrations.workos.user_management import (
    ProvisionedWorkOSUser,
    WorkOSUserManagementClient,
)
from scripts.provision_bootstrap_admin import (
    ProvisionError,
    delete_bootstrap_admin,
    provision_bootstrap_admin,
    resolve_email,
    resolve_password,
)


class FakeProvisioner:
    """In-memory ``WorkOSUserProvisioner``; records calls like the real adapter."""

    def __init__(self, existing: dict[str, ProvisionedWorkOSUser] | None = None) -> None:
        self.users = dict(existing or {})
        self.created: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def find_user_by_email(self, email: str) -> ProvisionedWorkOSUser | None:
        return self.users.get(email)

    def create_password_user(self, *, email: str, password: str) -> ProvisionedWorkOSUser:
        self.created.append((email, password))
        user = ProvisionedWorkOSUser(id=f"user_{len(self.created)}", email=email)
        self.users[email] = user
        return user

    def delete_user(self, user_id: str) -> None:
        self.deleted.append(user_id)
        self.users = {email: u for email, u in self.users.items() if u.id != user_id}


class FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class FakeSession:
    """Minimal ``AsyncSession`` stand-in: records the delete and the commit."""

    def __init__(self, rowcount: int = 1) -> None:
        self.executed: list[object] = []
        self.committed = False
        self._rowcount = rowcount

    async def execute(self, statement: object) -> FakeResult:
        self.executed.append(statement)
        return FakeResult(self._rowcount)

    async def commit(self) -> None:
        self.committed = True


def test_creates_a_verified_password_user_when_absent() -> None:
    provisioner = FakeProvisioner()
    result = provision_bootstrap_admin(provisioner, email="admin@example.com", password="s3cret!")

    assert result.created is True
    assert result.email == "admin@example.com"
    assert provisioner.created == [("admin@example.com", "s3cret!")]


def test_is_idempotent_and_never_overwrites_an_existing_user() -> None:
    existing = ProvisionedWorkOSUser(id="user_1", email="admin@example.com")
    provisioner = FakeProvisioner(existing={"admin@example.com": existing})

    result = provision_bootstrap_admin(
        provisioner, email="admin@example.com", password="different-password"
    )

    assert result.created is False
    assert result.user_id == "user_1"
    assert provisioner.created == []


def test_normalises_the_email_case_and_whitespace() -> None:
    assert resolve_email("  Admin@Example.COM ") == "admin@example.com"


def test_missing_email_raises_an_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOTSTRAP_PLATFORM_ADMIN_EMAIL", "")
    with pytest.raises(ProvisionError, match="BOOTSTRAP_PLATFORM_ADMIN_EMAIL"):
        resolve_email("")


def test_missing_password_raises_an_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOTSTRAP_PLATFORM_ADMIN_PASSWORD", raising=False)
    with pytest.raises(ProvisionError, match="BOOTSTRAP_PLATFORM_ADMIN_PASSWORD"):
        resolve_password("")


def test_password_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOOTSTRAP_PLATFORM_ADMIN_PASSWORD", "env-password")
    assert resolve_password("") == "env-password"


class _User:
    def __init__(self, *, id: str, email: str) -> None:
        self.id = id
        self.email = email


class StubWorkOSClient:
    """Minimal stand-in for the WorkOS SDK surface the adapter touches."""

    def __init__(self) -> None:
        self.users: list[_User] = []
        self.created: list[dict[str, object]] = []
        self.deleted: list[str] = []
        self.user_management = self._UserManagement(self)

    class _UserManagement:
        def __init__(self, owner: StubWorkOSClient) -> None:
            self._owner = owner

        def list_users(self, *, email: str) -> object:
            class _Page:
                def __init__(self, data: list[_User]) -> None:
                    self.data = data

            return _Page([user for user in self._owner.users if user.email == email])

        def create_user(self, **kwargs: object) -> _User:
            self._owner.created.append(kwargs)
            email = cast(str, kwargs["email"])
            return _User(id=f"user_{len(self._owner.created)}", email=email)

        def delete_user(self, id: str) -> None:
            self._owner.deleted.append(id)


def _client_with_stub(stub: StubWorkOSClient) -> WorkOSUserManagementClient:
    client = WorkOSUserManagementClient.__new__(WorkOSUserManagementClient)
    client._client = stub  # type: ignore[attr-defined]
    return client


def test_adapter_forwards_email_verified_true_and_password(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubWorkOSClient()
    adapter = _client_with_stub(stub)

    user = adapter.create_password_user(email="admin@example.com", password="s3cret!")

    assert user.email == "admin@example.com"
    assert len(stub.created) == 1
    created = stub.created[0]
    assert created["email"] == "admin@example.com"
    assert created["email_verified"] is True
    # The SDK takes a PasswordPlaintext wrapper; the secret itself is inside it.
    password = created["password"]
    assert type(password).__name__ == "PasswordPlaintext"
    assert getattr(password, "password", None) == "s3cret!"


def test_adapter_reports_existing_email_without_creating(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubWorkOSClient()
    stub.users.append(_User(id="user_1", email="admin@example.com"))
    adapter = _client_with_stub(stub)

    found = adapter.find_user_by_email("admin@example.com")

    assert found is not None
    assert found.id == "user_1"
    assert adapter.find_user_by_email("other@example.com") is None


def test_adapter_deletes_a_user() -> None:
    stub = StubWorkOSClient()
    adapter = _client_with_stub(stub)

    adapter.delete_user("user_1")

    assert stub.deleted == ["user_1"]


def test_delete_removes_the_workos_user_and_the_internal_row() -> None:
    provisioner = FakeProvisioner(
        existing={
            "admin@example.com": ProvisionedWorkOSUser(id="user_1", email="admin@example.com")
        }
    )
    session = FakeSession(rowcount=1)

    result = asyncio.run(
        delete_bootstrap_admin(
            provisioner,
            session,
            email="admin@example.com",  # type: ignore[arg-type]
        )
    )

    assert result.workos_deleted is True
    assert result.internal_deleted is True
    assert provisioner.deleted == ["user_1"]
    assert len(session.executed) == 1
    assert session.committed is True


def test_delete_is_idempotent_when_nothing_exists() -> None:
    provisioner = FakeProvisioner()
    session = FakeSession(rowcount=0)

    result = asyncio.run(
        delete_bootstrap_admin(
            provisioner,
            session,
            email="admin@example.com",  # type: ignore[arg-type]
        )
    )

    assert result.workos_deleted is False
    assert result.internal_deleted is False
    assert provisioner.deleted == []
    assert session.committed is True
