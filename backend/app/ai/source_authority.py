"""Private storage source authorisation seam (plan P6, BP §9, §17, §30).

A private ``storage_reference`` is a *name*, never an authorisation. Before the
AI layer resolves one into bytes, the boundary re-resolves it against durable
application state and the caller's validated organisation context:

- a retained ``documents/`` key must belong to a live, tenant-matched ``files``
  row in an allowed lifecycle state with a pinned content identity; and
- a scratch key must be backed by a live, ready, unexpired durable scratch
  intent.

The concrete implementation lives in ``app/modules/files/authority.py`` (it
needs the files table and scratch intents); this module only declares the seam
so the provider-neutral AI layer depends on an interface, not a feature module.
``AIService`` invokes it once per execution, with the caller's session, before
any provider transfer.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


class SourceAuthorizer(Protocol):
    """Authorise a private storage reference before the AI layer reads it."""

    async def authorize(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        storage_reference: str,
    ) -> None:
        """Raise :class:`~app.ai.errors.AIInputValidationError` to deny.

        Returning normally means the reference is authorised for this
        organisation. The implementation must fail closed on an unknown,
        pending, failed, quarantined, deleted, expired or cross-organisation
        reference.
        """
