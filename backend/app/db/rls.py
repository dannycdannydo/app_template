"""PostgreSQL Row-Level Security tenant context (plan P2/P3, ADR-0022).

The application layer remains the first enforcement layer for organisation
isolation (blueprint §9, §30). This module adds the prototype/rollout
defence-in-depth backstop: *transaction-local* PostgreSQL settings that the
default-deny policies read, so a query that forgets its ``organisation_id`` (or
``user_id``) predicate still returns no foreign rows.

Design constraints (ADR-0022 decisions 8 and 9):

- Context is set with a **parameterised** ``set_config(name, value, true)``
  call; a request value is never interpolated into SQL.
- The setting is transaction-local, so it clears automatically on commit,
  rollback, exception, cancellation, timeout and connection reuse. It is
  deliberately **not** re-applied to later transactions in the same session:
  after any transaction boundary the context is absent and a protected query is
  default-denied, so a caller that crosses a boundary must explicitly rebind
  through :func:`bind_organisation_context` / :func:`bind_user_context` (the
  context then comes from a freshly validated membership, never from stale
  session state).
- ``app.organisation_id`` is the tenant key bound after the active membership
  is validated. ``app.user_id`` is the user-private key the user-private
  notification policies additionally require; it is bound from the
  authenticated user (API) or the durable job/user row (worker), never from a
  request body or broker argument. It is also the *pre-tenant* key the
  ``organisation_memberships``/``invitations`` identity policies use while a
  membership is being resolved and before any organisation exists (ADR-0022
  decision 8).
- ``app.invitation_provider_id`` is the single-row bootstrap the signature-
  verified ``invitation.revoked`` webhook binds from the provider event id so
  it can mirror a revocation with no organisation context, mirroring the
  ``app.job_id`` worker bootstrap (plan P4, group 5).
- ``app.platform_admin`` is the operational-ledger read context the validated
  platform permission dependency binds (plan P4, group 6). It admits the
  cross-tenant and global audit history the platform audit screen lists through
  the explicit ``audit_events_platform_read`` policy (ADR-0022 decision 4).
  It is a flag, never a tenant id, and it is bound only after the platform
  permission has been validated.
- The records/files/notifications services keep their post-write refresh inside
  the same transaction as the write, so they never need a second,
  automatically re-contextualised transaction.
- Binding happens only after the active membership has been validated (see
  ``app.api.dependencies.get_current_membership``); the health, authentication
  and public routes never fabricate a tenant context. Platform routes bind no
  request-selected tenant either, with the single reviewed exception of the P3
  group-4a organisation-settings services, which bind exactly the one
  organisation the platform operation targets after the platform permission
  dependency has validated the caller (ADR-0022 decision 4).

The setting names and the policy predicates are defined once here and mirrored
by the migrations, which install the ``app_current_tenant_id()`` /
``app_current_user_id()`` helpers the policies call. Keeping the names in
single Python constants stops the write side and the tests from drifting apart.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Transaction-local PostgreSQL setting the RLS policies read. The value is the
#: organisation UUID as text; absent/empty/malformed values resolve to ``NULL``
#: in ``app_current_tenant_id()`` and therefore match no row.
RLS_ORGANISATION_SETTING: Final = "app.organisation_id"

#: Transaction-local PostgreSQL setting the user-private RLS policies read. The
#: value is the user UUID as text; absent/empty/malformed values resolve to
#: ``NULL`` in ``app_current_user_id()`` and therefore match no row.
RLS_USER_SETTING: Final = "app.user_id"

#: Transaction-local PostgreSQL setting the single-row worker-bootstrap job
#: policy reads. A worker is handed exactly one opaque job UUID by the broker
#: and must read that durable ``jobs`` row before it can know the organisation;
#: the value is the job UUID as text, and absent/empty/malformed values resolve
#: to ``NULL`` in ``app_current_job_id()`` so the bootstrap returns no row. It
#: grants no enumeration and is never treated as a tenant authority: the worker
#: then binds ``app.organisation_id`` from the durable row's own value
#: (ADR-0022 decision 3).
RLS_JOB_SETTING: Final = "app.job_id"

#: Transaction-local PostgreSQL setting the single-row webhook bootstrap
#: invitation policy reads (plan P4, group 5). A signature-verified
#: ``invitation.revoked`` delivery names exactly one opaque WorkOS invitation
#: id and must read/revoke that local invitation before any organisation
#: context exists. The value is the provider invitation id as text, and
#: absent/empty values resolve to ``NULL`` in
#: ``app_current_invitation_provider_id()`` so the bootstrap returns no row. It
#: grants no enumeration and is never treated as tenant authority: it admits
#: only the one row the verified provider event names (ADR-0022 decision 8's
#: single-row bootstrap pattern, applied to the webhook control-plane path).
RLS_INVITATION_PROVIDER_SETTING: Final = "app.invitation_provider_id"

#: Transaction-local PostgreSQL setting the operational-ledger policies read
#: (plan P4, group 6). A validated platform administrator binds it after the
#: platform permission dependency has authorised the caller, so the
#: ``audit_events`` platform-read policy can admit the cross-tenant and global
#: audit history the platform screen exists to list. It is deliberately a plain
#: flag, not an arbitrary tenant id: the runtime role can never set a tenant
#: context it was not authorised for, and no ordinary tenant request binds it.
#: Absent/empty/malformed values resolve to ``false`` in
#: ``app_current_platform_admin()`` and therefore admit no cross-tenant row.
RLS_PLATFORM_SETTING: Final = "app.platform_admin"

#: Keys under which the most recently bound ids are held on ``session.info``.
#: These are convenience records for diagnostics/tests only; they are never
#: used to re-apply context to a later transaction.
_SESSION_INFO_KEY: Final = "rls_organisation_id"
_SESSION_INFO_USER_KEY: Final = "rls_user_id"
_SESSION_INFO_JOB_KEY: Final = "rls_job_id"
_SESSION_INFO_INVITATION_KEY: Final = "rls_invitation_provider_id"
_SESSION_INFO_PLATFORM_KEY: Final = "rls_platform_admin"

_SET_LOCAL_SQL = text("SELECT set_config(:setting, :value, true)")


def _validated_uuid(value: uuid.UUID | str) -> str:
    """Return the canonical text form of a UUID, rejecting malformed input.

    Validation happens in Python as well as in the policy helper so a caller
    cannot bind an arbitrary string that would only be caught later by a
    database cast. A malformed id raises ``ValueError`` before any SQL runs.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(uuid.UUID(str(value)))


