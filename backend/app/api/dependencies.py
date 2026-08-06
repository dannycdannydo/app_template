"""Shared FastAPI dependencies (blueprint §5, §6, §8).

Routers stay thin and receive their dependencies here: the database session,
the current request ID, the authenticated user resolved from a validated
WorkOS session token, and the caller's organisation membership resolved from
the ``X-Org-Id`` header (v0.2 Scope §6.3).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, PermissionDenied, UnauthorizedError
from app.core.logging import current_request_id
from app.core.security import (
    InvalidSessionError,
    SessionValidator,
    UserProfileClient,
    get_session_validator,
    get_user_profile_client,
)
from app.db.session import async_session_factory
from app.modules.organisations.models import MembershipStatus, OrganisationMembership
from app.modules.permissions.queries import permission_codes_for_membership
from app.modules.users.models import User
from app.modules.users.service import get_or_provision_user

logger = structlog.get_logger()


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield one database session for the duration of a request.

    The session is committed/closed by SQLAlchemy's ``async_sessionmaker``
    context manager; the service layer owns transaction boundaries.
    """
    async with async_session_factory() as session:
        yield session


def get_request_id() -> str:
    """Return the request ID bound by the request ID middleware."""
    return current_request_id()


def _bearer_token(authorization: str | None) -> str:
    """Extract and return the Bearer token; reject missing/malformed headers."""
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError(
            code="invalid_token",
            message="A valid Bearer token is required.",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise UnauthorizedError(
            code="invalid_token",
            message="A valid Bearer token is required.",
        )
    return token


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_db)],
    validator: Annotated[SessionValidator, Depends(get_session_validator)],
    profiles: Annotated[UserProfileClient, Depends(get_user_profile_client)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Resolve the Bearer token to a validated, enabled internal user (BP §8).

    Invalid tokens are rejected with 401; valid sessions map to the internal
    user, provisioning the row on first login. Disabled users are blocked with
    403 even with a valid session.
    """
    token = _bearer_token(authorization)
    try:
        validated = await validator.validate_token(token)
    except InvalidSessionError as exc:
        logger.warning(
            "session_rejected",
            reason=exc.reason,
            token_issuer=exc.token_issuer,
        )
        raise UnauthorizedError(
            code="invalid_session",
            message="The session is invalid or has expired.",
        ) from exc
    user = await get_or_provision_user(session, validated, profiles)
    if not user.is_active:
        logger.warning("disabled_user_rejected", workos_user_id=validated.workos_user_id)
        raise PermissionDenied(
            code="user_disabled",
            message="Your account is disabled.",
        )
    return user


def _org_context_id(x_org_id: str | None) -> uuid.UUID:
    """Parse the ``X-Org-Id`` header; missing or malformed values are 400.

    The organisation id always comes from this validated header context and
    never from a request body.
    """
    if not x_org_id:
        raise BadRequestError(
            code="org_context_required",
            message="The X-Org-Id header is required.",
        )
    try:
        return uuid.UUID(x_org_id)
    except ValueError as exc:
        raise BadRequestError(
            code="invalid_org_id",
            message="The X-Org-Id header must be a valid organisation id.",
        ) from exc


async def get_current_membership(
    session: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    x_org_id: Annotated[str | None, Header()] = None,
) -> OrganisationMembership:
    """Resolve the ``X-Org-Id`` header to the caller's active membership.

    Missing token → 401 (from ``get_current_user``); missing or malformed
    ``X-Org-Id`` → 400; an organisation the user does not belong to, or a
    non-active membership → 403. Only active memberships establish an
    organisation context (v0.2 Scope §6.3).
    """
    org_id = _org_context_id(x_org_id)
    membership = await session.scalar(
        select(OrganisationMembership).where(
            OrganisationMembership.user_id == user.id,
            OrganisationMembership.organisation_id == org_id,
        )
    )
    if membership is None or membership.status != MembershipStatus.ACTIVE:
        logger.warning(
            "membership_context_rejected",
            user_id=str(user.id),
            organisation_id=str(org_id),
        )
        raise PermissionDenied(
            code="not_a_member",
            message="You are not an active member of this organisation.",
        )
    return membership


def require_permission(permission_code: str):
    """Return a dependency requiring the caller's membership to hold a permission.

    Composes ``get_current_membership`` so the caller must first be an active
    member of the organisation in ``X-Org-Id``, then checks the permission
    against the role bundles of that membership. Default deny: a code not
    granted to any of the caller's roles is rejected with 403 (v0.2 Scope §6.4,
    blueprint §9 rules).
    """

    async def _require_permission(
        session: Annotated[AsyncSession, Depends(get_db)],
        membership: Annotated[OrganisationMembership, Depends(get_current_membership)],
    ) -> OrganisationMembership:
        granted = await permission_codes_for_membership(session, membership.id)
        if permission_code not in granted:
            logger.warning(
                "permission_denied",
                organisation_id=str(membership.organisation_id),
                permission=permission_code,
            )
            raise PermissionDenied(
                code="permission_denied",
                message="You do not have permission to perform this action.",
            )
        return membership

    return _require_permission
