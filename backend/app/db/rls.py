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
  request body or broker argument.
- The records/files/notifications services keep their post-write refresh inside
  the same transaction as the write, so they never need a second,
  automatically re-contextualised transaction.
- Binding happens only after the active membership has been validated (see
  ``app.api.dependencies.get_current_membership``); the platform, health,
  authentication and public routes never fabricate a tenant context.

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

#: Keys under which the most recently bound ids are held on ``session.info``.
#: These are convenience records for diagnostics/tests only; they are never
#: used to re-apply context to a later transaction.
_SESSION_INFO_KEY: Final = "rls_organisation_id"
_SESSION_INFO_USER_KEY: Final = "rls_user_id"

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


async def clear_organisation_context(session: AsyncSession) -> None:
    """Clear the current transaction's tenant setting explicitly.

    Exposed for tests and for handlers that deliberately end a tenant scope
    within one session. Ordinary request/worker sessions do not need to call
    this: the setting is transaction-local and clears on the next boundary.
    """
    session.info.pop(_SESSION_INFO_KEY, None)
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_ORGANISATION_SETTING, "value": ""})


async def clear_user_context(session: AsyncSession) -> None:
    """Clear the current transaction's user setting explicitly."""
    session.info.pop(_SESSION_INFO_USER_KEY, None)
    await session.execute(_SET_LOCAL_SQL, {"setting": RLS_USER_SETTING, "value": ""})


def bound_organisation_id(session: AsyncSession) -> str | None:
    """Return the most recently bound tenant id, if one has been bound."""
    value = session.info.get(_SESSION_INFO_KEY)
    return str(value) if value is not None else None


def bound_user_id(session: AsyncSession) -> str | None:
    """Return the most recently bound user id, if one has been bound."""
    value = session.info.get(_SESSION_INFO_USER_KEY)
    return str(value) if value is not None else None