def _is_database_backed(session: AsyncSession) -> bool:
    """Return whether the session has a real DBAPI-backed transaction.

    Request-flow test doubles implement the small part of the session surface
    the services use but have no underlying connection, so there is no
    transaction-local context to bind (and no RLS policy to satisfy). Such a
    session is skipped rather than assumed to support ``sync_session``.
    """
    return getattr(session, "sync_session", None) is not None


async def bind_organisation_context(
    session: AsyncSession, organisation_id: uuid.UUID | str
) -> None:
    """Bind a validated organisation to the session's *current* transaction.

    Callers must only reach this after the active membership (and therefore
    the selected organisation) has been validated; the value is never taken
    directly from a request body or an ``X-Org-Id`` header without that check.
    The setting is transaction-local: it is cleared by the next commit,
    rollback or exception and is never re-applied automatically. A caller that
    starts a new transaction under a tenant scope must call this again with a
    freshly validated organisation.
    """
    if not _is_database_backed(session):
        return
    tenant_id = _validated_uuid(organisation_id)
    session.info[_SESSION_INFO_KEY] = tenant_id
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_ORGANISATION_SETTING, "value": tenant_id})


async def bind_user_context(session: AsyncSession, user_id: uuid.UUID | str) -> None:
    """Bind a validated user to the session's *current* transaction.

    The user-private policies (``notifications`` and its delivery rows) require
    both ``app.organisation_id`` and ``app.user_id``. The value must come from
    the authenticated user (API) or the durable job/user row (worker), never
    from a request body or broker argument. Like the tenant setting it is
    transaction-local and is never re-applied automatically: a caller that
    starts a new transaction under a user scope must bind it again.
    """
    if not _is_database_backed(session):
        return
    user_id_text = _validated_uuid(user_id)
    session.info[_SESSION_INFO_USER_KEY] = user_id_text
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_USER_SETTING, "value": user_id_text})


async def bind_job_context(session: AsyncSession, job_id: uuid.UUID | str) -> None:
    """Bind the opaque job id a worker is bootstrapping from.

    The ``jobs`` worker-bootstrap policy admits exactly the one row whose
    ``id`` matches the transaction-local ``app.job_id``, so a worker can read
    its durable row before any tenant context exists (ADR-0022 decision 3).
    The value is the broker message's opaque id and is never treated as tenant
    authority: the worker validates the row through the claim/fencing path and
    only then binds the organisation its own row carries. Like the other
    settings it is transaction-local and is never re-applied automatically.
    """
    if not _is_database_backed(session):
        return
    job_id_text = _validated_uuid(job_id)
    session.info[_SESSION_INFO_JOB_KEY] = job_id_text
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_JOB_SETTING, "value": job_id_text})


