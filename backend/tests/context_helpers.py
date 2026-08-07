"""Shared in-memory fakes for request-context and organisation tests (v0.2 Scope §6.3).

Mirrors the philosophy of ``tests/auth_helpers.py`` and the fakes in
``tests/test_auth.py``: the full ASGI stack runs with the real WorkOS session
validator backed by a local RSA key, while the database session and the
WorkOS profile client are replaced so the suite needs neither PostgreSQL nor
a network connection. ``lookup_queue`` drives ``scalar()`` in call order, so a
test queues exactly the rows the request flow reads.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from tests.auth_helpers import build_validator, generate_key_pair

from app.api.dependencies import get_current_membership, get_db
from app.core.security import (
    UserProfile,
    UserProfileClient,
    get_session_validator,
    get_user_profile_client,
)
from app.integrations.workos.invitations import (
    WorkOSInvitation,
    WorkOSInvitationsProvider,
    get_workos_invitations_client,
)
from app.integrations.workos.organizations import (
    WorkOSOrganisation,
    WorkOSOrganizationsProvider,
    get_workos_organizations_client,
)
from app.main import create_app
from app.modules.audit.models import AuditEvent
from app.modules.feature_flags.models import OrganisationFeature
from app.modules.files.models import File, FileStatus
from app.modules.invitations.models import Invitation, InvitationStatus
from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.permissions.models import MembershipRole, Role
from app.modules.platform_admin.models import (
    BootstrapState,
    PlatformMembership,
    PlatformRole,
)
from app.modules.records.models import Record
from app.modules.users.models import User


@dataclass
class ContextState:
    """In-memory stand-ins shared across requests of one test."""

    users: dict[str, User] = field(default_factory=dict[str, User])  # by workos_user_id
    lookup_queue: list[Any] = field(  # consumed by scalar() in call order
        default_factory=list[Any]
    )
    organisations: list[Organisation] = field(default_factory=list[Organisation])
    memberships: list[OrganisationMembership] = field(default_factory=list[OrganisationMembership])
    membership_roles: list[MembershipRole] = field(default_factory=list[MembershipRole])
    # Role catalogue rows staged for the membership-administration tests; the
    # fake answers ``select(Role)`` from here (the real WHERE/join filtering is
    # proven by the query-construction and real-database tests).
    roles: list[Role] = field(default_factory=list[Role])
    records: list[Record] = field(default_factory=list[Record])
    audit_events: list[AuditEvent] = field(default_factory=list[AuditEvent])
    # Files (Scope §6.3): the metadata records staged or created by the files
    # module; the fake answers the org-scoped listing from here and re-applies
    # the org/deleted/status filters in the service tests when needed (the
    # WHERE clauses are proven by the query-construction and real-database
    # tests).
    files: list[File] = field(default_factory=list[File])
    # Invitations (Scope §6.5): the local invite rows staged or created by the
    # fake session; the pending-invitation query in the login-time linking
    # service returns these and the service's own status/email/expiry guards
    # decide which can grant.
    invitations: list[Invitation] = field(default_factory=list[Invitation])
    # Feature flags (Scope §6.7): the organisation override rows staged or
    # created by the fake session; the enforcement helper answers from here.
    feature_flags: list[OrganisationFeature] = field(default_factory=list[OrganisationFeature])
    owner_role: Role | None = None
    # Platform plane (Scope §6.2/§6.4): seeded platform roles, the memberships
    # granted by the bootstrap hook, and the single bootstrap record when the
    # one-time grant has been consumed.
    platform_roles: list[PlatformRole] = field(default_factory=list[PlatformRole])
    platform_memberships: list[PlatformMembership] = field(default_factory=list[PlatformMembership])
    bootstrap_states: list[BootstrapState] = field(default_factory=list[BootstrapState])
    # scalars() falls back to entity-based answers below unless the test queues
    # explicit rows in call order (used by the /me payload paths).
    scalars_queue: list[list[Any]] = field(default_factory=list[list[Any]])
    granted_permissions: set[str] = field(  # consumed by scalars() (permission checks)
        default_factory=set[str]
    )
    # The next commit raises an IntegrityError (bootstrap race simulation).
    fail_commits: int = 0
    # Optional profile the fake profile client returns (bootstrap grant tests);
    # defaults to the verified ada@example.com profile.
    profile: UserProfile | None = None
    # WorkOS organisations created by the fake provider, keyed by external id;
    # drives the mapping round-trip and the lazy-backfill assertions.
    workos_organisations: dict[str, WorkOSOrganisation] = field(
        default_factory=dict[str, WorkOSOrganisation]
    )
    # WorkOS invitations sent by the fake provider; drives the send/revoke
    # assertions of the invitation endpoints.
    workos_invitations: list[WorkOSInvitation] = field(default_factory=list[WorkOSInvitation])
    # WorkOS invitation ids revoked through the fake provider.
    revoked_workos_invitations: list[str] = field(default_factory=list[str])


def make_owner_role() -> Role:
    """Build the seeded owner role row the organisation service looks up."""
    return make_role(code="owner", name="Owner")


def make_role(code: str, name: str) -> Role:
    """Build a role row with a deterministic id (catalogue roles for invites)."""
    role = Role(code=code, name=name)
    role.id = uuid.uuid4()
    return role


def make_platform_admin_role() -> PlatformRole:
    """Build the seeded platform_admin role row the bootstrap hook looks up."""
    from app.modules.permissions.constants import PLATFORM_ADMIN_ROLE_CODE

    role = PlatformRole(code=PLATFORM_ADMIN_ROLE_CODE, name="Platform Admin")
    role.id = uuid.uuid4()
    return role


def make_user(*, workos_user_id: str = "user_test123", is_active: bool = True) -> User:
    user = User(workos_user_id=workos_user_id, email="ada@example.com", name="Ada Lovelace")
    user.id = uuid.uuid4()
    user.is_active = is_active
    user.created_at = datetime.now(UTC)
    return user


def make_membership(
    user: User,
    organisation_id: uuid.UUID,
    *,
    status: MembershipStatus = MembershipStatus.ACTIVE,
) -> OrganisationMembership:
    membership = OrganisationMembership(
        user_id=user.id,
        organisation_id=organisation_id,
        status=status,
    )
    membership.id = uuid.uuid4()
    membership.created_at = datetime.now(UTC)
    membership.updated_at = datetime.now(UTC)
    return membership


def make_membership_role(membership_id: uuid.UUID, role_id: uuid.UUID) -> MembershipRole:
    """Build a membership-role grant row for the role round-trip tests."""
    membership_role = MembershipRole(membership_id=membership_id, role_id=role_id)
    membership_role.id = uuid.uuid4()
    membership_role.created_at = datetime.now(UTC)
    return membership_role


def make_organisation(
    *,
    name: str = "Acme Ltd",
    workos_organisation_id: str | None = None,
) -> Organisation:
    """Build a standalone organisation row (existing org or pre-mapping)."""
    organisation = Organisation(name=name, workos_organisation_id=workos_organisation_id)
    organisation.id = uuid.uuid4()
    organisation.created_at = datetime.now(UTC)
    organisation.updated_at = datetime.now(UTC)
    return organisation


def make_record(
    organisation_id: uuid.UUID,
    *,
    title: str = "First record",
    body: str = "Record body",
) -> Record:
    record = Record(organisation_id=organisation_id, title=title, body=body)
    record.id = uuid.uuid4()
    record.created_at = datetime.now(UTC)
    record.updated_at = datetime.now(UTC)
    return record


def make_file(
    organisation_id: uuid.UUID,
    *,
    original_filename: str = "report.pdf",
    content_type: str = "application/pdf",
    size_bytes: int = 1024,
    status: FileStatus = FileStatus.PENDING,
    object_key: str | None = None,
) -> File:
    """Build a standalone file metadata row for the request-flow tests.

    The object key defaults to the server-generated format the service uses;
    ``created_by_user_id`` stays unset unless the test needs a creator.
    """
    file = File(
        organisation_id=organisation_id,
        storage_provider="fake",
        storage_bucket="test-bucket",
        object_key=object_key
        or f"organisations/{organisation_id}/documents/{uuid.uuid4()}/original",
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        status=status,
    )
    file.id = uuid.uuid4()
    file.created_at = datetime.now(UTC)
    file.updated_at = datetime.now(UTC)
    return file


def make_audit_event(
    *,
    organisation_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    action: str = "organisation.created",
    resource_type: str = "organisation",
    resource_id: str = "resource-1",
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata=metadata or {},
    )
    event.id = uuid.uuid4()
    event.created_at = datetime.now(UTC)
    return event


def make_invitation(
    organisation_id: uuid.UUID,
    invited_by_user_id: uuid.UUID,
    *,
    email: str = "invitee@example.com",
    role_code: str = "member",
    workos_invitation_id: str | None = None,
    status: InvitationStatus = InvitationStatus.SENT,
    expires_at: datetime | None = None,
) -> Invitation:
    """Build a standalone invitation row for the request-flow tests."""
    invitation = Invitation(
        organisation_id=organisation_id,
        email=email,
        role_code=role_code,
        workos_invitation_id=workos_invitation_id,
        invited_by_user_id=invited_by_user_id,
        status=status,
        expires_at=expires_at or (datetime.now(UTC) + timedelta(days=7)),
    )
    invitation.id = uuid.uuid4()
    invitation.created_at = datetime.now(UTC)
    invitation.updated_at = datetime.now(UTC)
    return invitation


def make_organisation_feature(
    organisation_id: uuid.UUID,
    *,
    feature_key: str = "records.deletion",
    enabled: bool = True,
    configuration_json: dict[str, Any] | None = None,
) -> OrganisationFeature:
    """Build a standalone feature-flag override row for the request-flow tests."""
    feature = OrganisationFeature(
        organisation_id=organisation_id,
        feature_key=feature_key,
        enabled=enabled,
        configuration_json=configuration_json or {},
    )
    feature.id = uuid.uuid4()
    feature.created_at = datetime.now(UTC)
    feature.updated_at = datetime.now(UTC)
    return feature


class _ScalarsResult:
    """Stand-in for an async ``ScalarResult``: carries rows and exposes .all()."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Minimal session stand-in for the request-context and creation flows."""

    def __init__(self, state: ContextState) -> None:
        self._state = state
        self._added: list[Any] = []
        # Mirrors AsyncSession.info so the feature-flag helper's per-session
        # memo (core/feature_flags.py) works against the fake.
        self.info: dict[Any, Any] = {}
        # Status of every staged invitation after the last successful commit;
        # a simulated lost race restores from this, like a database rollback
        # would (the losing transaction's status flips never persisted).
        self._last_committed_statuses: dict[uuid.UUID, InvitationStatus] = {
            invitation.id: invitation.status for invitation in state.invitations
        }

    async def scalar(self, statement: object) -> Any:
        self._track_invitation_statuses()
        if self._state.lookup_queue:
            return self._state.lookup_queue.pop(0)
        return None

    async def scalars(self, statement: object) -> _ScalarsResult:
        # The invitation queries (pending-at-login and the platform listing)
        # answer from the staged invitations before the scalars_queue: the
        # login-time linking service runs inside get_current_user, i.e.
        # before the /me payload queries that consume the queue, and its
        # own status/email/expiry guards decide which staged rows can grant
        # (the WHERE clauses are proven by the query-construction and
        # real-database tests). Anything else falls back to the queue, then
        # to the entity-based answers below.
        self._track_invitation_statuses()
        descriptions = getattr(statement, "column_descriptions", None)
        entity = descriptions[0].get("entity") if descriptions else None
        if entity is Invitation:
            return _ScalarsResult(list(self._state.invitations))
        if self._state.scalars_queue:
            return _ScalarsResult(self._state.scalars_queue.pop(0))
        # Membership administration (Scope §6.6): the fake answers membership,
        # membership-role and role queries from the staged state; the services
        # re-filter in Python because the WHERE/join clauses are not applied
        # here (they are proven by the query-construction and real-database
        # tests). These branches sit after the scalars_queue on purpose: the
        # /me payload paths still consume queued rows first.
        if entity is OrganisationMembership:
            return _ScalarsResult(list(self._state.memberships))
        if entity is Organisation:
            # Scope §6.9: the platform organisations listing answers from the
            # staged organisations; the ordering and pagination clauses are
            # not applied here (proven by the query-construction and
            # real-database tests).
            return _ScalarsResult(list(self._state.organisations))
        if entity is MembershipRole:
            return _ScalarsResult(list(self._state.membership_roles))
        if entity is Role:
            return _ScalarsResult(list(self._state.roles))
        if entity is OrganisationFeature:
            # Scope §6.7: the feature-flag queries answer from the staged
            # overrides; the enforcement helper and the management service
            # re-filter in Python because the WHERE clauses are not applied
            # here (they are proven by the query-construction and
            # real-database tests).
            return _ScalarsResult(list(self._state.feature_flags))
        if entity is Record:
            return _ScalarsResult(list(self._state.records))
        if entity is File:
            return _ScalarsResult(list(self._state.files))
        if entity is AuditEvent:
            return _ScalarsResult(list(self._state.audit_events))
        return _ScalarsResult(sorted(self._state.granted_permissions))

    def _track_invitation_statuses(self) -> None:
        """Baseline any staged invitation status not yet tracked.

        Service flows read an invitation before they mutate its status, so the
        status at first read is the pre-transaction state a simulated lost
        race must roll back to.
        """
        for invitation in self._state.invitations:
            self._last_committed_statuses.setdefault(invitation.id, invitation.status)

    def add(self, instance: Any) -> None:
        self._added.append(instance)

    async def flush(self) -> None:
        now = datetime.now(UTC)
        for obj in self._added:
            if obj.id is None:
                obj.id = uuid.uuid4()
            if getattr(obj, "created_at", None) is None and hasattr(obj, "created_at"):
                obj.created_at = now
            # AuditEvent and BootstrapState have no updated_at column
            # (append-only / single-write by design), so only touch the
            # timestamp when the model carries it.
            if getattr(obj, "updated_at", None) is None and hasattr(obj, "updated_at"):
                obj.updated_at = now

    async def commit(self) -> None:
        await self.flush()
        if self._state.fail_commits:
            # Simulate a lost race (e.g. the bootstrap sentinel constraint or
            # the membership unique constraint): nothing is persisted and the
            # caller's rollback discards it. Statuses flipped on staged
            # invitation rows this transaction are restored to their last
            # committed values, exactly like a real database rollback, so a
            # losing linking pass re-runs against the pre-race state.
            self._state.fail_commits -= 1
            for invitation in self._state.invitations:
                invitation.status = self._last_committed_statuses.get(
                    invitation.id, invitation.status
                )
            raise IntegrityError("insert", {}, Exception("duplicate key value"))
        for obj in self._added:
            if isinstance(obj, Organisation):
                self._state.organisations.append(obj)
            elif isinstance(obj, OrganisationMembership):
                self._state.memberships.append(obj)
            elif isinstance(obj, MembershipRole):
                self._state.membership_roles.append(obj)
            elif isinstance(obj, Record):
                # Updates reuse the staged instance, so never append twice.
                if all(existing is not obj for existing in self._state.records):
                    self._state.records.append(obj)
            elif isinstance(obj, File):
                # Updates reuse the staged instance, so never append twice.
                if all(existing is not obj for existing in self._state.files):
                    self._state.files.append(obj)
            elif isinstance(obj, AuditEvent):
                # Append-only: never modify or remove an existing event.
                if all(existing is not obj for existing in self._state.audit_events):
                    self._state.audit_events.append(obj)
            elif isinstance(obj, Invitation):
                self._state.invitations.append(obj)
            elif isinstance(obj, OrganisationFeature):
                # Updates reuse the staged instance, so never append twice.
                if all(existing is not obj for existing in self._state.feature_flags):
                    self._state.feature_flags.append(obj)
            elif isinstance(obj, PlatformRole):
                self._state.platform_roles.append(obj)
            elif isinstance(obj, PlatformMembership):
                self._state.platform_memberships.append(obj)
            elif isinstance(obj, BootstrapState):
                self._state.bootstrap_states.append(obj)
            elif isinstance(obj, User):
                # Mirrors the model's ``is_active`` default applied at flush time;
                # provisioned users are always created active.
                obj.is_active = True
                self._state.users[obj.workos_user_id] = obj
        self._added.clear()
        # The transaction committed: staged invitation statuses are now the
        # rollback baseline for any later simulated lost race.
        self._last_committed_statuses = {
            invitation.id: invitation.status for invitation in self._state.invitations
        }

    async def delete(self, instance: Any) -> None:
        # Membership administration (Scope §6.6): role removal deletes a
        # MembershipRole, membership removal deletes the membership and drops
        # its role grants, mirroring the database-level CASCADE. Everything
        # else keeps the original record behaviour.
        if isinstance(instance, MembershipRole):
            self._state.membership_roles = [
                membership_role
                for membership_role in self._state.membership_roles
                if membership_role.id != instance.id
            ]
        elif isinstance(instance, OrganisationMembership):
            self._state.memberships = [
                membership for membership in self._state.memberships if membership.id != instance.id
            ]
            self._state.membership_roles = [
                membership_role
                for membership_role in self._state.membership_roles
                if membership_role.membership_id != instance.id
            ]
        else:
            self._state.records = [
                record for record in self._state.records if record.id != instance.id
            ]

    async def rollback(self) -> None:
        self._added.clear()

    async def refresh(self, instance: Any) -> None:
        # Attributes are already populated by flush(); nothing to re-read.
        return None


