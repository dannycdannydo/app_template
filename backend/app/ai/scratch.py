"""Durable AI scratch-upload lifecycle (plan P6, v0.7 Scope §6.5).

Scratch objects (``organisations/{org}/ai/scratch/…``) are transient,
AI-owned throwaway inputs. This module is the provider-neutral platform API a
feature uses to obtain one honestly:

- :func:`create_scratch_intent` confirms the organisation's AI policy is
  enabled (default-deny), writes a durable intent with a bounded expiry and
  returns the server-generated key/upload id the feature signs a PUT for;
- :func:`complete_scratch_intent` promotes the intent to ``ready`` only after
  the caller has verified the stored object;
- :func:`authorize_scratch_reference` is the source-authority check the AI
  layer runs before resolving a scratch key: no live, ready, unexpired intent
  means the reference fails closed;
- :func:`scratch_object_key` / :func:`is_scratch_reference` are the shared
  namespace helpers (the template key format is owned by ``app.ai.transfer``).

The global maximum lifetime (``AI_SCRATCH_MAX_LIFETIME_SECONDS``) bounds every
intent independently of any optional per-organisation retention policy, so a
scratch object can never live forever. The retention sweep
(:func:`app.ai.persistence.service.enforce_ai_retention`) deletes expired
intents and their objects; object-store lifecycle rules are the backstop.

Feature modules import this module only — never the persistence internals it
delegates to (v0.8 Scope §6.1 import boundary).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.errors import AIUnavailableError
from app.ai.persistence import service as persistence
from app.ai.transfer import SCRATCH_KEY_TEMPLATE
from app.core.config import get_settings
from app.db.conventions import uuid7

#: The organisation-scoped AI scratch key prefix (re-exported from the transfer
#: contract so the classifier and this lifecycle module can never drift).
SCRATCH_KEY_PREFIX = SCRATCH_KEY_TEMPLATE


@dataclass(frozen=True)
class ScratchIntent:
    """The signed-URL target and bounded lifetime of one scratch upload."""

    upload_id: uuid.UUID
    object_key: str
    expires_at: datetime


@dataclass(frozen=True)
class ScratchIntentState:
    """The durable declared contract and lifecycle of one scratch intent.

    A feature reads this before promoting an intent to ``ready`` so completion
    can verify the stored object against the exact declared content type and
    size, refuse an expired intent, and keep the persistence row out of the
    feature's view (v0.8 Scope §6.1 import boundary).
    """

    upload_id: uuid.UUID
    object_key: str
    content_type: str
    size_bytes: int
    status: str
    expires_at: datetime


def ai_scratch_prefix(organisation_id: uuid.UUID) -> str:
    """Return the organisation-scoped scratch key prefix."""
    return SCRATCH_KEY_PREFIX.format(organisation_id=organisation_id)


def is_scratch_reference(organisation_id: uuid.UUID, reference: str) -> bool:
    """Return whether a storage reference lives in the AI scratch namespace.

    A cross-organisation scratch key is *not* a scratch reference for this
    organisation and is handled by the generic namespace denial.
    """
    return reference.startswith(ai_scratch_prefix(organisation_id))


def scratch_object_key(organisation_id: uuid.UUID, upload_id: uuid.UUID) -> str:
    """The server-generated object key for one transient scratch upload."""
    return ai_scratch_prefix(organisation_id) + f"{upload_id}.pdf"


def _bounded_expiry(
    *,
    now: datetime,
    organisation_retention_days: int | None,
) -> datetime:
    """The global maximum scratch lifetime, tightened by any org policy."""
    max_lifetime = timedelta(seconds=get_settings().ai_scratch_max_lifetime_seconds)
    if organisation_retention_days is not None:
        policy_lifetime = timedelta(days=organisation_retention_days)
        if policy_lifetime < max_lifetime:
            max_lifetime = policy_lifetime
    return now + max_lifetime


async def create_scratch_intent(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    content_type: str,
    size_bytes: int,
    now: datetime | None = None,
) -> ScratchIntent:
    """Confirm AI enablement, persist a bounded intent and return its key.

    Raises :class:`~app.ai.errors.AIUnavailableError` when the organisation's
    AI policy is disabled or missing, so issuing a scratch upload capability is
    gated by the same default-deny rule as any other AI work (plan P6).
    """
    if not await persistence.is_ai_enabled(session, organisation_id=organisation_id):
        raise AIUnavailableError("AI is not enabled for this organisation")
    created_at = now or datetime.now(UTC)
    upload_id = uuid7()
    object_key = scratch_object_key(organisation_id, upload_id)
    retention_days = await persistence.scratch_retention_days(
        session, organisation_id=organisation_id
    )
    expires_at = _bounded_expiry(now=created_at, organisation_retention_days=retention_days)
    await persistence.create_scratch_upload(
        session,
        organisation_id=organisation_id,
        upload_id=upload_id,
        object_key=object_key,
        content_type=content_type,
        size_bytes=size_bytes,
        expires_at=expires_at,
    )
    return ScratchIntent(upload_id=upload_id, object_key=object_key, expires_at=expires_at)


async def get_scratch_intent(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> ScratchIntentState | None:
    """Return the org-scoped durable intent for one caller-visible upload id.

    ``None`` means no intent exists for this organisation (unknown or
    cross-organisation upload id), which the caller maps to a safe validation
    error. The returned state carries the exact declared content type/size and
    the bounded expiry so completion can enforce the persisted contract.
    """
    row = await persistence.get_scratch_upload(
        session,
        organisation_id=organisation_id,
        upload_id=upload_id,
    )
    if row is None:
        return None
    return ScratchIntentState(
        upload_id=row.upload_id,
        object_key=row.object_key,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        status=row.status.value,
        expires_at=row.expires_at,
    )


async def complete_scratch_intent(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    upload_id: uuid.UUID,
    now: datetime | None = None,
) -> str | None:
    """Promote a pending scratch intent to ``ready``; return its object key.

    ``None`` means no pending intent exists (unknown, already terminal, or a
    cross-organisation upload id), which the caller maps to a validation error.
    """
    row = await persistence.complete_scratch_upload(
        session,
        organisation_id=organisation_id,
        upload_id=upload_id,
        now=now,
    )
    if row is None:
        return None
    return row.object_key


async def authorize_scratch_reference(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    reference: str,
    now: datetime | None = None,
) -> bool:
    """Whether a live, ready, unexpired scratch intent authorises the key."""
    row = await persistence.authorize_scratch_object(
        session,
        organisation_id=organisation_id,
        object_key=reference,
        now=now,
    )
    return row is not None
