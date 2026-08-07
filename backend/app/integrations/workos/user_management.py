"""WorkOS User Management provisioning adapter (Scope §6.4 operational step).

Pre-creates the bootstrap platform admin in WorkOS ahead of their first login.
This exists because signups are expected to be disabled in the AuthKit
configuration: the operator provisions the account (email + password,
``email_verified`` true), and the login-time bootstrap grant in
``platform_admin.service.maybe_grant_bootstrap_platform_admin`` then promotes
that exact verified email to ``platform_admin`` exactly once.

Like the other adapters in this package, the Management API key stays inside
this layer and the SDK's ``User`` model never leaves it — the rest of the
application depends on the small :class:`ProvisionedWorkOSUser` value.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from workos import WorkOSClient, WorkOSError
from workos.user_management import PasswordPlaintext

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError


@dataclass(frozen=True)
class ProvisionedWorkOSUser:
    """A WorkOS user as seen by the provisioning command (SDK model stays out)."""

    id: str
    email: str


class WorkOSUserProvisioner(Protocol):
    """The provisioning adapter contract; scripts depend on this, not the SDK."""

    def find_user_by_email(self, email: str) -> ProvisionedWorkOSUser | None:
        """Return the WorkOS user with this email, or None when none exists."""
        ...

    def create_password_user(self, *, email: str, password: str) -> ProvisionedWorkOSUser:
        """Create a verified WorkOS user with the given password."""
        ...

    def delete_user(self, user_id: str) -> None:
        """Delete the WorkOS user with this id (idempotent at the SDK level)."""
        ...


class WorkOSUserManagementClient:
    """The WorkOS-backed adapter; the Management API key never leaves it."""

    def __init__(
        self,
        *,
        api_key: str,
        client_id: str,
        api_base_url: str,
    ) -> None:
        self._client = WorkOSClient(
            api_key=api_key,
            client_id=client_id,
            base_url=api_base_url,
        )

    def find_user_by_email(self, email: str) -> ProvisionedWorkOSUser | None:
        """Return the WorkOS user with this email, or None.

        The User Management list endpoint filters server-side by email; the
        first match is authoritative because emails are unique in WorkOS.
        """
        try:
            page = self._client.user_management.list_users(email=email)
        except WorkOSError as exc:
            raise ExternalServiceError(
                code="workos_user_lookup_failed",
                message="The WorkOS user could not be looked up. Please try again.",
            ) from exc
        user = next(iter(page.data), None)
        if user is None:
            return None
        return ProvisionedWorkOSUser(id=user.id, email=user.email)

    def create_password_user(self, *, email: str, password: str) -> ProvisionedWorkOSUser:
        """Create a verified WorkOS user with the given password.

        ``email_verified`` is set so the login-time bootstrap grant accepts the
        profile (the grant requires a verified email, Scope §6.4). A failure is
        an external-service failure surfaced as an :class:`ExternalServiceError`
        with the WorkOS code attached when available.
        """
        try:
            user = self._client.user_management.create_user(
                email=email,
                password=PasswordPlaintext(password=password),
                email_verified=True,
            )
        except WorkOSError as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            raise ExternalServiceError(
                code="workos_user_create_failed",
                message=(
                    "The WorkOS user could not be created. "
                    f"WorkOS error: {code}. "
                    "Check the email is free and the password clears the WorkOS policy."
                ),
            ) from exc
        return ProvisionedWorkOSUser(id=user.id, email=user.email)

    def delete_user(self, user_id: str) -> None:
        """Delete the WorkOS user with this id.

        Used by the provisioning command's ``--delete`` mode to tear down a
        test bootstrap admin. The WorkOS SDK treats deleting a missing user as
        an error, so callers look the user up first and skip the call when it
        is already absent.
        """
        try:
            self._client.user_management.delete_user(user_id)
        except WorkOSError as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            raise ExternalServiceError(
                code="workos_user_delete_failed",
                message=(f"The WorkOS user could not be deleted. WorkOS error: {code}."),
            ) from exc


@lru_cache
def get_workos_user_management_client() -> WorkOSUserProvisioner:
    """Dependency factory for the process-wide WorkOS user provisioning client."""
    settings = get_settings()
    return WorkOSUserManagementClient(
        api_key=settings.workos_api_key,
        client_id=settings.workos_client_id,
        api_base_url=settings.workos_api_base_url,
    )
