"""MinIO-backed S3 adapter integration tests (Scope §6.2, blueprint §17).

These prove the acceptance-criteria items a mock cannot: a signed upload round
trip through a real S3-compatible server, private-bucket denial of unsigned
requests (unsigned GET -> 403) and lazy bucket creation. They carry the
``storage_integration`` marker and are excluded from the default suite by the
pytest addopts in ``pyproject.toml``; run them against the MinIO started by
``make dev`` (or a CI service) with:

    uv run pytest -m storage_integration

Every test skips when the configured endpoint is unreachable, so a developer
without MinIO running sees skips, not failures.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, cast

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError

from app.storage import S3Storage
from app.storage.types import ObjectInfo, SignedUrl

pytestmark = pytest.mark.storage_integration

_ENDPOINT = os.environ.get("STORAGE_ENDPOINT_URL", "http://localhost:9000")
_BUCKET = os.environ.get("STORAGE_BUCKET", "test-bucket")
_REGION = os.environ.get("STORAGE_REGION", "us-east-1")
_ACCESS_KEY = os.environ.get("STORAGE_ACCESS_KEY_ID", "minioadmin")
_SECRET_KEY = os.environ.get("STORAGE_SECRET_ACCESS_KEY", "minioadmin")


def _storage(*, bucket: str = _BUCKET) -> S3Storage:
    return S3Storage(
        bucket=bucket,
        endpoint_url=_ENDPOINT,
        region=_REGION,
        access_key_id=_ACCESS_KEY,
        secret_access_key=_SECRET_KEY,
    )


@pytest.fixture(scope="module")
def storage() -> S3Storage:
    """Probe connectivity once; skip the whole module when MinIO is down."""
    candidate = _storage()
    try:
        asyncio.run(candidate.ensure_bucket())
    except Exception as exc:
        pytest.skip(f"S3-compatible storage not reachable at {_ENDPOINT}: {exc}")
    return candidate


async def _upload(storage: S3Storage, key: str, content: bytes, content_type: str) -> None:
    """Sign an upload URL and PUT the bytes directly, as the browser would."""
    upload = await storage.create_upload_url(
        file_id=uuid.uuid4(),
        object_key=key,
        content_type=content_type,
        size_bytes=len(content),
    )
    assert isinstance(upload, SignedUrl)
    assert upload.method == "PUT"
    async with httpx.AsyncClient() as client:
        response = await client.put(
            upload.url,
            content=content,
            headers={"Content-Type": content_type},
        )
    assert response.status_code in (200, 201, 204)


async def test_signed_upload_round_trip(storage: S3Storage) -> None:
    """intent URL -> direct PUT -> head -> signed GET -> delete, end to end."""
    content = b"%PDF-1.7 integration round trip " + uuid.uuid4().hex.encode()
    key = f"organisations/org-integration/documents/{uuid.uuid4()}/original.pdf"
    await _upload(storage, key, content, "application/pdf")

    info = await storage.head_object(key)
    assert isinstance(info, ObjectInfo)
    assert info.size_bytes == len(content)
    assert info.content_type == "application/pdf"
    assert isinstance(info.checksum, str) and info.checksum

    download = await storage.create_download_url(object_key=key)
    assert download.method == "GET"
    async with httpx.AsyncClient() as client:
        response = await client.get(download.url)
    assert response.status_code == 200
    assert response.content == content

    await storage.delete_object(key)
    assert await storage.head_object(key) is None


async def test_private_bucket_denies_unsigned_requests(storage: S3Storage) -> None:
    """Acceptance criterion 2: the bucket is private, unsigned GET -> 403."""
    content = b"private object"
    key = f"organisations/org-integration/private/{uuid.uuid4()}/secret.txt"
    await _upload(storage, key, content, "text/plain")

    unsigned_url = f"{_ENDPOINT.rstrip('/')}/{_BUCKET}/{key}"
    async with httpx.AsyncClient() as client:
        response = await client.get(unsigned_url)
    assert response.status_code == 403
    async with httpx.AsyncClient() as client:
        response = await client.put(unsigned_url, content=b"unsigned put")
    assert response.status_code == 403

    await storage.delete_object(key)


async def test_ensure_bucket_creates_missing_bucket_lazily(storage: S3Storage) -> None:
    """Acceptance criterion 2: the adapter creates its bucket on first use."""
    bucket = f"lazy-bucket-{uuid.uuid4().hex[:8]}"
    candidate = _storage(bucket=bucket)
    probe = cast(
        Any,
        boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=_ENDPOINT,
            region_name=_REGION,
            aws_access_key_id=_ACCESS_KEY,
            aws_secret_access_key=_SECRET_KEY,
        ),
    )
    with pytest.raises(ClientError):
        probe.head_bucket(Bucket=bucket)
    await candidate.ensure_bucket()
    probe.head_bucket(Bucket=bucket)  # exists now; raises on failure


async def test_head_missing_object_returns_none(storage: S3Storage) -> None:
    missing = f"organisations/org-integration/missing/{uuid.uuid4()}/none.pdf"
    assert await storage.head_object(missing) is None


async def test_delete_missing_object_is_idempotent(storage: S3Storage) -> None:
    never_uploaded = f"organisations/org-integration/never-uploaded/{uuid.uuid4()}/none.txt"
    await storage.delete_object(never_uploaded)
    await storage.delete_object(never_uploaded)
