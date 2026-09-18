"""Reusable real-PostgreSQL two-organisation fixture for the P2 matrix.

The plan P2 checkpoint requires a real-database fixture containing:

- organisation A and organisation B;
- an A-only user and a B-only user;
- a user who is owner in A but viewer in B (the multi-membership user);
- a suspended membership; and
- a platform-only user.

This module builds that world and the ASGI harness around it. It is the
real-database counterpart to ``context_helpers.py``: the real migrated
PostgreSQL is wired into the app through the ``get_db`` override, while the
WorkOS session is validated against a local RSA key and the profile/invitation
adapters are in-memory stand-ins, so the matrix needs no network.

The functions here are pure helpers; ``test_org_isolation_matrix_db.py`` wires
them into pytest fixtures. Keeping the seeding and app assembly here makes the
fixture reusable by any later tenant-isolation or protected-route work unit.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from tests.auth_helpers import build_validator, make_token

from app.ai.persistence.models import (
    AIAttachmentReference,
    AIRequestRecord,
    AIRequestStatus,
    AIScratchUpload,
    AIScratchUploadStatus,
    OrganisationAISettings,
)
from app.ai.transfer import TransferMode, derive_idempotency_key
from app.api.dependencies import get_db
from app.core.security import UserProfile, get_session_validator, get_user_profile_client
from app.integrations.workos.invitations import get_workos_invitations_client
from app.main import create_app
from app.modules.files.models import File, FileStatus
from app.modules.jobs.models import Job, JobStatus
from app.modules.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationDeliveryStatus,
)
from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.permissions.constants import PLATFORM_ADMIN_ROLE_CODE
from app.modules.permissions.models import MembershipRole, Role
from app.modules.platform_admin.models import PlatformMembership, PlatformRole
from app.modules.records.models import Record
from app.modules.users.models import User

#: HTTP status codes the matrix asserts, named so the intent is legible.
NOT_FOUND = 404
FORBIDDEN = 403


@dataclass(frozen=True)
class Identity:
    """One seeded user and the WorkOS subject its token must carry."""

    user_id: uuid.UUID
    workos_user_id: str

    @property
    def email(self) -> str:
        return f"{self.workos_user_id}@example.com"


@dataclass(frozen=True)
class IsolationWorld:
    """Identifiers for one freshly seeded two-organisation world.

    Every test seeds its own world (via a function-scoped fixture) so tests
    cannot interfere: the organisations and rows are unique, which keeps list
    totals and pagination assertions deterministic inside a test.
    """

    org_a: uuid.UUID
    org_b: uuid.UUID
    a_owner: Identity
    b_owner: Identity
    multi: Identity
    suspended: Identity
    platform_only: Identity
    record_a: uuid.UUID
    record_b: uuid.UUID
    file_a: uuid.UUID
    file_b: uuid.UUID
    job_a: uuid.UUID
    job_b: uuid.UUID
    notification_a: uuid.UUID
    notification_b: uuid.UUID
    ai_request_a: str
    ai_request_b: str
    scratch_upload_a: uuid.UUID
    scratch_upload_b: uuid.UUID
    delivery_a: uuid.UUID
    delivery_b: uuid.UUID
    reference_request_a: str
    reference_request_b: str
    reference_key_a: str
    reference_key_b: str
    reference_digest_a: str
    reference_digest_b: str


def _make_user(session: AsyncSession, label: str) -> User:
    """Build and stage one user with a unique WorkOS subject."""
    workos_user_id = f"user_{label}_{uuid.uuid4().hex[:10]}"
    user = User(
        workos_user_id=workos_user_id,
        email=f"{workos_user_id}@example.com",
        name=label,
    )
    session.add(user)
    return user


async def seed_isolation_world(database_url: str) -> IsolationWorld:
    """Seed the two-organisation isolation world and return its identifiers.

    The migration has already seeded the role catalogue, so the memberships are
    granted roles by code. The world is committed once at the end; the returned
    object holds only plain ids/values so no ORM instance escapes this session.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            roles = {role.code: role for role in (await session.scalars(select(Role))).all()}
            platform_role = await session.scalar(
                select(PlatformRole).where(PlatformRole.code == PLATFORM_ADMIN_ROLE_CODE)
            )
            assert platform_role is not None, "the platform_admin role must be seeded"

            org_a = Organisation(name=f"Isolation A {uuid.uuid4().hex[:8]}")
            org_b = Organisation(name=f"Isolation B {uuid.uuid4().hex[:8]}")
            session.add_all([org_a, org_b])

            a_owner = _make_user(session, "a_owner")
            b_owner = _make_user(session, "b_owner")
            multi = _make_user(session, "multi")
            suspended = _make_user(session, "suspended")
            platform_only = _make_user(session, "platform")
            await session.flush()

            membership_a_owner = OrganisationMembership(
                user_id=a_owner.id,
                organisation_id=org_a.id,
                status=MembershipStatus.ACTIVE,
            )
            membership_b_owner = OrganisationMembership(
                user_id=b_owner.id,
                organisation_id=org_b.id,
                status=MembershipStatus.ACTIVE,
            )
            membership_multi_a = OrganisationMembership(
                user_id=multi.id,
                organisation_id=org_a.id,
                status=MembershipStatus.ACTIVE,
            )
            membership_multi_b = OrganisationMembership(
                user_id=multi.id,
                organisation_id=org_b.id,
                status=MembershipStatus.ACTIVE,
            )
            membership_suspended = OrganisationMembership(
                user_id=suspended.id,
                organisation_id=org_a.id,
                status=MembershipStatus.SUSPENDED,
            )
            session.add_all(
                [
                    membership_a_owner,
                    membership_b_owner,
                    membership_multi_a,
                    membership_multi_b,
                    membership_suspended,
                ]
            )
            await session.flush()

            session.add_all(
                [
                    MembershipRole(membership_id=membership_a_owner.id, role_id=roles["owner"].id),
                    MembershipRole(membership_id=membership_b_owner.id, role_id=roles["owner"].id),
                    # The multi-membership user is owner in A and viewer in B:
                    # the two memberships must grant different capabilities.
                    MembershipRole(membership_id=membership_multi_a.id, role_id=roles["owner"].id),
                    MembershipRole(membership_id=membership_multi_b.id, role_id=roles["viewer"].id),
                    MembershipRole(
                        membership_id=membership_suspended.id, role_id=roles["owner"].id
                    ),
                ]
            )
            session.add(
                PlatformMembership(user_id=platform_only.id, platform_role_id=platform_role.id)
            )
            # AI is default-deny; enable it for both tenants so the scratch/
            # classify surfaces are exercisable, while still proving that the
            # cross-tenant lookups are org-scoped.
            session.add_all(
                [
                    OrganisationAISettings(organisation_id=org_a.id, enabled=True),
                    OrganisationAISettings(organisation_id=org_b.id, enabled=True),
                ]
            )

            record_a = Record(organisation_id=org_a.id, title="A record", body="A body")
            record_b = Record(organisation_id=org_b.id, title="B record", body="B body")
            file_a = File(
                organisation_id=org_a.id,
                storage_provider="fake",
                storage_bucket="test-bucket",
                object_key=f"organisations/{org_a.id}/documents/{uuid.uuid4()}/original",
                original_filename="a.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                status=FileStatus.UPLOADED,
            )
            file_b = File(
                organisation_id=org_b.id,
                storage_provider="fake",
                storage_bucket="test-bucket",
                object_key=f"organisations/{org_b.id}/documents/{uuid.uuid4()}/original",
                original_filename="b.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                status=FileStatus.UPLOADED,
            )
            job_a = Job(
                organisation_id=org_a.id,
                job_type="file.processing",
                status=JobStatus.SUCCEEDED,
                progress=100,
                input_reference=str(file_a.id),
                attempt_count=1,
            )
            job_b = Job(
                organisation_id=org_b.id,
                job_type="file.processing",
                status=JobStatus.SUCCEEDED,
                progress=100,
                input_reference=str(file_b.id),
                attempt_count=1,
            )
            notification_a = Notification(
                organisation_id=org_a.id,
                user_id=a_owner.id,
                type="notification.test_sent",
                title="A notification",
                body="A body",
            )
            notification_b = Notification(
                organisation_id=org_b.id,
                user_id=b_owner.id,
                type="notification.test_sent",
                title="B notification",
                body="B body",
            )
            request_id_a = uuid.uuid4().hex
            request_id_b = uuid.uuid4().hex
            ai_request_a = AIRequestRecord(
                organisation_id=org_a.id,
                user_id=a_owner.id,
                request_id=request_id_a,
                attempt_number=1,
                task="document.classify",
                provider="fake",
                model="fake.document-classifier",
                prompt_name="document.classify",
                prompt_version=1,
                routing_reason="seeded",
                status=AIRequestStatus.FAILED,
                error_code="provider_error",
            )
            ai_request_b = AIRequestRecord(
                organisation_id=org_b.id,
                user_id=b_owner.id,
                request_id=request_id_b,
                attempt_number=1,
                task="document.classify",
                provider="fake",
                model="fake.document-classifier",
                prompt_name="document.classify",
                prompt_version=1,
                routing_reason="seeded",
                status=AIRequestStatus.FAILED,
                error_code="provider_error",
            )
            upload_id_a = uuid.uuid4()
            upload_id_b = uuid.uuid4()
            scratch_a = AIScratchUpload(
                organisation_id=org_a.id,
                upload_id=upload_id_a,
                object_key=f"organisations/{org_a.id}/ai/scratch/{upload_id_a}.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                status=AIScratchUploadStatus.PENDING,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            scratch_b = AIScratchUpload(
                organisation_id=org_b.id,
                upload_id=upload_id_b,
                object_key=f"organisations/{org_b.id}/ai/scratch/{upload_id_b}.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                status=AIScratchUploadStatus.PENDING,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            # Representative indirect rows so the matrix can exercise the real
            # organisation check behind each named path instead of asserting
            # that table names are absent from route templates: a durable
            # notification delivery (owned through its parent notification) and
            # a durable AI transfer reference (read only through the
            # organisation-scoped transfer store).
            reference_request_a = "matrix-reference-a"
            reference_request_b = "matrix-reference-b"
            reference_digest_a = "a1" * 32
            reference_digest_b = "b2" * 32
            reference_key_a = derive_idempotency_key(
                provider="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                organisation_id=org_a.id,
                logical_request_id=reference_request_a,
                source_digest=reference_digest_a,
                region="eu-west-1",
            )
            reference_key_b = derive_idempotency_key(
                provider="fake",
                mode=TransferMode.PROVIDER_UPLOAD,
                organisation_id=org_b.id,
                logical_request_id=reference_request_b,
                source_digest=reference_digest_b,
                region="eu-west-1",
            )
            reference_a = AIAttachmentReference(
                organisation_id=org_a.id,
                logical_request_id=reference_request_a,
                provider="fake",
                transfer_mode=TransferMode.PROVIDER_UPLOAD.value,
                external_id=f"fake-a-{uuid.uuid4().hex[:12]}",
                source_reference=f"organisations/{org_a.id}/documents/{uuid.uuid4()}/original",
                source_digest=reference_digest_a,
                size_bytes=1600,
                mime_type="application/pdf",
                source_lifecycle="transient",
                region="eu-west-1",
                status="live",
                idempotency_key=reference_key_a,
            )
            reference_b = AIAttachmentReference(
                organisation_id=org_b.id,
                logical_request_id=reference_request_b,
                provider="fake",
                transfer_mode=TransferMode.PROVIDER_UPLOAD.value,
                external_id=f"fake-b-{uuid.uuid4().hex[:12]}",
                source_reference=f"organisations/{org_b.id}/documents/{uuid.uuid4()}/original",
                source_digest=reference_digest_b,
                size_bytes=1600,
                mime_type="application/pdf",
                source_lifecycle="transient",
                region="eu-west-1",
                status="live",
                idempotency_key=reference_key_b,
            )
            session.add_all(
                [
                    record_a,
                    record_b,
                    file_a,
                    file_b,
                    job_a,
                    job_b,
                    notification_a,
                    notification_b,
                    ai_request_a,
                    ai_request_b,
                    scratch_a,
                    scratch_b,
                    reference_a,
                    reference_b,
                ]
            )
            await session.flush()
            delivery_a = NotificationDelivery(
                notification_id=notification_a.id,
                channel="email",
                recipient=a_owner.email,
                status=NotificationDeliveryStatus.QUEUED,
            )
            delivery_b = NotificationDelivery(
                notification_id=notification_b.id,
                channel="email",
                recipient=b_owner.email,
                status=NotificationDeliveryStatus.QUEUED,
            )
            session.add_all([delivery_a, delivery_b])
            await session.commit()

            return IsolationWorld(
                org_a=org_a.id,
                org_b=org_b.id,
                a_owner=Identity(user_id=a_owner.id, workos_user_id=a_owner.workos_user_id),
                b_owner=Identity(user_id=b_owner.id, workos_user_id=b_owner.workos_user_id),
                multi=Identity(user_id=multi.id, workos_user_id=multi.workos_user_id),
                suspended=Identity(user_id=suspended.id, workos_user_id=suspended.workos_user_id),
                platform_only=Identity(
                    user_id=platform_only.id, workos_user_id=platform_only.workos_user_id
                ),
                record_a=record_a.id,
                record_b=record_b.id,
                file_a=file_a.id,
                file_b=file_b.id,
                job_a=job_a.id,
                job_b=job_b.id,
                notification_a=notification_a.id,
                notification_b=notification_b.id,
                ai_request_a=request_id_a,
                ai_request_b=request_id_b,
                scratch_upload_a=upload_id_a,
                scratch_upload_b=upload_id_b,
                delivery_a=delivery_a.id,
                delivery_b=delivery_b.id,
                reference_request_a=reference_request_a,
                reference_request_b=reference_request_b,
                reference_key_a=reference_key_a,
                reference_key_b=reference_key_b,
                reference_digest_a=reference_digest_a,
                reference_digest_b=reference_digest_b,
            )
    finally:
        await engine.dispose()


class _IsolationProfileClient:
    """Profile client echoing the seeded user's verified WorkOS profile.

    The seeded users' emails are derived from their WorkOS subject, so the
    login-time profile refresh is a no-op and the pending-invitation lookup
    (there are none) never matches across tests.
    """

    async def get_profile(self, workos_user_id: str) -> UserProfile:
        return UserProfile(
            email=f"{workos_user_id}@example.com",
            name=workos_user_id,
            email_verified=True,
        )


class _NoInvitationsProvider:
    """In-memory stand-in: the isolation world contains no pending invitations."""

    async def get_invitation(self, workos_invitation_id: str) -> None:
        return None


def build_isolation_app(database_url: str, private_key: rsa.RSAPrivateKey) -> FastAPI:
    """Build the real ASGI app against the migrated database and a local RSA key.

    The per-app NullPool engine is exposed on ``app.state.isolation_engine`` so
    the caller owns a teardown hook that disposes it deterministically.
    ``httpx.ASGITransport`` does not run the app lifespan, so a lifespan
    shutdown callback would not fire; the test fixture disposes the engine in
    its own ``finally`` instead.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app()
    app.state.isolation_engine = engine

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_session_validator] = lambda: build_validator(private_key)
    app.dependency_overrides[get_user_profile_client] = lambda: _IsolationProfileClient()
    app.dependency_overrides[get_workos_invitations_client] = lambda: _NoInvitationsProvider()
    return app


def auth_headers(
    private_key: rsa.RSAPrivateKey,
    identity: Identity,
    *,
    org_id: uuid.UUID | None = None,
) -> dict[str, str]:
    """Return Bearer (and optional ``X-Org-Id``) headers for a seeded identity."""
    headers = {
        "Authorization": f"Bearer {make_token(private_key, sub=identity.workos_user_id)}",
    }
    if org_id is not None:
        headers["X-Org-Id"] = str(org_id)
    return headers


def assert_org_scoped_row(row_organisation_id: uuid.UUID, expected_org_id: uuid.UUID) -> None:
    """Guard that a fetched row belongs to the tenant the caller selected.

    Shared test-support for the direct-row assertions the matrix makes (for
    example the organisation-scoped transfer-reference reads) and the
    deliberate-omission demonstration, which proves the guard rejects a row
    fetched without its ``organisation_id`` predicate. The API-response
    assertions use the error envelope rather than this guard.
    """
    if row_organisation_id != expected_org_id:
        raise AssertionError(
            "cross-organisation row leaked: row belongs to "
            f"{row_organisation_id} but the caller selected {expected_org_id}"
        )


async def fetch_record_without_org_predicate(
    session: AsyncSession, *, record_id: uuid.UUID
) -> Record | None:
    """Test-only lookup that deliberately omits the organisation predicate.

    This exists solely for the plan P2 checkbox 12 demonstration: the matrix's
    :func:`assert_org_scoped_row` guard must detect the leaked foreign row.
    Production code must never query a tenant-owned table this way.
    """
    return await session.scalar(select(Record).where(Record.id == record_id))


__all__ = [
    "FORBIDDEN",
    "NOT_FOUND",
    "Identity",
    "IsolationWorld",
    "assert_org_scoped_row",
    "auth_headers",
    "build_isolation_app",
    "fetch_record_without_org_predicate",
    "seed_isolation_world",
]
