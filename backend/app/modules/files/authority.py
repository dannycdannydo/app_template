"""Document source authorisation (plan P6, BP §9, §17, §30).

One service answers the question the AI layer, job retries and the download
endpoint must all ask before treating stored bytes as trusted input: *is this
private storage reference backed by a live, tenant-matched application record
in a state that permits reading?*

- A retained document key (``organisations/{org}/documents/…``) must resolve to
  a non-deleted ``files`` row in the same organisation, in the ``ready``
  lifecycle state, whose pinned ``content_identity`` still matches the stored
  object. A prefix check, an object HEAD, a MIME type or a size are **not**
  authorisation (the finding this work unit closes): the database row is.
- A scratch key (``organisations/{org}/ai/scratch/…``) must be backed by a
  live, ready, unexpired durable scratch intent (``app.ai.scratch``).

Anything else — unknown keys, pending/failed/quarantined/deleted documents,
expired scratch, cross-organisation keys — fails closed with the same safe
input-validation error, never echoing the key (BP §28). The implementation
depends on the provider-neutral AI error taxonomy so ``AIService`` can map it
into the standard API error surface untouched.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import scratch as ai_scratch
from app.ai.errors import AIInputValidationError
from app.modules.files.models import File, FileStatus
from app.modules.files.queries import file_by_object_key_statement
from app.storage import get_storage

#: The lifecycle states whose bytes may be treated as trusted AI/download
#: input. ``ready`` is post-verification and post-scan-verdict; every other
#: state (pending/uploaded/processing/failed/quarantined/deleted) fails closed.
TRUSTED_DOCUMENT_STATUSES = frozenset({FileStatus.READY})


def _denied() -> AIInputValidationError:
    """One safe denial that never reveals whether the key exists (BP §28)."""
    return AIInputValidationError(
        "the referenced storage object is not accessible to this organisation"
    )


def document_key_prefix(organisation_id: uuid.UUID) -> str:
    """The organisation-scoped retained-document key prefix."""
    return f"organisations/{organisation_id}/documents/"


class DocumentSourceAuthority:
    """The concrete :class:`~app.ai.source_authority.SourceAuthorizer`.

    Stateless and process-safe: it reads the caller's session and the
    provider-neutral storage head only, so it can back the process-wide
    ``AIService`` instance (``app.ai.runtime.get_ai_service``).
    """

    async def authorize(
        self,
        *,
        session: AsyncSession,
        organisation_id: uuid.UUID,
        storage_reference: str,
    ) -> None:
        if ai_scratch.is_scratch_reference(organisation_id, storage_reference):
            if await ai_scratch.authorize_scratch_reference(
                session,
                organisation_id=organisation_id,
                reference=storage_reference,
            ):
                return
            raise _denied()
        if not storage_reference.startswith(document_key_prefix(organisation_id)):
            # Unknown namespace or cross-organisation reference: deny before
            # any metadata is read.
            raise _denied()
        file = await session.scalar(
            file_by_object_key_statement(organisation_id, storage_reference)
        )
        await self.authorize_file(organisation_id=organisation_id, file=file)

    async def authorize_file(
        self,
        *,
        organisation_id: uuid.UUID,
        file: File | None,
    ) -> None:
        """Apply the shared document decision to an already-resolved File row.

        This is the one lifecycle/identity boundary the reference-based
        ``authorize`` above, inline AI, streamed AI, job retries and download
        all reach: the row must be tenant-matched, in an allowed lifecycle
        state and pinned to a content identity that still matches the stored
        object. Callers translate the denial into their own error surface
        (the AI taxonomy for AI reads, a 409 for download).
        """
        if (
            file is None
            or file.organisation_id != organisation_id
            or file.status not in TRUSTED_DOCUMENT_STATUSES
            or file.content_identity is None
        ):
            raise _denied()
        # The pinned identity is the immutability check: a same-key overwrite
        # after approval changes the provider checksum and fails closed here.
        info = await get_storage().head_object(file.object_key)
        if info is None or info.checksum is None or info.checksum != file.content_identity:
            raise _denied()
