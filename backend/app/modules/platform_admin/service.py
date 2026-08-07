"""Platform administration services (Scope §6.3/§6.6, blueprint §9, §11, §29).

Organisation and membership administration live on the platform plane: every
operation here is called from platform-gated routes and is audited through the
shared ``record_event`` service. The WorkOS organisation mapping is the v0.4
tenant story (ADR-0001): the internal organisation is the primary record, the
WorkOS organisation is created 1:1 for it, and the mapping is either created
eagerly (platform org creation) or backfilled lazily (pre-existing
organisations at first invite, Scope §6.5). The mapping field itself is never
client-writable; it is written only by these services.

Membership administration (Scope §6.6) completes the platform management of
organisations: listing memberships with the member's user context and role
codes, assigning and removing organisation roles, suspending and reactivating
memberships, and removing memberships. Suspension and removal both revoke the
member's pending invitations into the organisation (WorkOS + local, design
plan §9 item 5) so no grantable invitation outlives a blocked or departed
member. Enforcement against org routes needs no new code: the existing
active-membership dependency (``get_current_membership``) already rejects
suspended memberships with 403 ``not_a_member``.

Transaction boundary (BP §11): ``create_platform_organisation`` creates the
internal organisation and its mapping in one database transaction, and calls
the WorkOS API before the mapping is committed. If the database commit fails
after WorkOS returned a successful create, the WorkOS organisation is an
orphan; it is reconciled by the documented operational step in
``integrations/workos/organizations.py`` (findable by ``external_id``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import BadRequestError, NotFoundError, ServiceUnavailableError
from app.core.security import UserProfileClient
from app.integrations.workos.invitations import WorkOSInvitationsProvider
from app.integrations.workos.organizations import WorkOSOrganizationsProvider
from app.modules.audit.service import (
    ACTION_INVITATION_REVOKED,
    ACTION_MEMBERSHIP_REACTIVATED,
    ACTION_MEMBERSHIP_REMOVED,
    ACTION_MEMBERSHIP_ROLE_CHANGED,
    ACTION_MEMBERSHIP_SUSPENDED,
    ACTION_ORGANISATION_CREATED,
    ACTION_ORGANISATION_UPDATED,
    ACTION_PLATFORM_BOOTSTRAP_GRANTED,
    record_event,
)
from app.modules.invitations.models import Invitation, InvitationStatus
from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.permissions.constants import PLATFORM_ADMIN_ROLE_CODE
from app.modules.permissions.models import MembershipRole, Role
from app.modules.platform_admin.models import (
    BOOTSTRAP_SINGLETON_ID,
    BootstrapState,
    PlatformMembership,
    PlatformRole,
)
from app.modules.platform_admin.queries import (
    memberships_count_statement,
    memberships_statement,
    organisations_count_statement,
    organisations_statement,
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


async def list_organisations(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
) -> tuple[list[Organisation], int]:
    """Return one page of every organisation plus the total.

    Newest first, ties broken by id so paging is stable (matching the audit,
    invitation and membership listings). This is the admin centre's catalogue
    over the whole tenant fleet, so there is no filter beyond the standard
    pagination envelope.
    """
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total = await session.scalar(organisations_count_statement())
    organisations = await session.scalars(
        organisations_statement()
        .order_by(Organisation.created_at.desc(), Organisation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(organisations.all()), total or 0


async def get_organisation(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
) -> Organisation:
    """Return one organisation, or raise the standard 404.

    Thin public wrapper over the private ``_get_organisation_or_404`` helper
    so the detail route reads like the listing and mutation routes.
    """
    return await _get_organisation_or_404(session, organisation_id)


async def update_organisation(
    session: AsyncSession,
    *,
    actor: User,
    organisation_id: uuid.UUID,
    name: str,
) -> Organisation:
    """Rename an organisation and record the change in the audit log.

    Only the name is editable through the platform plane; the WorkOS mapping
    is written exclusively by the services (creation and lazy backfill), so
    it is never touched here. The audit row records the new name so the
    history is readable without a second lookup.
    """
    organisation = await _get_organisation_or_404(session, organisation_id)
    organisation.name = name
    await record_event(
        session,
        organisation_id=organisation.id,
        actor_user_id=actor.id,
        action=ACTION_ORGANISATION_UPDATED,
        resource_type="organisation",
        resource_id=str(organisation.id),
        metadata={"name": name},
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


# --- Membership administration (Scope §6.6) ---

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


@dataclass
class MembershipDetail:
    """One membership plus the user context and role codes for the platform UI.

    The platform listing and every mutation response carry the member's name
    and email and the membership's role codes, so the admin centre renders a
    memberships table without a second round trip. Pure assembly data, built
    by the service queries.
    """

    membership: OrganisationMembership
    user_name: str
    user_email: str
    roles: list[str]


async def _get_organisation_or_404(
    session: AsyncSession,
    organisation_id: uuid.UUID,
) -> Organisation:
    """Return the organisation or raise the standard 404."""
    organisation = await session.scalar(
        select(Organisation).where(Organisation.id == organisation_id)
    )
    if organisation is None:
        raise NotFoundError(
            code="organisation_not_found",
            message="The organisation could not be found.",
        )
    return organisation


async def _get_membership_or_404(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    membership_id: uuid.UUID,
) -> OrganisationMembership:
    """Return a membership scoped to its organisation, or raise a 404.

    The lookup is org-scoped like the invitations service, so a membership id
    from another organisation is a 404 rather than a cross-org read. Unlike
    ``get_current_membership`` this does not check status: the platform admin
    must be able to administer suspended memberships (reactivate them), so any
    membership of the organisation is addressable here.
    """
    membership = await session.scalar(
        select(OrganisationMembership).where(
            OrganisationMembership.id == membership_id,
            OrganisationMembership.organisation_id == organisation_id,
        )
    )
    if membership is None:
        raise NotFoundError(
            code="membership_not_found",
            message="The membership could not be found.",
        )
    return membership


async def membership_detail(
    session: AsyncSession,
    membership: OrganisationMembership,
) -> MembershipDetail:
    """Assemble the user context and role codes for one membership.

    The member's row always exists (foreign key), so a missing user can only
    mean a broken test double; an empty string keeps the response harmless
    instead of crashing the listing. Role codes are resolved in two steps —
    the membership's ``membership_roles`` rows, then the ``roles`` rows — and
    re-filtered in Python because, like ``_accept_invitations``, the in-memory
    test session cannot apply the SQL WHERE clauses; the compiled statements
    are covered by the query-construction and real-database tests.
    """
    user = await session.scalar(select(User).where(User.id == membership.user_id))
    granted = (
        await session.scalars(
            select(MembershipRole).where(MembershipRole.membership_id == membership.id)
        )
    ).all()
    role_ids = {row.role_id for row in granted if row.membership_id == membership.id}
    role_rows = (await session.scalars(select(Role).where(Role.id.in_(role_ids)))).all()
    codes_by_id = {role.id: role.code for role in role_rows if role.id in role_ids}
    return MembershipDetail(
        membership=membership,
        user_name=user.name if user is not None else "",
        user_email=user.email if user is not None else "",
        roles=sorted(codes_by_id[role_id] for role_id in role_ids if role_id in codes_by_id),
    )


async def list_memberships(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    page: int,
    page_size: int,
) -> tuple[list[MembershipDetail], int]:
    """Return one page of an organisation's memberships plus the total.

    Newest first, ties broken by id so paging is stable (matching the audit
    and invitation listings). The organisation must exist — the platform
    routes operate on a concrete organisation — so an unknown id is a 404
    rather than an empty page. Each detail carries the member's user context
    and role codes for the admin centre table.
    """
    await _get_organisation_or_404(session, organisation_id)
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total = await session.scalar(memberships_count_statement(organisation_id=organisation_id))
    memberships = await session.scalars(
        memberships_statement(organisation_id=organisation_id)
        .order_by(OrganisationMembership.created_at.desc(), OrganisationMembership.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    details = [await membership_detail(session, membership) for membership in memberships.all()]
    return details, total or 0


async def _revoke_pending_invitations(
    session: AsyncSession,
    actor: User,
    *,
    organisation_id: uuid.UUID,
    user_email: str,
    workos_invitations: WorkOSInvitationsProvider,
) -> int:
    """Revoke the member's pending invitations into the org (WorkOS + local).

    Mirrors ``revoke_invitation`` (Scope §6.5): each ``sent`` invitation for
    this email and organisation is revoked at WorkOS first — a WorkOS failure
    aborts the caller's transaction so no local status flip survives without
    its WorkOS counterpart — then marked ``revoked`` locally with an
    ``invitation.revoked`` audit row, all in the caller's transaction. The
    grantable conditions are re-checked in Python after the SELECT because the
    in-memory test session cannot apply the SQL WHERE clause and because, like
    ``_accept_invitations``, a concurrent webhook refresh (Scope §6.8) may
    have revoked one between the SELECT and this pass.
    """
    candidates = (
        await session.scalars(
            select(Invitation).where(
                Invitation.organisation_id == organisation_id,
                Invitation.status == InvitationStatus.SENT,
                func.lower(Invitation.email) == user_email.strip().lower(),
            )
        )
    ).all()
    revoked = 0
    for invitation in candidates:
        if (
            invitation.organisation_id != organisation_id
            or invitation.status is not InvitationStatus.SENT
            or invitation.email.strip().lower() != user_email.strip().lower()
        ):
            continue
        if invitation.workos_invitation_id is not None:
            await workos_invitations.revoke_invitation(invitation.workos_invitation_id)
        invitation.status = InvitationStatus.REVOKED
        await record_event(
            session,
            organisation_id=organisation_id,
            actor_user_id=actor.id,
            action=ACTION_INVITATION_REVOKED,
            resource_type="invitation",
            resource_id=str(invitation.id),
            metadata={"email": invitation.email},
        )
        revoked += 1
    return revoked


async def assign_role(
    session: AsyncSession,
    actor: User,
    *,
    organisation_id: uuid.UUID,
    membership_id: uuid.UUID,
    role_code: str,
) -> MembershipDetail:
    """Grant one organisation role to a membership; audited.

    The role must exist in the organisation role catalogue (400
    ``unknown_role``, mirroring the invite flow, Scope §6.5). Assigning a role
    the membership already holds is an idempotent no-op — no duplicate
    ``membership_roles`` row and no audit event — so a retried request
    (double click, network retry) cannot duplicate a grant or spam the audit
    log. The grant and its ``membership.role_changed`` event commit in one
    transaction.
    """
    await _get_organisation_or_404(session, organisation_id)
    membership = await _get_membership_or_404(
        session, organisation_id=organisation_id, membership_id=membership_id
    )
    role = await session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise BadRequestError(
            code="unknown_role",
            message="The role is not part of the organisation role catalogue.",
        )
    held = await session.scalar(
        select(MembershipRole).where(
            MembershipRole.membership_id == membership.id,
            MembershipRole.role_id == role.id,
        )
    )
    if held is None:
        session.add(MembershipRole(membership_id=membership.id, role_id=role.id))
        await record_event(
            session,
            organisation_id=organisation_id,
            actor_user_id=actor.id,
            action=ACTION_MEMBERSHIP_ROLE_CHANGED,
            resource_type="membership",
            resource_id=str(membership.id),
            metadata={"role_code": role.code, "action": "assigned"},
        )
        await session.commit()
    return await membership_detail(session, membership)


async def remove_role(
    session: AsyncSession,
    actor: User,
    *,
    organisation_id: uuid.UUID,
    membership_id: uuid.UUID,
    role_code: str,
) -> MembershipDetail:
    """Revoke one organisation role from a membership; audited.

    The role must exist in the catalogue (400 ``unknown_role``); a role the
    membership does not hold is an idempotent no-op, so removal can be retried
    safely. Removing the last role leaves the membership active with no
    permissions (default deny, BP §9) — a valid managed state; membership
    removal, not role removal, is how a member's presence ends.
    """
    await _get_organisation_or_404(session, organisation_id)
    membership = await _get_membership_or_404(
        session, organisation_id=organisation_id, membership_id=membership_id
    )
    role = await session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise BadRequestError(
            code="unknown_role",
            message="The role is not part of the organisation role catalogue.",
        )
    held = await session.scalar(
        select(MembershipRole).where(
            MembershipRole.membership_id == membership.id,
            MembershipRole.role_id == role.id,
        )
    )
    if held is not None:
        await session.delete(held)
        await record_event(
            session,
            organisation_id=organisation_id,
            actor_user_id=actor.id,
            action=ACTION_MEMBERSHIP_ROLE_CHANGED,
            resource_type="membership",
            resource_id=str(membership.id),
            metadata={"role_code": role.code, "action": "removed"},
        )
        await session.commit()
    return await membership_detail(session, membership)


async def set_membership_status(
    session: AsyncSession,
    actor: User,
    *,
    organisation_id: uuid.UUID,
    membership_id: uuid.UUID,
    status: MembershipStatus,
    workos_invitations: WorkOSInvitationsProvider,
) -> MembershipDetail:
    """Suspend or reactivate a membership; audited.

    Only ``active`` and ``suspended`` are settable here; the request schema
    rejects the lifecycle-only statuses. Setting the current status is an
    idempotent no-op. Suspension also revokes the member's pending invitations
    into the organisation (WorkOS + local, design plan §9 item 5), writing an
    ``invitation.revoked`` row per invitation in the same transaction as the
    status change and the ``membership.suspended`` event, so no grantable
    invitation outlives the suspension.
    """
    await _get_organisation_or_404(session, organisation_id)
    membership = await _get_membership_or_404(
        session, organisation_id=organisation_id, membership_id=membership_id
    )
    if membership.status == status:
        return await membership_detail(session, membership)

    previous = membership.status
    membership.status = status
    metadata: dict[str, str | int] = {
        "previous_status": previous.value,
        "status": status.value,
    }
    if status == MembershipStatus.SUSPENDED:
        user = await session.scalar(select(User).where(User.id == membership.user_id))
        metadata["revoked_invitations"] = await _revoke_pending_invitations(
            session,
            actor,
            organisation_id=organisation_id,
            user_email=user.email if user is not None else "",
            workos_invitations=workos_invitations,
        )
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor.id,
        action=(
            ACTION_MEMBERSHIP_SUSPENDED
            if status == MembershipStatus.SUSPENDED
            else ACTION_MEMBERSHIP_REACTIVATED
        ),
        resource_type="membership",
        resource_id=str(membership.id),
        metadata=metadata,
    )
    await session.commit()
    return await membership_detail(session, membership)


async def remove_membership(
    session: AsyncSession,
    actor: User,
    *,
    organisation_id: uuid.UUID,
    membership_id: uuid.UUID,
    workos_invitations: WorkOSInvitationsProvider,
) -> MembershipDetail:
    """Remove a membership and revoke its pending invitations; audited.

    Removal is the terminal operation: the member's pending invitations into
    the organisation are revoked first (WorkOS + local) so no grantable
    invitation outlives the membership, then the membership row is deleted and
    its ``membership_roles`` go with it via cascade. The response still
    returns the removed membership's user context and roles so the admin
    centre can drop the row it already rendered. The audit trail preserves the
    history — ``membership.removed`` plus one ``invitation.revoked`` row per
    revoked invitation — all in one transaction.
    """
    await _get_organisation_or_404(session, organisation_id)
    membership = await _get_membership_or_404(
        session, organisation_id=organisation_id, membership_id=membership_id
    )
    detail = await membership_detail(session, membership)
    revoked = await _revoke_pending_invitations(
        session,
        actor,
        organisation_id=organisation_id,
        user_email=detail.user_email,
        workos_invitations=workos_invitations,
    )
    await record_event(
        session,
        organisation_id=organisation_id,
        actor_user_id=actor.id,
        action=ACTION_MEMBERSHIP_REMOVED,
        resource_type="membership",
        resource_id=str(membership.id),
        metadata={"user_id": str(membership.user_id), "revoked_invitations": revoked},
    )
    await session.delete(membership)
    await session.commit()
    return detail
