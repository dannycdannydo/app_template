"""Break-glass operator recovery for a locked-out platform plane (plan P7).

Use only when a provider-driven deactivation (``user.deleted`` webhook) removed
the last enabled platform administrator, so no platform admin can sign in to
re-grant the role through the admin centre. The command grants ``platform_admin``
to an already provisioned and enabled internal user **only when there are zero
enabled platform administrators**; if any active admin still exists it refuses,
so it can never be used as an ordinary grant route. Every successful run writes
a null-actor ``platform.admin_recovery_granted`` audit event carrying the
operator's reason.

Usage (from the repo root, with ``.env`` in place):

    uv --directory backend run python -m scripts.recover_platform_admin \\
        --email admin@example.com --reason "recover after WorkOS deletion"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError


def main() -> int:
    # Keep the command runnable from a bare checkout, mirroring
    # provision_bootstrap_admin.py: real .env values still win because these
    # only set missing defaults.
    os.environ.setdefault("APP_ENV", "development")
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/app_template"
    )

    parser = argparse.ArgumentParser(
        description=(
            "Grant platform_admin to an existing enabled user when the platform "
            "plane is locked out (zero enabled administrators). Audited; refuses "
            "while an active administrator still exists."
        ),
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Email of the already provisioned internal user to recover.",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Short operator reason recorded on the audit event.",
    )
    args = parser.parse_args()

    from app.db.session import async_session_factory
    from app.modules.platform_admin.service import recover_platform_admin

    async def run() -> int:
        async with async_session_factory() as session:
            try:
                user = await recover_platform_admin(session, email=args.email, reason=args.reason)
            except ConflictError as exc:
                print(f"recover-platform-admin: {exc.message}", file=sys.stderr)
                return 1
            except NotFoundError as exc:
                print(f"recover-platform-admin: {exc.message}", file=sys.stderr)
                return 1
            except BadRequestError as exc:
                print(f"recover-platform-admin: {exc.message}", file=sys.stderr)
                return 1
        print(
            f"recover-platform-admin: granted platform_admin to {user.email} "
            f"({user.id}); audit event platform.admin_recovery_granted written."
        )
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
