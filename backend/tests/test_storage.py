"""Interface contract tests for the storage package (Scope §6.1, blueprint §17).

The suite runs against :class:`FakeObjectStorage` — the default adapter under
``STORAGE_PROVIDER=fake`` (pinned in ``tests/conftest.py``) — and proves the
whole provider contract: signed-URL round trip, deterministic expiry, head
metadata, delete semantics and bucket creation. No provider SDK is imported
anywhere, which is the point of ADR-0006.
"""

from __future__ import annotations

import hashlib
import types
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.storage import FakeObjectStorage, ObjectStorage
from app.storage.base import DEFAULT_SIGNED_URL_TTL
from app.storage.factory import get_storage
from app.storage.types import ObjectInfo, SignedUrl

_OBJECT_KEY = "organisations/org-1/documents/file-1/original.pdf"
_FILE_ID = uuid.uuid4()


@pytest.fixture
def storage() -> FakeObjectStorage:
    return FakeObjectStorage(bucket="test-bucket")


async def test_fake_is_an_object_storage_implementation(storage: FakeObjectStorage) -> None:
    assert isinstance(storage, ObjectStorage)


async def test_round_trip_upload_head_download_delete(storage: FakeObjectStorage) -> None:
    """The full provider contract: intent URL → put → head → download → delete."""
    content = b"%PDF-1.7 fake bytes for the storage contract"
    upload = await storage.create_upload_url(
        file_id=_FILE_ID,
        object_key=_OBJECT_KEY,
        content_type="application/pdf",
        size_bytes=len(content),
    )
    assert isinstance(upload, SignedUrl)

    await storage.put(_OBJECT_KEY, content)

    info = await storage.head_object(_OBJECT_KEY)
    assert isinstance(info, ObjectInfo)
    assert info.object_key == _OBJECT_KEY
    assert info.size_bytes == len(content)
    assert info.content_type == "application/pdf"
    assert info.checksum == hashlib.sha256(content).hexdigest()

    download = await storage.create_download_url(object_key=_OBJECT_KEY)
    assert download.method == "GET"
    assert _OBJECT_KEY in download.url

    await storage.delete_object(_OBJECT_KEY)
    assert await storage.head_object(_OBJECT_KEY) is None


async def test_upload_url_is_a_signed_put_with_the_object_key(
    storage: FakeObjectStorage,
) -> None:
    upload = await storage.create_upload_url(
        file_id=_FILE_ID,
        object_key=_OBJECT_KEY,
        content_type="application/pdf",
        size_bytes=1024,
    )
    assert upload.method == "PUT"
    assert _OBJECT_KEY in upload.url
    assert "expires=" in upload.url


async def test_signed_urls_expire_after_the_fixed_ttl(storage: FakeObjectStorage) -> None:
    """Deterministic expiry: expires_at is now + the configured (default) TTL."""
    before = datetime.now(UTC)
    upload = await storage.create_upload_url(
        file_id=_FILE_ID,
        object_key=_OBJECT_KEY,
        content_type="application/pdf",
        size_bytes=1024,
    )
    assert upload.expires_at >= before + DEFAULT_SIGNED_URL_TTL
    remaining = upload.expires_at - datetime.now(UTC)
    assert remaining <= DEFAULT_SIGNED_URL_TTL
    assert remaining > timedelta(minutes=14, seconds=55)


async def test_signed_url_ttl_can_be_overridden(storage: FakeObjectStorage) -> None:
    custom = FakeObjectStorage(bucket="test-bucket", url_ttl=timedelta(hours=1))
    upload = await custom.create_upload_url(
        file_id=_FILE_ID,
        object_key=_OBJECT_KEY,
        content_type="application/pdf",
        size_bytes=1024,
    )
    remaining = upload.expires_at - datetime.now(UTC)
    assert remaining <= timedelta(hours=1)
    assert remaining > timedelta(minutes=59, seconds=55)


