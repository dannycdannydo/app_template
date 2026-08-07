"""Invitation services (Scope §6.5, blueprint §8, §9, §11, §29).

The invite lifecycle split (design plan §4): WorkOS sends and expires the
invitation email; the application owns the local ``invitations`` row, the
acceptance-time membership grant and the audit trail. Every mutation here is
audited through the shared ``record_event`` service and runs inside the
service-owned transaction (BP §11 — routers never commit).

- ``invite_user``: validate the intended role, backfill the WorkOS org mapping
  lazily (Scope §6.3), call the WorkOS Invitation API through the adapter, then
  insert the local row and audit ``invitation.sent``. No membership row is
  created at invite time (acceptance §5.6).
- ``revoke_invitation``: revoke at WorkOS, mark the local row ``revoked``,
  audit ``invitation.revoked``. Only a pending (``sent``) invitation can be
  revoked; terminal states are left untouched.
- ``list_invitations``: the platform listing, paginated newest-first.
- ``link_invitation_on_login``: the authoritative acceptance point. Called
  from the ``get_current_user`` provisioning chain; matches pending
  invitations by the authenticated (verified) WorkOS email, creates an active
  membership with the intended role, marks the invitation ``accepted`` and
  audits both events. Idempotent and race-safe: an invitation whose
  membership already exists is merely marked accepted, and a lost race
  against a concurrent login (unique ``(user_id, organisation_id)``
  constraint) is recovered by rollback-and-re-run, so a double first login
  can never double-grant (mirroring the bootstrap hook, Scope §6.4).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.core.security import UserProfileClient
from app.integrations.workos.invitations import WorkOSInvitationsProvider
from app.integrations.workos.organizations import WorkOSOrganizationsProvider
from app.modules.audit.service import (
    ACTION_INVITATION_ACCEPTED,
    ACTION_INVITATION_REVOKED,
    ACTION_INVITATION_SENT,
    ACTION_MEMBERSHIP_ROLE_CHANGED,
    record_event,
)
from app.modules.invitations.models import Invitation, InvitationStatus
from app.modules.invitations.queries import (
    invitations_count_statement,
    invitations_statement,
    pending_invitations_statement,
)
from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.permissions.models import MembershipRole, Role
from app.modules.platform_admin.service import ensure_workos_organisation
from app.modules.users.models import User

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def _normalise_email(email: str) -> str:
    """Normalise an invitee email for stable storage and case-insensitive match."""
    return email.strip().lower()


async def invite_user(
    session: AsyncSession,
    actor: User,
    *,
    organisation_id: uuid.UUID,
    email: str,
    role_code: str,
    workos_invitations: WorkOSInvitationsProvider,
    workos_organisations: WorkOSOrganizationsProvider,
) -> Invitation:
    """Send an invitation into an organisation and record it locally.

    Order matters (design plan §3.3): the organisation must exist (404), the
    intended role must exist in the catalogue (400), and there must be no
    already-pending invitation for the same email in the same organisation
    (409 — one grantable invitation per email per org, so acceptance-time
    linking stays unambiguous). Then the WorkOS organisation mapping is
    backfilled lazily if the organisation predates it, the WorkOS invitation
    is sent through the adapter, and the local row is inserted and audited in
    the same transaction. A WorkOS failure rolls back the local row, so an
    invitation never exists locally without a WorkOS delivery.
    """
    organisation = await session.scalar(
        select(Organisation).where(Organisation.id == organisation_id)
    )
    if organisation is None:
        raise NotFoundError(
            code="organisation_not_found",
            message="The organisation could not be found.",
        )

    role = await session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise BadRequestError(
            code="unknown_role",
            message="The role is not part of the organisation role catalogue.",
        )

    normalised_email = _normalise_email(email)
    pending = await session.scalar(
        select(Invitation).where(
            Invitation.organisation_id == organisation_id,
            Invitation.status == InvitationStatus.SENT,
            func.lower(Invitation.email) == normalised_email,
        )
    )
    if pending is not None:
        raise ConflictError(
            code="invitation_pending_exists",
            message="This user already has a pending invitation to this organisation.",
        )

    # Lazy backfill (Scope §6.3): pre-existing organisations gain their WorkOS
    # mapping at first invite. The mapping commit stays inside this caller's
    # transaction, so a failed invite rolls the mapping back with it; a
    # WorkOS org created for an aborted transaction is an orphan, reconciled
    # by the documented operational step in the organizations adapter.
    await ensure_workos_organisation(
        session, organisation, workos=workos_organisations, actor=actor
    )
    workos_organisation_id = organisation.workos_organisation_id
    if workos_organisation_id is None:
        # Unreachable: ensure_workos_organisation always maps the organisation.
        raise ServiceUnavailableError(
            code="workos_mapping_unavailable",
            message="The invitation could not be sent. Please try again.",
        )

    try:
        workos_invitation = await workos_invitations.send_invitation(
            email=normalised_email,
            organisation_id=workos_organisation_id,
        )
    except Exception:
        # The caller's request session can otherwise retain the lazy mapping
        # assignment after a failed external delivery.
        await session.rollback()
        raise

    invitation = Invitation(
        organisation_id=organisation.id,
        email=normalised_email,
        role_code=role.code,
        workos_invitation_id=workos_invitation.id,
        invited_by_user_id=actor.id,
        status=InvitationStatus.SENT,
        expires_at=workos_invitation.expires_at,
    )
    session.add(invitation)
    # Flush so the server-generated id exists for the audit row's resource_id
    # (the organisation service flushes its row for the same reason).
    await session.flush()
    await record_event(
        session,
        organisation_id=organisation.id,
        actor_user_id=actor.id,
        action=ACTION_INVITATION_SENT,
        resource_type="invitation",
        resource_id=str(invitation.id),
        metadata={
            "email": normalised_email,
            "role_code": role.code,
            "workos_invitation_id": workos_invitation.id,
        },
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # A concurrent request may have committed the partial-unique pending
        # invitation first. The provider delivery cannot be rolled back, so
        # surface the stable conflict rather than leaking an integrity error.
        raise ConflictError(
            code="invitation_pending_exists",
            message="This user already has a pending invitation to this organisation.",
        ) from None
    await session.refresh(invitation)
    return invitation


async def revoke_invitation(
    session: AsyncSession,
    actor: User,
    *,
    organisation_id: uuid.UUID,
    invitation_id: uuid.UUID,
    workos: WorkOSInvitationsProvider,
) -> Invitation:
    """Revoke a pending invitation at WorkOS and mark the local row revoked.

    The invitation is looked up scoped to its organisation (a platform admin
    revokes through the organisation's own endpoint), so an id from another
    organisation is a 404. Only ``sent`` invitations can be revoked: an
    accepted, revoked or expired invitation is terminal (409). The WorkOS
    revocation happens before the local status flip, and the audit row
    commits in the same transaction as the status change.
    """
    invitation = await session.scalar(
        select(Invitation).where(
            Invitation.id == invitation_id,
            Invitation.organisation_id == organisation_id,
        )
    )
    if invitation is None:
        raise NotFoundError(
            code="invitation_not_found",
            message="The invitation could not be found.",
        )
    if invitation.status is not InvitationStatus.SENT:
        raise ConflictError(
            code="invitation_not_revocable",
            message="Only a pending invitation can be revoked.",
        )

    if invitation.workos_invitation_id is not None:
        await workos.revoke_invitation(invitation.workos_invitation_id)

    invitation.status = InvitationStatus.REVOKED
    await record_event(
        session,
        organisation_id=invitation.organisation_id,
        actor_user_id=actor.id,
        action=ACTION_INVITATION_REVOKED,
        resource_type="invitation",
        resource_id=str(invitation.id),
        metadata={"email": invitation.email},
    )
    await session.commit()
    await session.refresh(invitation)
    return invitation


async def list_invitations(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    page: int,
    page_size: int,
) -> tuple[list[Invitation], int]:
    """Return one page of an organisation's invitations plus the total.

    Newest first, ties broken by id so paging is stable. The organisation must
    exist (the platform routes operate on a concrete organisation), so an
    unknown id is a 404 rather than an empty page.
    """
    organisation = await session.scalar(
        select(Organisation).where(Organisation.id == organisation_id)
    )
    if organisation is None:
        raise NotFoundError(
            code="organisation_not_found",
            message="The organisation could not be found.",
        )

    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    total = await session.scalar(invitations_count_statement(organisation_id=organisation_id))
    rows = await session.scalars(
        invitations_statement(organisation_id=organisation_id)
        .order_by(Invitation.created_at.desc(), Invitation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.all()), total or 0


async def link_invitation_on_login(
    session: AsyncSession,
    user: User,
    profiles: UserProfileClient,
) -> list[Invitation]:
    """Link the user's pending invitations at login; idempotent and race-safe.

    Called from the ``get_current_user`` provisioning chain after the user is
    resolved. The WorkOS profile is fetched server-side before the candidate
    query, so both verification and the email lookup use current provider
    identity rather than a stale local profile field (BP §8). This makes a
    provider-email change safe even when a best-effort webhook is delayed or
    missed. The invitation is accepted only when that verified email matches
    (acceptance §5.6, mirroring the bootstrap gate, Scope §6.4).

    A membership is created (active, with the intended role) only when the
    user is not already a member of the organisation; an existing membership
    is never silently mutated by an invitation — membership administration
    (Scope §6.6) owns status and role changes. The invitation is marked
    ``accepted`` and both events are audited in the same transaction.

    Race safety: the unique ``(user_id, organisation_id)`` membership
    constraint turns a concurrent double first login into an
    ``IntegrityError``; the transaction rolls back and re-runs once, and the
    second pass sees the winning membership and only marks the invitation
    accepted — no duplicate membership, no duplicate grant.
    """
    profile = await profiles.get_profile(user.workos_user_id)
    if not profile.email_verified:
        return []
    # Use the current validated WorkOS address for the candidate query. The
    # internal copy is deliberately not the authority: a user may change
    # their provider email between invitations and their next login.
    candidates = (await session.scalars(pending_invitations_statement(profile.email))).all()
    if not candidates:
        return []
    matched = [
        invitation
        for invitation in candidates
        if invitation.email.strip().lower() == profile.email.strip().lower()
    ]
    if not matched:
        return []

    try:
        accepted = await _accept_invitations(session, user, profile.email, matched)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # A concurrent first login linked one of these invitations first and
        # committed its membership; our insert lost the unique-constraint
        # race. Re-run once: the second pass sees the membership and marks
        # the invitation accepted without creating a duplicate.
        try:
            accepted = await _accept_invitations(session, user, profile.email, matched)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise ServiceUnavailableError(
                code="invitation_link_failed",
                message="The invitation could not be linked. Please try again.",
            ) from None
    return accepted


async def _accept_invitations(
    session: AsyncSession,
    user: User,
    profile_email: str,
    invitations: list[Invitation],
) -> list[Invitation]:
    """Grant one active membership per grantable invitation (no commit).

    Re-checks the grantable conditions in Python: between the SELECT and this
    insert a webhook refresh (Scope §6.8) or a concurrent login may have
    revoked the invitation or created the membership, and the SQL WHERE clause
    of the pending statement cannot see those in-transaction changes.
    """
    accepted: list[Invitation] = []
    for invitation in invitations:
        if invitation.status is not InvitationStatus.SENT or invitation.is_expired:
            continue
        if invitation.email.strip().lower() != profile_email.strip().lower():
            continue

        role = await session.scalar(select(Role).where(Role.code == invitation.role_code))
        if role is None:
            raise ServiceUnavailableError(
                code="invitation_role_missing",
                message="The invitation could not be linked. Please try again.",
            )

        existing = await session.scalar(
            select(OrganisationMembership).where(
                OrganisationMembership.user_id == user.id,
                OrganisationMembership.organisation_id == invitation.organisation_id,
            )
        )
        if existing is None:
            membership = OrganisationMembership(
                user_id=user.id,
                organisation_id=invitation.organisation_id,
                status=MembershipStatus.ACTIVE,
            )
            session.add(membership)
            await session.flush()
            session.add(MembershipRole(membership_id=membership.id, role_id=role.id))
            await record_event(
                session,
                organisation_id=invitation.organisation_id,
                actor_user_id=user.id,
                action=ACTION_MEMBERSHIP_ROLE_CHANGED,
                resource_type="membership",
                resource_id=str(membership.id),
                metadata={"role_code": role.code, "via": "invitation"},
            )

        invitation.status = InvitationStatus.ACCEPTED
        await record_event(
            session,
            organisation_id=invitation.organisation_id,
            actor_user_id=user.id,
            action=ACTION_INVITATION_ACCEPTED,
            resource_type="invitation",
            resource_id=str(invitation.id),
            metadata={"email": invitation.email},
        )
        accepted.append(invitation)
    return accepted