class FakeProfileClient(UserProfileClient):
    def __init__(self, state: ContextState) -> None:
        self._state = state

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        return self._state.profile or UserProfile(
            email="ada@example.com", name="Ada Lovelace", email_verified=True
        )


class FakeWorkOSOrganizationsProvider(WorkOSOrganizationsProvider):
    """In-memory stand-in for the WorkOS organisations adapter.

    Creates organisations with deterministic fake WorkOS ids and records them
    by external id, so tests can assert the mapping round-trip and the
    lazy-backfill behaviour without a network or an API key. Without a
    ``ContextState`` the provider keeps its own private record of created
    organisations (used by the real-database tests).
    """

    def __init__(self, state: ContextState | None = None) -> None:
        self._state = state
        self._counter = 0
        self.created: dict[str, WorkOSOrganisation] = {}

    async def create_workos_organisation(
        self, *, name: str, external_id: str
    ) -> WorkOSOrganisation:
        organisation = WorkOSOrganisation(
            # Unique per provider instance so the real-database tests (which
            # share one migrated database) never collide on the unique mapping
            # column.
            id=f"org_workos_{uuid.uuid4().hex[:12]}",
            name=name,
            external_id=external_id,
        )
        self.created[external_id] = organisation
        if self._state is not None:
            self._state.workos_organisations[external_id] = organisation
        return organisation

    async def get_workos_organisation(self, workos_organisation_id: str) -> WorkOSOrganisation:
        for organisation in self.created.values():
            if organisation.id == workos_organisation_id:
                return organisation
        raise AssertionError(f"no fake WorkOS organisation with id {workos_organisation_id!r}")

    async def get_workos_organisation_by_external_id(
        self, external_id: str
    ) -> WorkOSOrganisation | None:
        return self.created.get(external_id)