async def test_head_object_missing_returns_none(storage: FakeObjectStorage) -> None:
    assert await storage.head_object("organisations/org-1/documents/missing/original.pdf") is None


async def test_read_object_returns_stored_bytes(storage: FakeObjectStorage) -> None:
    """The server-side read seam the AI layer resolves references through
    (v0.7 Scope §6.4): bytes round-trip and never leave memory."""
    content = b"%PDF-1.7 analysis fixture"
    await storage.put(_OBJECT_KEY, content, content_type="application/pdf")
    assert await storage.read_object(_OBJECT_KEY) == content


async def test_read_object_missing_raises_key_error(storage: FakeObjectStorage) -> None:
    """A missing object is a KeyError so the AI resolver can translate it into
    its safe error without echoing the reference (v0.7 Scope §6.4)."""
    with pytest.raises(KeyError):
        await storage.read_object("organisations/org-1/documents/missing/original.pdf")


async def test_read_object_is_bounded_by_max_bytes(storage: FakeObjectStorage) -> None:
    """The bounded read cap (v0.7 Scope §6.4): requesting fewer bytes than the
    stored object fails with ValueError instead of returning the full body, so
    a head/read race can never allocate unbounded worker memory."""
    await storage.put(_OBJECT_KEY, b"x" * 32, content_type="application/pdf")
    assert await storage.read_object(_OBJECT_KEY, max_bytes=32) == b"x" * 32
    with pytest.raises(ValueError, match="read limit"):
        await storage.read_object(_OBJECT_KEY, max_bytes=31)


async def test_delete_object_is_idempotent(storage: FakeObjectStorage) -> None:
    await storage.put(_OBJECT_KEY, b"content")
    await storage.delete_object(_OBJECT_KEY)
    await storage.delete_object(_OBJECT_KEY)  # deleting a missing object is a no-op
    assert await storage.head_object(_OBJECT_KEY) is None


async def test_ensure_bucket_is_idempotent_and_tracked(storage: FakeObjectStorage) -> None:
    assert storage.bucket_created is False
    await storage.ensure_bucket()
    assert storage.bucket_created is True
    await storage.ensure_bucket()
    assert storage.bucket_created is True


async def test_put_rejects_content_that_mismatches_the_declared_size(
    storage: FakeObjectStorage,
) -> None:
    """The verification seam Scope §6.3 relies on: declared size is enforced."""
    await storage.create_upload_url(
        file_id=_FILE_ID,
        object_key=_OBJECT_KEY,
        content_type="application/pdf",
        size_bytes=100,
    )
    with pytest.raises(ValueError, match="does not match declared size"):
        await storage.put(_OBJECT_KEY, b"only 11 bytes")


async def test_constructor_requires_a_bucket() -> None:
    with pytest.raises(ValueError, match="bucket"):
        FakeObjectStorage(bucket="")


# --- Factory (get_storage, wired from settings) ---


def _settings_with_provider(provider: str) -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+asyncpg://x",
        storage_provider=provider,
        storage_bucket="test-bucket",
    )


def test_get_storage_returns_a_cached_fake_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    get_storage.cache_clear()
    monkeypatch.setattr("app.storage.factory.get_settings", lambda: _settings_with_provider("fake"))
    storage = get_storage()
    assert isinstance(storage, FakeObjectStorage)
    assert storage.bucket == "test-bucket"
    assert get_storage() is storage  # lru_cache singleton, like get_settings
    get_storage.cache_clear()


def test_get_storage_rejects_unknown_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory stays defensive even though Settings rejects unknown providers."""
    get_storage.cache_clear()
    monkeypatch.setattr(
        "app.storage.factory.get_settings",
        lambda: types.SimpleNamespace(storage_provider="gcs", storage_bucket="x"),
    )
    with pytest.raises(ValueError, match="unknown storage_provider"):
        get_storage()
    get_storage.cache_clear()
