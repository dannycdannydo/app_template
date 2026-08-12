"""Just-in-time managed download-URL minting for retained sources (v0.8 Scope §2.3, §6.3).

A managed signed URL is a **temporary bearer capability minted anew for each
dispatch/retry** — never a reusable durable reference (Scope §2.3): it is never
returned to the caller, persisted, audited or logged, and its query string is
redacted from every log/error/telemetry boundary (BP §28).

The minter enforces the reviewed contract (Scope §2.2/§2.3) before any URL is
minted:

- the source must be a *retained* private S3-compatible object (managed URLs
  exist only for the ``managed_signed_url`` mode);
- the organisation ownership boundary is re-checked from the durable
  reference's own scope — a cross-organisation object is denied, never minted;
- the object is re-headed at dispatch time and must still exist with the
  exact size and MIME type the durable reference recorded, then re-streamed
  through the bounded streaming seam so the incremental SHA-256 must equal the
  digest the transfer was verified against (Scope §2.3: "exact immutable
  object identity, size, MIME and digest validation"). Size and MIME alone
  cannot prove identity — an object replaced with different bytes of the same
  length and content type must never mint a URL for content the transfer never
  verified;
- the TTL is bounded to the reviewed window (default 900 s, maximum 1,800 s)
  and the resulting URL must be HTTPS and read-only (GET), or the mint fails
  closed with a safe error that never echoes the URL.

Nothing in this module can persist a URL: it returns the
:class:`~app.storage.types.SignedUrl` value object and the caller is
responsible for keeping it out of rows, broker messages, logs and audit
metadata; :func:`redact_signed_url` is the single helper every logging and
error boundary uses.
"""

from __future__ import annotations

import hashlib
from collections.abc import Buffer
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.ai.errors import TransferSourceError
from app.ai.staging import ExternalFileReference
from app.ai.transfer import (
    MANAGED_URL_DEFAULT_TTL_SECONDS,
    MANAGED_URL_MAX_TTL_SECONDS,
    SourceLifecycle,
    TransferMode,
)
from app.storage.base import ObjectStorage
from app.storage.types import SignedUrl


def redact_signed_url(url: str) -> str:
    """Return a signed URL with its query string and fragment removed.

    The query string of a signed URL is the bearer material (the signature and
    expiry); every log, error message, audit and telemetry boundary must carry
    the redacted form only (BP §28, Scope §2.3). The scheme/authority/path are
    safe identifiers.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class _DigestSink:
    """A write-only sink that folds streamed bytes into a running SHA-256.

    The minter re-streams the source bounded by the durable size so an object
    that grew after verification fails mid-stream without buffering the body,
    exactly like the streaming seam (Scope §2.3 bounded-memory contract). It
    satisfies :class:`~app.storage.base.WritableByteStream` (the only operation
    the streaming adapters use is ``write``).
    """

    def __init__(self, *, max_bytes: int, digest: Any) -> None:
        self._max_bytes = max_bytes
        self._digest = digest
        self._written = 0

    @property
    def written(self) -> int:
        """Total bytes forwarded so far (the bounded re-streamed count)."""
        return self._written

    def write(self, buffer: Buffer, /) -> int:
        chunk = bytes(buffer)
        self._written += len(chunk)
        if self._written > self._max_bytes:
            raise ValueError("object exceeds the byte limit")
        self._digest.update(chunk)
        return len(chunk)


async def mint_managed_download_url(
    *,
    storage: ObjectStorage,
    reference: ExternalFileReference,
    ttl_seconds: int | None = None,
) -> SignedUrl:
    """Mint one short-lived, read-only HTTPS download URL for a retained source.

    Verifies the mode/lifecycle contract, the organisation ownership boundary
    and the exact immutable object identity — a fresh head for size and MIME,
    plus a bounded re-stream whose incremental SHA-256 must equal the durable
    digest — before delegating to :meth:`ObjectStorage.create_download_url`
    with the bounded TTL. Raises :class:`TransferSourceError` (permanent, safe
    message) on any violation; the URL itself is never embedded in the message.
    The returned value object is for one dispatch only and must never be
    persisted or logged (use :func:`redact_signed_url` at every boundary).
    """
    if reference.mode is not TransferMode.MANAGED_SIGNED_URL:
        raise TransferSourceError(
            "a managed download URL can only be minted for the managed-signed-url mode"
        )
    if reference.source_lifecycle is not SourceLifecycle.RETAINED:
        raise TransferSourceError(
            "a managed download URL can only be minted for a retained private source"
        )
    ttl = MANAGED_URL_DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    if not MANAGED_URL_DEFAULT_TTL_SECONDS <= ttl <= MANAGED_URL_MAX_TTL_SECONDS:
        raise TransferSourceError(
            f"managed URL TTL must be between {MANAGED_URL_DEFAULT_TTL_SECONDS} and "
            f"{MANAGED_URL_MAX_TTL_SECONDS} seconds"
        )
    source = reference.source_reference
    if not source.startswith(f"organisations/{reference.organisation_id}/"):
        raise TransferSourceError(
            "the referenced storage object is not accessible to this organisation"
        )
    info = await storage.head_object(source)
    if info is None:
        raise TransferSourceError("the referenced storage object does not exist")
    stored_mime = _normalise_mime(info.content_type)
    if info.size_bytes != reference.size_bytes or stored_mime != reference.mime_type:
        raise TransferSourceError("the referenced storage object changed since it was verified")
    # Exact immutable identity: size and MIME cannot prove the object is the
    # one the transfer verified — content replaced with different bytes of the
    # same length and content type would pass the head check. Re-stream the
    # object bounded (never buffered) and require the incremental SHA-256 to
    # equal the durable digest (Scope §2.3 exact identity/digest validation).
    digest = hashlib.sha256()
    try:
        sink = _DigestSink(max_bytes=reference.size_bytes, digest=digest)
        await storage.stream_object(source, destination=sink)
    except (KeyError, ValueError):
        raise TransferSourceError("the referenced storage object could not be verified") from None
    if sink.written != info.size_bytes:
        raise TransferSourceError("the referenced storage object changed since it was verified")
    if digest.hexdigest() != reference.source_digest:
        raise TransferSourceError("the referenced storage object changed since it was verified")
    signed = await storage.create_download_url(
        object_key=source,
        expires_in=timedelta(seconds=ttl),
    )
    if signed.method != "GET":
        raise TransferSourceError("the storage seam returned a non-read-only download URL")
    if not signed.url.startswith("https://"):
        raise TransferSourceError("the storage seam returned a non-HTTPS download URL")
    return signed


def _normalise_mime(content_type: str | None) -> str:
    """Normalise a stored content type to its bare lowercase form, or ``""``."""
    if not content_type:
        return ""
    return content_type.strip().lower().split(";")[0]


__all__ = ["mint_managed_download_url", "redact_signed_url"]
