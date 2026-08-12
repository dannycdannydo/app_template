"""Bounded streaming of private sources into secure temporary files (v0.8 Scope §2.3, §6.3).

The v0.8 large-file path never accumulates a 50 MB object in Python memory
(Scope §2.3, §5.3): :class:`StreamedSource` is the service/job seam that turns
an organisation-authorised private ``storage_reference`` into a verified,
bounded, on-disk copy whose metadata (ownership, size, MIME type and SHA-256
digest) has been checked *before* and *during* the stream.

The seam is deliberately small and fail-closed, mirroring the inline
:class:`~app.ai.storage_resolver.StorageAttachmentResolver`:

- the reference must live in the requesting organisation's own namespace
  (``organisations/{organisation_id}/…``) or it fails closed before any
  metadata is read — a cross-organisation reference is denied, never resolved;
- the object is headed first so a missing reference becomes a safe AI error
  and an oversized object is rejected *from the head metadata* before any
  bytes are allocated;
- the MIME type must be on the reviewed allowlist (the non-inline path
  additionally gates exactly one ``application/pdf`` at mode selection, Scope
  §2.1/§5.3);
- the body is streamed through :meth:`ObjectStorage.stream_object` into a
  private temporary file (``0600``) while the SHA-256 digest is computed
  incrementally and the byte ceiling is enforced *during* the read, so a
  head/read race (the object grew or changed after ``head_object``) still
  fails bounded instead of allocating arbitrary memory;
- the streamed byte count is cross-checked against the head metadata and the
  digest is exposed for the durable reference, so the recorded size and
  SHA-256 are by construction correct.

The temporary file exists only for the duration of the :class:`StreamedSource`
context and is removed on exit; it never reaches the broker, the database,
logs or audit metadata (BP §28, ADR-0017). Feature modules never import this
module (import-boundary test, Scope §6.1 checkbox 3).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Buffer, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

from app.ai.attachments import ALLOWED_ATTACHMENT_MIME_TYPES
from app.ai.errors import AIInputValidationError
from app.ai.storage_resolver import EXTENSION_MIME_TYPES
from app.storage.base import ObjectStorage, WritableByteStream


class _DigestBoundedWriter:
    """A write-only stream wrapper enforcing the byte ceiling mid-write.

    Every chunk written is forwarded to the underlying temporary-file handle,
    counted against ``max_bytes`` and folded into the running SHA-256, so the
    caller learns the digest incrementally and an oversized body fails with
    :class:`ValueError` at the exact chunk that crosses the ceiling — without
    ever holding the body in memory. It satisfies
    :class:`~app.storage.base.WritableByteStream` (the only operation the
    streaming adapters use is ``write``), so it can sit between the storage
    adapter and the real file handle.
    """

    def __init__(self, handle: WritableByteStream, *, max_bytes: int, digest: Any) -> None:
        self._handle = handle
        self._max_bytes = max_bytes
        self._digest = digest
        self._written = 0

    @property
    def written(self) -> int:
        """Total bytes forwarded so far (the bounded streamed count)."""
        return self._written

    def write(self, buffer: Buffer, /) -> int:
        chunk = bytes(buffer)
        self._written += len(chunk)
        if self._written > self._max_bytes:
            raise ValueError(f"object exceeds the {self._max_bytes} byte read limit")
        self._digest.update(chunk)
        self._handle.write(chunk)
        return len(chunk)


class StreamedSource:
    """A verified, bounded, on-disk copy of one private source object.

    Use as an async context manager: entering verifies ownership, heads the
    object, streams the body into a private temporary file (incrementally
    hashing and enforcing ``max_bytes``), and cross-checks the streamed count
    against the head metadata. Exiting removes the temporary file. Exposes
    ``size_bytes``, ``mime_type``, ``sha256_digest`` and ``path`` for the
    transfer orchestration layer.

    Raises :class:`~app.ai.errors.AIInputValidationError` with a safe message
    (never echoing the reference — BP §28) when the object is inaccessible,
    missing, oversized or unreadable.
    """

    def __init__(
        self,
        *,
        storage: ObjectStorage,
        reference: str,
        organisation_id: UUID,
        max_bytes: int,
        allowed_mime_types: frozenset[str] = ALLOWED_ATTACHMENT_MIME_TYPES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._storage = storage
        self._reference = reference
        self._organisation_id = organisation_id
        self._max_bytes = max_bytes
        self._allowed_mime_types = allowed_mime_types
        self._path: Path | None = None
        self._size_bytes = 0
        self._mime_type = ""
        self._sha256_digest = ""

    @property
    def path(self) -> Path:
        """The verified temporary-file path (valid only inside the context)."""
        if self._path is None:
            raise RuntimeError("StreamedSource is not active")
        return self._path

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    @property
    def mime_type(self) -> str:
        return self._mime_type

    @property
    def sha256_digest(self) -> str:
        return self._sha256_digest

    async def __aenter__(self) -> StreamedSource:
        reference = self._reference
        if not reference.startswith(f"organisations/{self._organisation_id}/"):
            raise AIInputValidationError(
                "the referenced storage object is not accessible to this organisation"
            )
        info = await self._storage.head_object(reference)
        if info is None:
            raise AIInputValidationError("the referenced storage object does not exist")
        mime_type = self._resolve_mime_type(info.content_type, reference)
        # Reject from head metadata before any bytes are read: an oversized
        # object must never be allocated into worker memory (Scope §2.3
        # bounded-memory contract, §5.8).
        if info.size_bytes > self._max_bytes:
            raise AIInputValidationError("the referenced storage object is too large")

        digest = hashlib.sha256()
        descriptor, path = tempfile.mkstemp(prefix="ai-stream-", suffix=".bin")
        self._path = Path(path)
        streamed = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                writer = _DigestBoundedWriter(handle, max_bytes=self._max_bytes, digest=digest)
                await self._storage.stream_object(reference, destination=writer)
                streamed = writer.written
        except (KeyError, ValueError, OSError) as exc:
            self._discard()
            raise AIInputValidationError("the referenced storage object could not be read") from exc
        # A head/read race (the object changed after head_object) is caught by
        # the streamed count; the digest is over exactly the streamed bytes, so
        # the recorded digest can never describe different content than the
        # size/MIME metadata the reference was verified against.
        if streamed != info.size_bytes:
            self._discard()
            raise AIInputValidationError("the referenced storage object changed while being read")
        self._size_bytes = streamed
        self._mime_type = mime_type
        self._sha256_digest = digest.hexdigest()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._discard()

    def _discard(self) -> None:
        """Close and remove the temporary file; safe to call repeatedly."""
        path = self._path
        self._path = None
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    def _resolve_mime_type(self, content_type: str | None, reference: str) -> str:
        """The allowlisted MIME type for an object, failing closed otherwise.

        Mirrors the inline resolver: a stored content type must be on the
        reviewed allowlist; when absent the object key's extension is the
        fallback; anything else is rejected before any bytes are streamed.
        """
        if content_type:
            candidate = content_type.strip().lower().split(";")[0]
            if candidate in self._allowed_mime_types:
                return candidate
            raise AIInputValidationError(
                "the referenced storage object has an unsupported content type"
            )
        fallback = EXTENSION_MIME_TYPES.get(Path(reference.rstrip("/")).suffix.lower())
        if fallback is None or fallback not in self._allowed_mime_types:
            raise AIInputValidationError(
                "the referenced storage object declares no supported content type"
            )
        return fallback


def iter_streamed_chunks(source: StreamedSource, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Yield bounded chunks from a :class:`StreamedSource`'s temporary file.

    The provider upload/staging adapters (Scope §6.4-§6.6) read the verified
    on-disk copy through this helper so a 50 MB source is streamed to the
    provider in bounded chunks rather than being loaded whole.
    """
    with source.path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            yield chunk


__all__ = ["StreamedSource", "iter_streamed_chunks"]
