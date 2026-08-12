"""Private storage reference → bounded attachment resolution (v0.7 Scope §6.4).

The v0.7 attachment contract (ADR-0017 amendment, BP §23) lets a feature
supply a private ``storage_reference`` for document-scale work; this module is
the service/job boundary that *authorises and resolves* that object server-side
into a provider-neutral :class:`~app.ai.attachments.Attachment` carrying only a
validated display name, MIME type, bytes and SHA-256 digest.

The resolver consumes an :class:`AttachmentResolutionContext` — the request's
validated organisation id plus the reference — never a bare key, so the
tenant-isolation boundary is enforceable: the reference must live in the
requesting organisation's own namespace (``organisations/{organisation_id}/…``,
the server-generated key format from Scope §6.3) or it fails closed before any
storage metadata is read. A cross-organisation reference is denied, never
resolved.

The resolver is deliberately small and fail-closed:

- it heads the object first so a missing reference becomes the safe AI
  input-validation error before any bytes are read;
- the object's stored content type must be on the template allowlist, or the
  reference is rejected before dispatch — a storage object with an unknown or
  unsafe MIME type can never reach a provider;
- the template limits (5 MB per file / 10 MB combined, bounded count) are
  enforced from the head metadata *before* reading (an oversized object is
  rejected without allocating its bytes) and again by
  :class:`Attachment` / ``validate_attachment_set`` after a bounded read, so a
  head/read race still fails bounded;
- the SHA-256 digest is computed from the carried bytes by :class:`Attachment`
  itself, so the recorded digest is always correct;
- only approved metadata propagates: the adapter receives the display name and
  bytes, never the object key, a signed URL or any storage credential.

Attachment bytes exist only in memory for the duration of one provider call
and are never persisted, placed on the broker, or logged (BP §28, ADR-0017).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.ai.attachments import (
    ALLOWED_ATTACHMENT_MIME_TYPES,
    MAX_ATTACHMENT_BYTES,
    Attachment,
    validate_attachment_set,
)
from app.ai.errors import AIInputValidationError
from app.storage.base import ObjectStorage


#: The validated context the service/job boundary supplies to a resolver
#: (ADR-0017: the boundary authorises *and* resolves the object). The
#: organisation id is the caller's validated context, never client-supplied,
#: and is what makes cross-organisation resolution structurally impossible.
class AttachmentResolutionContext(BaseModel):
    reference: str = Field(min_length=1, max_length=1024)
    organisation_id: UUID

    @field_validator("reference")
    @classmethod
    def _reference_is_bounded(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("storage reference must not contain whitespace")
        return value


#: A resolver maps one authorised private storage reference to a validated
#: attachment set. The service/job boundary owns this mapping (ADR-0017):
#: application code names a reference, never a provider or an object path.
AttachmentResolver = Callable[[AttachmentResolutionContext], Awaitable[Sequence[Attachment]]]

#: Fallback content types for objects whose stored content type is absent;
#: only allowlisted types are ever produced, anything else fails closed.
EXTENSION_MIME_TYPES = {
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".txt": "text/plain",
}


class StorageAttachmentResolver:
    """Resolve an organisation-authorised object key into attachments.

    Implements :data:`AttachmentResolver` directly (``await resolver(context)``
    == ``await resolver.resolve(context)``) so a wired instance is a valid
    service ``attachment_resolver``.
    """

    def __init__(self, storage: ObjectStorage) -> None:
        self._storage = storage

    async def __call__(self, context: AttachmentResolutionContext) -> Sequence[Attachment]:
        return await self.resolve(context)

    async def resolve(self, context: AttachmentResolutionContext) -> Sequence[Attachment]:
        """Resolve one authorised storage reference to a validated attachment set.

        The reference must sit in the requesting organisation's own namespace
        (``organisations/{organisation_id}/…``); anything else is a
        cross-organisation denial that fails closed before any metadata is
        read. Raises :class:`~app.ai.errors.AIInputValidationError` with a safe
        message when the object is missing, too large, or carries an
        unsupported MIME type — always before any provider dispatch, and never
        echoing the private reference (BP §28 never-log list).
        """
        reference = context.reference
        if not reference.startswith(f"organisations/{context.organisation_id}/"):
            raise AIInputValidationError(
                "the referenced storage object is not accessible to this organisation"
            )
        info = await self._storage.head_object(reference)
        if info is None:
            raise AIInputValidationError("the referenced storage object does not exist")
        mime_type = self._mime_type(info.content_type, reference)
        # Reject from head metadata before any bytes are read: an oversized
        # object must never be allocated into worker memory (Scope §6.4
        # bounded-memory contract).
        if info.size_bytes > MAX_ATTACHMENT_BYTES:
            raise AIInputValidationError("the referenced storage object is too large")
        try:
            attachment = Attachment(
                display_name=self._display_name(reference),
                mime_type=mime_type,
                # The bounded read enforces the same ceiling *during* the read,
                # so a head/read race (the object grew after head_object) still
                # fails bounded instead of allocating arbitrary memory.
                content=await self._storage.read_object(reference, max_bytes=MAX_ATTACHMENT_BYTES),
            )
        except KeyError as exc:
            raise AIInputValidationError("the referenced storage object could not be read") from exc
        except (ValueError, ValidationError) as exc:
            raise AIInputValidationError(
                "the referenced storage object is not a valid attachment"
            ) from exc
        try:
            return validate_attachment_set([attachment])
        except ValueError as exc:
            raise AIInputValidationError(str(exc)) from exc

    @staticmethod
    def _display_name(reference: str) -> str:
        """Derive the bare approved display name from the object key's tail."""
        return Path(reference.rstrip("/")).name or "document"

    @staticmethod
    def _mime_type(content_type: str | None, reference: str) -> str:
        """The allowlisted MIME type for an object, failing closed otherwise."""
        if content_type:
            candidate = content_type.strip().lower().split(";")[0]
            if candidate in ALLOWED_ATTACHMENT_MIME_TYPES:
                return candidate
            raise AIInputValidationError(
                "the referenced storage object has an unsupported content type"
            )
        fallback = EXTENSION_MIME_TYPES.get(Path(reference.rstrip("/")).suffix.lower())
        if fallback is None:
            raise AIInputValidationError(
                "the referenced storage object declares no supported content type"
            )
        return fallback
