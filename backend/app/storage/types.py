"""Value objects returned by the storage interface (blueprint §17, Scope §6.1).

These are the provider-neutral payloads every adapter returns: a signed URL
with its expiry and HTTP method, and the head/verify metadata of one stored
object. Application code depends on these types (never on provider SDK types)
so the provider interface stays the only seam between the app and storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SignedUrl:
    """A short-lived pre-signed URL plus its expiry and HTTP method."""

    url: str
    method: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Provider-level metadata for one stored object (the head result).

    ``checksum`` is provider dependent: S3 exposes the object ETag while the
    fake adapter returns a SHA-256 of the stored bytes. It is opaque to
    application code, which only ever compares it for equality (Scope §6.3).
    ``last_modified`` is the provider's last-modification timestamp (``None``
    when unavailable); the AI retention job uses it to age out orphaned
    analyse-only scratch objects (v0.7 Scope §6.5).
    """

    object_key: str
    size_bytes: int
    content_type: str | None
    checksum: str | None
    last_modified: datetime | None = None
