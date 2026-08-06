"""Platform administration services (Scope §6.3, blueprint §9, §11, §29).

Organisation administration lives on the platform plane: every operation here
is called from platform-gated routes and is audited through the shared
``record_event`` service. The WorkOS organisation mapping is the v0.4 tenant
story (ADR-0001): the internal organisation is the primary record, the WorkOS
organisation is created 1:1 for it, and the mapping is either created eagerly
(platform org creation) or backfilled lazily (pre-existing organisations at
first invite, Scope §6.5). The mapping field itself is never client-writable;
it is written only by these services.

Transaction boundary (BP §11): ``create_platform_organisation`` creates the
internal organisation and its mapping in one database transaction, and calls
the WorkOS API before the mapping is committed. If the database commit fails
after WorkOS returned a successful create, the WorkOS organisation is an
orphan; it is reconciled by the documented operational step in
``integrations/workos/organizations.py`` (findable by ``external_id``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ServiceUnavailableError
from app.core.security import UserProfileClient
from app.integrations.workos.organizations import WorkOSOrganizationsProvider
from app.modules.audit.service import (
    ACTION_ORGANISATION_CREATED,
    ACTION_PLATFORM_BOOTSTRAP_GRANTED,
    record_event,
)
from app.modules.organisations.models import Organisation
from app.modules.permissions.constants import PLATFORM_ADMIN_ROLE_CODE
from app.modules.platform_admin.models import (
    BOOTSTRAP_SINGLETON_ID,
    BootstrapState,
    PlatformMembership,
    PlatformRole,
)
from app.modules.users.models import User

# The lazy-backfill event is a platform-only write: the mapping changes without
# a user-visible organisation edit, so it gets its own audited action.
ACTION_ORGANISATION_WORKOS_MAPPED = "organisation.workos_mapped"


async def create_platform_organisation(
    session: AsyncSession,
    actor: User,
    name: str,
    workos: WorkOSOrganizationsProvider,
) -> Organisation:
    """Create an internal organisation with its WorkOS mapping (transactional).

    Order matters: the internal organisation is flushed first so its id exists
    to serve as the WorkOS ``external_id`` (the orphan-reconciliation key),
    then the WorkOS organisation is created, then the mapping is persisted and
    the whole unit committed together with the audit row. A WorkOS failure
    rolls back the internal row, so an internal organisation never exists
    without a WorkOS organisation at platform creation.
    """
    organisation = Organisation(name=name)
    session.add(organisation)
    await session.flush()

    workos_organisation = await workos.create_workos_organisation(
        name=name,
        external_id=str(organisation.id),
    )
    organisation.workos_organisation_id = workos_organisation.id

    await record_event(
        session,
        organisation_id=organisation.id,
        actor_user_id=actor.id,
        action=ACTION_ORGANISATION_CREATED,
        resource_type="organisation",
        resource_id=str(organisation.id),
        metadata={"workos_organisation_id": workos_organisation.id},
    )
    await session.commit()
    await session.refresh(organisation)
    return organisation


async def ensure_workos_organisation(
    session: AsyncSession,
    organisation: Organisation,
    workos: WorkOSOrganizationsProvider,
    *,
    actor: User | None = None,
) -> Organisation:
    """Backfill the WorkOS mapping for an organisation that lacks one.

    Called lazily wherever an organisation that predates the mapping (or that
    somehow lost it) needs WorkOS delivery, e.g. the first invite (Scope
    §6.5). Already-mapped organisations are a no-op — no WorkOS call, no audit
    row — so the helper is safe to call from every delivery path. The mapping
    commit is deliberately left to the caller's transaction so backfill can be
    part of the surrounding business operation.
    """
    if organisation.workos_organisation_id is not None:
        return organisation

    workos_organisation = await workos.create_workos_organisation(
        name=organisation.name,
        external_id=str(organisation.id),
    )
    organisation.workos_organisation_id = workos_organisation.id
    await record_event(
        session,
        organisation_id=organisation.id,
        actor_user_id=actor.id if actor else None,
        action=ACTION_ORGANISATION_WORKOS_MAPPED,
        resource_type="organisation",
        resource_id=str(organisation.id),
        metadata={"workos_organisation_id": workos_organisation.id},
    )
    return organisation


async def maybe_grant_bootstrap_platform_admin(
    session: AsyncSession,
    user: User,
    profiles: UserProfileClient,
) -> PlatformMembership | None:
    """Grant ``platform_admin`` to the configured bootstrap email, exactly once.

    This is the one-time bootstrap hook (Scope §6.4, acceptance §5.5): it runs
    on every successful authentication and is a no-op unless all of the
    following hold:

    - ``BOOTSTRAP_PLATFORM_ADMIN_EMAIL`` is configured;
    - the ``bootstrap_state`` row does not exist yet (unconsumed);
    - the WorkOS profile behind the session reports the configured email and
      ``email_verified`` (the profile is fetched server-side, so the email and
      its verification state never come from client input — BP §8).

    When the grant fires, the platform membership, the bootstrap record and
    the ``platform.bootstrap_granted`` audit event are written in one
    transaction and committed together. The bootstrap record's id is a fixed
    sentinel guarded by a check constraint, so a concurrent first login loses
    the race with an ``IntegrityError``: the transaction is rolled back and
    the hook re-checks, then treats the bootstrap as already consumed — no
    second grant, no second audit row. Any other ``IntegrityError`` (e.g. the
    seeded ``platform_admin`` role is missing) surfaces as a 503 rather than a
    silent no-op, so a broken deployment cannot swallow the bootstrap.
    """

    configured = get_settings().bootstrap_platform_admin_email
    if not configured:
        return None

    bootstrap = await session.scalar(
        select(BootstrapState).where(BootstrapState.id == BOOTSTRAP_SINGLETON_ID)
    )
    if bootstrap is not None:
        return None

    profile = await profiles.get_profile(user.workos_user_id)
    if profile.email.strip().lower() != configured:
        return None
    if not profile.email_verified:
        return None

    role = await session.scalar(
        select(PlatformRole).where(PlatformRole.code == PLATFORM_ADMIN_ROLE_CODE)
    )
    if role is None:
        raise ServiceUnavailableError(
            code="platform_bootstrap_failed",
            message="The platform bootstrap could not be completed. Please try again.",
        )

    membership = PlatformMembership(user_id=user.id, platform_role_id=role.id)
    bootstrap_state = BootstrapState(email=profile.email, consumed_by_user_id=user.id)
    session.add(membership)
    session.add(bootstrap_state)
    try:
        # record_event flushes, which is where the sentinel-constraint
        # violation actually surfaces in a lost race, so the whole insert unit
        # must sit inside the try.
        await record_event(
            session,
            actor_user_id=user.id,
            action=ACTION_PLATFORM_BOOTSTRAP_GRANTED,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"email": profile.email, "role": PLATFORM_ADMIN_ROLE_CODE},
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        bootstrap = await session.scalar(
            select(BootstrapState).where(BootstrapState.id == BOOTSTRAP_SINGLETON_ID)
        )
        if bootstrap is not None:
            # A concurrent first login consumed the bootstrap first; our
            # grant was rolled back with the losing transaction.
            return None
        raise ServiceUnavailableError(
            code="platform_bootstrap_failed",
            message="The platform bootstrap could not be completed. Please try again.",
        ) from None
    return membership