async def bind_invitation_provider_context(
    session: AsyncSession, provider_invitation_id: str
) -> None:
    """Bind the opaque provider invitation id a webhook is reacting to.

    The ``invitations`` webhook-bootstrap policy admits exactly the one row
    whose ``workos_invitation_id`` matches the transaction-local
    ``app.invitation_provider_id``, so the signature-verified
    ``invitation.revoked`` consumer can mirror a provider revocation while no
    organisation context exists (plan P4, group 5). The value is the verified
    event's provider invitation id and is never treated as tenant authority: it
    admits one row by an unguessable provider id and grants no enumeration.
    Like the other settings it is transaction-local and is never re-applied
    automatically. A blank value is ignored (fail closed): the setting is left
    unset rather than bound, so the policy matches no row.
    """
    if not _is_database_backed(session):
        return
    value = provider_invitation_id.strip()
    if not value:
        return
    session.info[_SESSION_INFO_INVITATION_KEY] = value
    await session.execute(
        _SET_LOCAL_SQL,
        {"setting": RLS_INVITATION_PROVIDER_SETTING, "value": value},
    )


async def bind_platform_context(session: AsyncSession) -> None:
    """Bind the validated platform-administrator read context.

    The operational-ledger policies (plan P4, group 6) have no tenant key to
    filter on for the platform audit screen: the platform administrator lists
    the audit history across every organisation. After
    ``require_platform_permission`` has authorised the caller, it binds this
    transaction-local flag so the explicit ``audit_events_platform_read`` policy
    admits those rows (ADR-0022 decision 4). It is a flag, never a tenant id:
    the ordinary runtime role still cannot set an arbitrary organisation, and
    the tenant-scoped policies on every other protected table are unaffected.
    Like the other settings it is transaction-local and is never re-applied
    automatically.
    """
    if not _is_database_backed(session):
        return
    session.info[_SESSION_INFO_PLATFORM_KEY] = "true"
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_PLATFORM_SETTING, "value": "true"})


async def clear_organisation_context(session: AsyncSession) -> None:
    """Clear the current transaction's tenant setting explicitly.

    Exposed for tests and for handlers that deliberately end a tenant scope
    within one session. Ordinary request/worker sessions do not need to call
    this: the setting is transaction-local and clears on the next boundary.
    """
    if not _is_database_backed(session):
        return
    session.info.pop(_SESSION_INFO_KEY, None)
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_ORGANISATION_SETTING, "value": ""})


async def clear_user_context(session: AsyncSession) -> None:
    """Clear the current transaction's user setting explicitly."""
    if not _is_database_backed(session):
        return
    session.info.pop(_SESSION_INFO_USER_KEY, None)
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_USER_SETTING, "value": ""})


async def clear_job_context(session: AsyncSession) -> None:
    """Clear the current transaction's worker-bootstrap job setting explicitly.

    The worker clears ``app.job_id`` before it binds the organisation context
    derived from the durable row, so the bootstrap setting cannot linger as a
    second identity inside the tenant-scoped phase (ADR-0022 decision 3). The
    setting is transaction-local, so ordinary sessions do not need to call it.
    """
    if not _is_database_backed(session):
        return
    session.info.pop(_SESSION_INFO_JOB_KEY, None)
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_JOB_SETTING, "value": ""})


async def clear_invitation_provider_context(session: AsyncSession) -> None:
    """Clear the current transaction's webhook invitation-bootstrap setting.

    The webhook consumer binds the verified event's provider invitation id only
    for the one revocation statement; the setting is transaction-local, so
    ordinary sessions do not need to call this.
    """
    if not _is_database_backed(session):
        return
    session.info.pop(_SESSION_INFO_INVITATION_KEY, None)
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_INVITATION_PROVIDER_SETTING, "value": ""})


async def clear_platform_context(session: AsyncSession) -> None:
    """Clear the current transaction's platform-administrator read context.

    Exposed for tests and for request paths that must end the platform scope
    deliberately. Ordinary platform requests do not need to call this: the
    setting is transaction-local and clears on the next boundary.
    """
    if not _is_database_backed(session):
        return
    session.info.pop(_SESSION_INFO_PLATFORM_KEY, None)
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_PLATFORM_SETTING, "value": ""})


def bound_organisation_id(session: AsyncSession) -> str | None:
    """Return the most recently bound tenant id, if one has been bound."""
    value = session.info.get(_SESSION_INFO_KEY)
    return str(value) if value is not None else None


def bound_user_id(session: AsyncSession) -> str | None:
    """Return the most recently bound user id, if one has been bound."""
    value = session.info.get(_SESSION_INFO_USER_KEY)
    return str(value) if value is not None else None


def bound_job_id(session: AsyncSession) -> str | None:
    """Return the most recently bound bootstrap job id, if one has been bound."""
    value = session.info.get(_SESSION_INFO_JOB_KEY)
    return str(value) if value is not None else None


def bound_invitation_provider_id(session: AsyncSession) -> str | None:
    """Return the most recently bound webhook invitation id, if any."""
    value = session.info.get(_SESSION_INFO_INVITATION_KEY)
    return str(value) if value is not None else None


def bound_platform_context(session: AsyncSession) -> bool:
    """Return whether the platform-administrator read context is bound."""
    return session.info.get(_SESSION_INFO_PLATFORM_KEY) == "true"
