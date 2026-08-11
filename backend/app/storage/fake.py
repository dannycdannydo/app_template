"""In-memory ObjectStorage implementation for tests (blueprint §17, Scope §6.1).

The fake never touches a provider: it stores bytes in a dict, mints
deterministic signed URLs (fixed TTL, object key embedded, expiry derivable),
and tracks bucket creation. It is the adapter the pytest suite pins via
``STORAGE_PROVIDER=fake`` so ``make check`` needs no MinIO or network.

``put`` is not part of the :class:`ObjectStorage` interface — it simulates the
browser's direct PUT against the signed URL so tests can round-trip an upload.
It records the content type from the declaration made at ``create_upload_url``
time and refuses content whose size differs from the declared size, proving
the verification seam the files module relies on in Scope §6.3.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.storage.base import DEFAULT_SIGNED_URL_TTL, ObjectStorage
from app.storage.types import ObjectInfo, SignedUrl

_URL_PREFIX = "https://storage.example.invalid"
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


@dataclass
class _StoredObject:
    content: bytes
    content_type: str
    created_at: datetime


class FakeObjectStorage(ObjectStorage):
    """Deterministic, test-only :class:`ObjectStorage` implementation."""

    def __init__(
        self,
        *,
        bucket: str,
        url_ttl: timedelta = DEFAULT_SIGNED_URL_TTL,
        url_prefix: str = _URL_PREFIX,
    ) -> None:
        if not bucket:
            raise ValueError("FakeObjectStorage requires a bucket name")
        self._bucket = bucket
        self._url_ttl = url_ttl
        self._url_prefix = url_prefix.rstrip("/")
        # object_key -> (content_type, size_bytes) declared at upload-intent time.
        self._declared: dict[str, tuple[str, int]] = {}
        self._objects: dict[str, _StoredObject] = {}
        self._bucket_created = False

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def bucket_created(self) -> bool:
        return self._bucket_created

    async def ensure_bucket(self) -> None:
        self._bucket_created = True

    async def create_upload_url(
        self,
        *,
        file_id: uuid.UUID,
        object_key: str,
        content_type: str,
        size_bytes: int,
        expires_in: timedelta | None = None,
    ) -> SignedUrl:
        # file_id is part of the interface contract for audit/tracing in the
        # real adapters; the fake only needs the declaration to drive ``put``.
        # ``None`` falls back to the fake's configured TTL so a caller that
        # does not override the lifetime gets the adapter's default.
        expires_in = expires_in if expires_in is not None else self._url_ttl
        self._declared[object_key] = (content_type, size_bytes)
        expires_at = datetime.now(UTC) + expires_in
        return SignedUrl(
            url=f"{self._url_prefix}/upload/{object_key}?expires={expires_at.timestamp():.0f}",
            method="PUT",
            expires_at=expires_at,
        )

    async def create_download_url(
        self,
        *,
        object_key: str,
        expires_in: timedelta | None = None,
    ) -> SignedUrl:
        expires_in = expires_in if expires_in is not None else self._url_ttl
        expires_at = datetime.now(UTC) + expires_in
        return SignedUrl(
            url=f"{self._url_prefix}/download/{object_key}?expires={expires_at.timestamp():.0f}",
            method="GET",
            expires_at=expires_at,
        )

    async def put(self, object_key: str, content: bytes, content_type: str | None = None) -> None:
        """Simulate the browser's PUT to the signed upload URL (test helper)."""
        declared = self._declared.get(object_key)
        if declared is not None and len(content) != declared[1]:
            raise ValueError(
                f"uploaded size {len(content)} does not match declared size {declared[1]} "
                f"for object {object_key!r}"
            )
        actual_content_type = content_type or (declared[0] if declared else _DEFAULT_CONTENT_TYPE)
        self._objects[object_key] = _StoredObject(
            content=content,
            content_type=actual_content_type,
            created_at=datetime.now(UTC),
        )

    async def head_object(self, object_key: str) -> ObjectInfo | None:
        stored = self._objects.get(object_key)
        if stored is None:
            return None
        return ObjectInfo(
            object_key=object_key,
            size_bytes=len(stored.content),
            content_type=stored.content_type,
            checksum=hashlib.sha256(stored.content).hexdigest(),
        )

    async def read_object(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        stored = self._objects.get(object_key)
        if stored is None:
            raise KeyError(f"object not found: {object_key}")
        if max_bytes is not None and len(stored.content) > max_bytes:
            raise ValueError(f"object exceeds the {max_bytes} byte read limit")
        return stored.content

    async def delete_object(self, object_key: str) -> None:
        self._objects.pop(object_key, None)
        self._declared.pop(object_key, None)

    async def list_objects(
        self,
        prefix: str,
        *,
        limit: int = 1000,
        start_after: str | None = None,
    ) -> list[ObjectInfo]:
        """Return metadata for stored objects whose key starts with ``prefix``.

        Keys sort lexicographically and the result is bounded by ``limit``,
        mirroring the S3 adapter's listing contract (v0.7 Scope §6.5 retention
        sweep). ``start_after`` is the exclusive marker of the last key the
        caller already processed, so the sweep pages over a namespace of any
        size without re-reading fresh objects. ``last_modified`` is the fake's
        ``created_at`` so the sweep's age-based deletion works identically
        against the fake and the real adapter.
        """
        matches = sorted(key for key in self._objects if key.startswith(prefix))
        if start_after is not None:
            matches = [key for key in matches if key > start_after]
        result: list[ObjectInfo] = []
        for key in matches[:limit]:
            stored = self._objects[key]
            result.append(
                ObjectInfo(
                    object_key=key,
                    size_bytes=len(stored.content),
                    content_type=stored.content_type,
                    checksum=hashlib.sha256(stored.content).hexdigest(),
                    last_modified=stored.created_at,
                )
            )
        return result
