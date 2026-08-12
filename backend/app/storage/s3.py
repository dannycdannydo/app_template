"""S3-compatible object storage adapter (blueprint §17, ADR-0006, Scope §6.2).

``S3Storage`` is the first real implementation of the :class:`ObjectStorage`
interface: it talks to any S3-compatible service (MinIO locally, AWS S3 or any
other S3-compatible provider elsewhere) through boto3. The boto3/botocore SDK
is imported only in this module — no other module outside ``app/storage/`` may
import it (ADR-0006). Every blocking SDK call runs in a worker thread via
``asyncio.to_thread`` so the adapter satisfies the asyncio interface without
tying up the event loop.

Two boto3 clients are kept. The data-plane client targets
``storage_endpoint_url`` for the API's own head/delete/bucket operations; the
pre-signing client targets ``storage_public_endpoint_url``, the host the
browser actually reaches (in the dev-docker stack the API talks to
``http://minio:9000`` while the browser must PUT to ``http://localhost:9000``).
A URL pre-signed against the host the browser will use verifies, because the
signature covers that host. When the public endpoint equals the data endpoint
the two clients are the same object.

The configured bucket is created lazily on first use (idempotent, thread-safe)
so `make dev` works without a provisioning step; buckets stay private — the
adapter only ever hands out short-lived pre-signed URLs, never public reads.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.storage.base import DEFAULT_SIGNED_URL_TTL, ObjectStorage, WritableByteStream
from app.storage.types import ObjectInfo, SignedUrl

# S3 responses for "the object/bucket does not exist", reported with varying
# status codes across S3-compatible services. Treated as a missing object
# (``None`` for head) or a no-op (idempotent delete).
_MISSING_OBJECT_CODES = {"404", "NoSuchKey", "NotFound"}
# Codes S3-compatible services return when the bucket already exists, treated
# as a successful (no-op) bucket creation.
_BUCKET_ALREADY_EXISTS_CODES = {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}
#: The bounded chunk size for the v0.8 streaming seam (Scope §2.3/§6.3): the
#: body is pulled from S3 one chunk at a time and written straight to the
#: destination, so a large object never accumulates in Python memory and the
#: byte ceiling can be enforced mid-stream.
_STREAM_CHUNK_BYTES = 1024 * 1024


class S3Storage(ObjectStorage):
    """S3-compatible :class:`ObjectStorage` implementation over boto3."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        public_endpoint_url: str | None = None,
        url_ttl: timedelta = DEFAULT_SIGNED_URL_TTL,
    ) -> None:
        if not bucket:
            raise ValueError("S3Storage requires a bucket name")
        if not endpoint_url:
            raise ValueError("S3Storage requires an endpoint URL")
        if not endpoint_url.startswith(("http://", "https://")):
            raise ValueError("S3Storage endpoint URL must start with http(s)://")
        if not url_ttl or url_ttl <= timedelta(0):
            raise ValueError("S3Storage url_ttl must be positive")

        self._bucket = bucket
        self._region = region
        self._url_ttl = url_ttl
        self._bucket_ensured = False
        self._ensure_lock = threading.Lock()

        client_kwargs: dict[str, Any] = {
            "service_name": "s3",
            "endpoint_url": endpoint_url,
            "config": Config(
                connect_timeout=5,
                read_timeout=5,
                retries={"max_attempts": 2},
            ),
        }
        if region:
            client_kwargs["region_name"] = region
        if access_key_id:
            client_kwargs["aws_access_key_id"] = access_key_id
            client_kwargs["aws_secret_access_key"] = secret_access_key
        self._client: Any = cast(Any, boto3.client(**client_kwargs))  # pyright: ignore[reportUnknownMemberType]

        # Pre-sign against the host the browser will use when it differs from
        # the API's own storage host (e.g. dev-docker: minio:9000 vs
        # localhost:9000). The signature covers the host, so the URL only
        # verifies when the browser targets exactly this endpoint.
        if public_endpoint_url and public_endpoint_url != endpoint_url:
            pre_sign_kwargs = dict(client_kwargs)
            pre_sign_kwargs["endpoint_url"] = public_endpoint_url
            self._presign_client: Any = cast(
                Any,
                boto3.client(**pre_sign_kwargs),  # pyright: ignore[reportUnknownMemberType]
            )
        else:
            self._presign_client = self._client

        # S3 requires a LocationConstraint for bucket creation in any region
        # other than us-east-1; S3-compatible services accept the parameter
        # only for regions they know, and our development default is
        # us-east-1, so the constraint is sent solely when needed.
        self._create_bucket_kwargs: dict[str, Any] = {}
        if region and region != "us-east-1":
            self._create_bucket_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}

    @property
    def bucket(self) -> str:
        return self._bucket

    async def create_upload_url(
        self,
        *,
        file_id: uuid.UUID,
        object_key: str,
        content_type: str,
        size_bytes: int,
        expires_in: timedelta | None = None,
    ) -> SignedUrl:
        # file_id ties the signed URL to the file record for audit/tracing in
        # provider-side logs; boto3 itself has no place for it, so it is not
        # passed to the SDK. The declared content type is part of the
        # signature: the browser must PUT with exactly this Content-Type, so a
        # mismatched MIME type fails the request before it reaches storage.
        # size_bytes is intentionally dropped too: a presigned single PUT
        # cannot cap Content-Length (that needs S3 POST policies, out of
        # scope), so the size is validated at intent time (Scope §6.3) and
        # re-verified via head_object on complete.
        await self.ensure_bucket()
        expires_in = expires_in if expires_in is not None else self._url_ttl
        url = await asyncio.to_thread(
            self._presign_client.generate_presigned_url,
            ClientMethod="put_object",
            Params={
                "Bucket": self._bucket,
                "Key": object_key,
                "ContentType": content_type,
            },
            ExpiresIn=int(expires_in.total_seconds()),
        )
        return SignedUrl(
            url=str(url),
            method="PUT",
            expires_at=datetime.now(UTC) + expires_in,
        )

    async def create_download_url(
        self,
        *,
        object_key: str,
        expires_in: timedelta | None = None,
    ) -> SignedUrl:
        expires_in = expires_in if expires_in is not None else self._url_ttl
        url = await asyncio.to_thread(
            self._presign_client.generate_presigned_url,
            ClientMethod="get_object",
            Params={"Bucket": self._bucket, "Key": object_key},
            ExpiresIn=int(expires_in.total_seconds()),
        )
        return SignedUrl(
            url=str(url),
            method="GET",
            expires_at=datetime.now(UTC) + expires_in,
        )

    async def head_object(self, object_key: str) -> ObjectInfo | None:
        try:
            response = await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket,
                Key=object_key,
            )
        except ClientError as exc:
            if _error_code(exc) in _MISSING_OBJECT_CODES:
                return None
            raise
        # HeadObject payload is provider typed (botocore dict); every field is
        # read defensively. ``checksum`` is the object ETag (quotes stripped),
        # which S3 exposes as the single-PUT object hash — it is opaque to
        # application code, which only compares it for equality (Scope §6.3).
        etag = response.get("ETag")
        checksum = etag.strip('"') if isinstance(etag, str) else None
        last_modified = response.get("LastModified")
        return ObjectInfo(
            object_key=object_key,
            size_bytes=int(response.get("ContentLength") or 0),
            content_type=response.get("ContentType"),
            checksum=checksum,
            last_modified=last_modified,
        )

    async def read_object(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        """Read one object's bytes server-side, bounded when asked.

        The AI layer resolves private storage references into bounded
        in-memory attachments through this seam (v0.7 Scope §6.4, ADR-0017):
        bytes are fetched into memory for one provider call and never persist
        anywhere. Missing objects raise :class:`KeyError` so the AI resolver
        can translate them into its safe error without leaking the reference.

        When ``max_bytes`` is given, the read is capped *during* the body
        read: ``StreamingBody.read`` fetches at most ``max_bytes + 1`` bytes
        and a longer body raises :class:`ValueError`, so an object that grew
        or changed after ``head_object`` (a head/read race) can never pull
        unbounded bytes into worker memory (Scope §6.4/§5.8). The streaming
        body is always closed, including on bounded-read failures, so repeated
        AI reads never leak HTTP connections.
        """
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=object_key,
            )
        except ClientError as exc:
            if _error_code(exc) in _MISSING_OBJECT_CODES:
                raise KeyError(f"object not found: {object_key}") from exc
            raise
        body = response.get("Body")
        if body is None:
            raise ValueError(f"provider returned no body for object: {object_key}")
        try:
            if max_bytes is not None:
                data = await asyncio.to_thread(body.read, max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(
                        f"object exceeds the {max_bytes} byte read limit: {object_key}"
                    )
            else:
                data = await asyncio.to_thread(body.read)
        finally:
            await asyncio.to_thread(body.close)
        return bytes(data)

    async def stream_object(
        self,
        object_key: str,
        *,
        destination: WritableByteStream,
        max_bytes: int | None = None,
    ) -> None:
        """Stream one object's body into the destination, chunk by chunk.

        v0.8 Scope §2.3/§6.3: the non-inline transfer path streams a verified
        private source into a secure temporary file through this seam. The
        body is pulled in bounded chunks (``_STREAM_CHUNK_BYTES``) and written
        straight to the destination, so a 50 MB source is never accumulated in
        Python memory. When ``max_bytes`` is given, the count is enforced
        mid-stream: the read stops (raising :class:`ValueError`) as soon as
        the ceiling would be exceeded, without pulling the remainder of the
        body — the same bounded contract as :meth:`read_object`, so a
        head/read race still fails bounded (Scope §2.3, §5.8). Missing objects
        raise :class:`KeyError`; the streaming body is always closed, including
        on bounded-read failures, so repeated AI transfers never leak HTTP
        connections.
        """
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=object_key,
            )
        except ClientError as exc:
            if _error_code(exc) in _MISSING_OBJECT_CODES:
                raise KeyError(f"object not found: {object_key}") from exc
            raise
        body = response.get("Body")
        if body is None:
            raise ValueError(f"provider returned no body for object: {object_key}")
        try:
            remaining = max_bytes
            while True:
                # Read at most the remaining allowance: the adapter never pulls
                # a full chunk past the ``max_bytes`` ceiling. When the ceiling
                # is reached exactly, one probe byte decides whether the body
                # ends at the limit or must fail bounded (the probe is the only
                # byte ever read beyond ``max_bytes``, and nothing of the
                # remainder is buffered).
                read_size = (
                    _STREAM_CHUNK_BYTES
                    if remaining is None
                    else min(_STREAM_CHUNK_BYTES, remaining)
                )
                chunk = await asyncio.to_thread(body.read, read_size)
                if not chunk:
                    break
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining == 0 and await asyncio.to_thread(body.read, 1):
                        raise ValueError(
                            f"object exceeds the {max_bytes} byte read limit: {object_key}"
                        )
                destination.write(chunk)
        finally:
            await asyncio.to_thread(body.close)

    async def delete_object(self, object_key: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self._bucket,
                Key=object_key,
            )
        except ClientError as exc:
            # Deleting a missing object is a no-op; S3-compatible services may
            # report it as an error instead of returning the usual 204.
            if _error_code(exc) not in _MISSING_OBJECT_CODES:
                raise

    async def list_objects(
        self,
        prefix: str,
        *,
        limit: int = 1000,
        start_after: str | None = None,
    ) -> list[ObjectInfo]:
        """Return up to ``limit`` objects under a prefix (v0.7 Scope §6.5).

        Used by the AI retention job to sweep orphaned analyse-only scratch
        objects: each result carries the provider's ``LastModified`` so the
        sweep can age them out. The listing is bounded by ``MaxKeys`` and
        pages with ``StartAfter`` (the exclusive last-key marker), so the
        sweep can cover a namespace of any size without re-reading fresh
        objects (the fake adapter sorts lexicographically, mirroring S3's
        listing order).
        """
        parameters: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": prefix,
            "MaxKeys": limit,
        }
        # Botocore rejects ``None`` for StartAfter. Omit the optional parameter
        # entirely on the first page and add it only for continuation pages.
        if start_after is not None:
            parameters["StartAfter"] = start_after
        response = await asyncio.to_thread(self._client.list_objects_v2, **parameters)
        # list_objects_v2 payloads are provider typed (botocore dict); every
        # field is read defensively like head_object, and the Contents list is
        # explicitly boxed so a missing key is an empty listing.
        raw_contents = response.get("Contents")
        contents = (
            cast(list[dict[str, Any]], raw_contents) if isinstance(raw_contents, list) else []
        )
        result: list[ObjectInfo] = []
        for item in contents:
            key = item.get("Key")
            if not isinstance(key, str):
                continue
            etag = item.get("ETag")
            checksum = etag.strip('"') if isinstance(etag, str) else None
            size_raw = item.get("Size")
            result.append(
                ObjectInfo(
                    object_key=key,
                    size_bytes=int(size_raw or 0),
                    content_type=None,  # the listing does not carry content types
                    checksum=checksum,
                    last_modified=item.get("LastModified"),
                )
            )
        return result

    async def ensure_bucket(self) -> None:
        if self._bucket_ensured:
            return
        await asyncio.to_thread(self._ensure_bucket_once)

    def _ensure_bucket_once(self) -> None:
        """Create the bucket on first use, idempotently and thread-safely.

        Runs inside a worker thread (see :meth:`ensure_bucket`); the lock is
        only ever contended there, so it cannot stall the event loop.
        """
        with self._ensure_lock:
            if self._bucket_ensured:
                return
            try:
                self._client.create_bucket(
                    Bucket=self._bucket,
                    **self._create_bucket_kwargs,
                )
            except ClientError as exc:
                if _error_code(exc) not in _BUCKET_ALREADY_EXISTS_CODES:
                    raise
            self._bucket_ensured = True


def _error_code(exc: ClientError) -> str:
    """Return the S3 Error code of a :class:`ClientError`."""
    response = cast(dict[str, Any], exc.response)
    return str(response.get("Error", {}).get("Code", ""))
