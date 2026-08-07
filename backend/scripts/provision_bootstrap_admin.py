"""Create the bootstrap platform admin user in WorkOS (Scope §6.4 operational step).

Run after the app is created, before the first login, when WorkOS signups are
disabled: pre-provisions the account the login-time bootstrap grant promotes
to ``platform_admin``. The user is created with a password and a verified
email (the grant requires ``email_verified`` true), so the operator only needs
to sign in with the configured email + password afterwards.

Idempotent: if the email already exists in WorkOS the command reports that and
exits successfully without touching the existing user (never resets an
existing password). The password is taken from ``BOOTSTRAP_PLATFORM_ADMIN_PASSWORD``
in the environment (or ``--password``) and is never printed or logged.

``--delete`` tears the admin down again (WorkOS user + internal ``users`` row,
which resets the one-time bootstrap) so a different admin can be provisioned
and the bootstrap re-tested during development.

Usage (from the repo root, with ``.env`` in place):

    make provision-admin
    make provision-admin-delete              # tear down the .env admin again
    make provision-admin-delete EMAIL=a@b.co # ... or a specific email
    # raw equivalents (the module lives under backend/, so run from there or
    # point uv at it):
    uv --directory backend run python -m scripts.provision_bootstrap_admin --email a@b.co --password secret
    uv --directory backend run python -m scripts.provision_bootstrap_admin --delete --email a@b.co
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ExternalServiceError
from app.integrations.workos.user_management import WorkOSUserProvisioner


@dataclass(frozen=True)
class ProvisionResult:
    """Outcome of the provisioning step for the CLI to report."""

    created: bool
    user_id: str
    email: str


@dataclass(frozen=True)
class DeleteResult:
    """Outcome of the teardown step for the CLI to report."""

    workos_deleted: bool
    internal_deleted: bool
    email: str


class ProvisionError(Exception):
    """A user-facing failure (missing config, WorkOS rejected the request)."""


def provision_bootstrap_admin(
    provisioner: WorkOSUserProvisioner,
    *,
    email: str,
    password: str,
) -> ProvisionResult:
    """Create (or confirm) the verified password user for ``email``.

    ``provisioner`` is a ``WorkOSUserProvisioner``; the protocol lives in
    ``app.integrations.workos.user_management``. Idempotent by email: an
    existing account is reported as already provisioned and never modified.
    """
    existing = provisioner.find_user_by_email(email)
    if existing is not None:
        return ProvisionResult(created=False, user_id=existing.id, email=existing.email)

    created = provisioner.create_password_user(email=email, password=password)
    return ProvisionResult(created=True, user_id=created.id, email=created.email)


async def delete_bootstrap_admin(
    provisioner: WorkOSUserProvisioner,
    session: AsyncSession,
    *,
    email: str,
) -> DeleteResult:
    """Tear down one bootstrap admin on both sides (WorkOS and the app DB).

    Deleting the internal ``users`` row cascades to the ``platform_memberships``
    and ``bootstrap_state`` rows, which is what resets the one-time bootstrap:
    the next ``make provision-admin`` + first login of a fresh email can grant
    ``platform_admin`` again.
    """
    from app.modules.users.models import User

    workos_user = provisioner.find_user_by_email(email)
    if workos_user is not None:
        provisioner.delete_user(workos_user.id)

    result = cast(
        CursorResult[Any],
        await session.execute(delete(User).where(User.email == email.lower())),
    )
    await session.commit()

    return DeleteResult(
        workos_deleted=workos_user is not None,
        internal_deleted=result.rowcount > 0,
        email=email,
    )


def resolve_email(arg_email: str) -> str:
    from app.core.config import get_settings

    email = (arg_email or get_settings().bootstrap_platform_admin_email).strip().lower()
    if not email:
        raise ProvisionError(
            "No email configured. Set BOOTSTRAP_PLATFORM_ADMIN_EMAIL in .env or pass --email."
        )
    return email


def resolve_password(arg_password: str) -> str:
    password = arg_password or os.environ.get("BOOTSTRAP_PLATFORM_ADMIN_PASSWORD", "")
    if not password:
        raise ProvisionError(
            "No password configured. Set BOOTSTRAP_PLATFORM_ADMIN_PASSWORD in .env "
            "or pass --password."
        )
    return password


def main() -> int:
    # The script never touches the database, so it must run on a machine with
    # no .env (e.g. right after the app is created, from a checkout). Provide
    # safe defaults before settings are loaded, mirroring export_openapi.py; a
    # real .env still wins because these only set missing values.
    os.environ.setdefault("APP_ENV", "development")
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/app_template"
    )

    parser = argparse.ArgumentParser(
        description=(
            "Create or delete the bootstrap platform admin in WorkOS and the "
            "app database (idempotent; BOOTSTRAP_PLATFORM_ADMIN_EMAIL / "
            "BOOTSTRAP_PLATFORM_ADMIN_PASSWORD from the environment are used "
            "when the flags are omitted)."
        ),
    )
    parser.add_argument("--email", help="Email of the bootstrap admin (default: settings).")
    parser.add_argument("--password", help="Password for the bootstrap admin (default: env).")
    parser.add_argument(
        "--delete",
        action="store_true",
        help=(
            "Tear down the bootstrap admin instead of creating it: delete the "
            "WorkOS user and the internal users row (resetting the one-time "
            "bootstrap), so a fresh admin can be provisioned and tested again."
        ),
    )
    args = parser.parse_args()

    try:
        email = resolve_email(args.email)
    except ProvisionError as exc:
        print(f"provision-admin: {exc}", file=sys.stderr)
        return 1

    # Load settings before building the client so a missing/blank WORKOS_API_KEY
    # fails fast with a clear message rather than a WorkOS SDK error.
    from app.core.config import get_settings
    from app.integrations.workos.user_management import get_workos_user_management_client

    settings = get_settings()
    if not settings.workos_api_key:
        print(
            "provision-admin: WORKOS_API_KEY is not configured. Set it in .env "
            "(the script uses the backend Management API key).",
            file=sys.stderr,
        )
        return 1

    provisioner = get_workos_user_management_client()

    if args.delete:
        return _run_delete(provisioner, email=email)

    try:
        password = resolve_password(args.password)
    except ProvisionError as exc:
        print(f"provision-admin: {exc}", file=sys.stderr)
        return 1

    try:
        result = provision_bootstrap_admin(provisioner, email=email, password=password)
    except ExternalServiceError as exc:
        print(f"provision-admin: {exc.message}", file=sys.stderr)
        return 1
    except ProvisionError as exc:
        print(f"provision-admin: {exc}", file=sys.stderr)
        return 1

    if result.created:
        print(
            f"provision-admin: created WorkOS user {result.email} ({result.user_id}); "
            "the first login with this email and password grants platform_admin."
        )
    else:
        print(
            f"provision-admin: {result.email} already exists in WorkOS "
            f"({result.user_id}); left unchanged."
        )
    return 0


def _run_delete(provisioner: WorkOSUserProvisioner, *, email: str) -> int:
    """Run the ``--delete`` teardown: WorkOS user + internal app user row."""
    from app.db.session import async_session_factory

    async def run() -> int:
        async with async_session_factory() as session:
            try:
                result = await delete_bootstrap_admin(provisioner, session, email=email)
            except ExternalServiceError as exc:
                print(f"provision-admin: {exc.message}", file=sys.stderr)
                return 1
            except Exception as exc:  # DB-level failures (e.g. FK leftovers)
                print(
                    f"provision-admin: the WorkOS user was handled, but the internal "
                    f"user row could not be deleted: {exc}. Remove any organisation "
                    "memberships for this user first, then re-run --delete.",
                    file=sys.stderr,
                )
                return 1

        parts: list[str] = []
        if result.workos_deleted:
            parts.append("WorkOS user deleted")
        else:
            parts.append("WorkOS user already absent")
        if result.internal_deleted:
            parts.append("internal user deleted (bootstrap reset)")
        else:
            parts.append("no internal user row (nothing to reset)")
        print(f"provision-admin: {email}: " + ", ".join(parts) + ".")
        print(
            "provision-admin: update BOOTSTRAP_PLATFORM_ADMIN_EMAIL in .env to the "
            "new admin, then run `make provision-admin` and sign in once."
        )
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