class FakeWorkOSInvitationsProvider(WorkOSInvitationsProvider):
    """In-memory stand-in for the WorkOS invitations adapter.

    Records sent invitations so tests can assert the send/revoke round-trip
    without a network or an API key; with a ``ContextState`` the sent
    invitations are also recorded on the state for the endpoint tests.
    """

    def __init__(self, state: ContextState | None = None) -> None:
        self._state = state
        self._counter = 0
        self.sent: dict[str, WorkOSInvitation] = {}
        self.revoked: list[str] = []

    async def send_invitation(self, *, email: str, organisation_id: str) -> WorkOSInvitation:
        self._counter += 1
        invitation = WorkOSInvitation(
            id=f"inv_workos_{uuid.uuid4().hex[:12]}",
            email=email,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        self.sent[invitation.id] = invitation
        if self._state is not None:
            self._state.workos_invitations.append(invitation)
        return invitation

    async def revoke_invitation(self, workos_invitation_id: str) -> None:
        self.revoked.append(workos_invitation_id)
        if self._state is not None:
            self._state.revoked_workos_invitations.append(workos_invitation_id)

    async def get_invitation(self, workos_invitation_id: str) -> WorkOSInvitation | None:
        return self.sent.get(workos_invitation_id)


def build_context_app(
    *,
    private_key: rsa.RSAPrivateKey,
    state: ContextState,
) -> FastAPI:
    """Build the app with fakes, adding a probe route for the membership dependency."""
    app = create_app()

    def _probe(
        membership: Annotated[OrganisationMembership, Depends(get_current_membership)],
    ) -> dict[str, str]:
        return {"organisation_id": str(membership.organisation_id)}

    app.add_api_route("/_test/context", _probe, methods=["GET"])

    async def override_db() -> AsyncIterator[FakeSession]:
        yield FakeSession(state)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_validator] = lambda: build_validator(private_key)
    app.dependency_overrides[get_user_profile_client] = lambda: FakeProfileClient(state)
    app.dependency_overrides[get_workos_organizations_client] = lambda: (
        FakeWorkOSOrganizationsProvider(state)
    )
    app.dependency_overrides[get_workos_invitations_client] = lambda: FakeWorkOSInvitationsProvider(
        state
    )
    return app


def context_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


ContextApp = tuple[FastAPI, ContextState, rsa.RSAPrivateKey]


def build_context_app_fixture() -> ContextApp:
    """Configure an app with an in-memory context state, ready for a request."""
    private_key, _ = generate_key_pair()
    state = ContextState(owner_role=make_owner_role())
    app = build_context_app(private_key=private_key, state=state)
    return app, state, private_key
