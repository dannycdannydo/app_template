"""Bounded streaming and managed-URL minting tests (v0.8 Scope §2.3, §6.3).

Unit tests for the two seams §6.3 adds behind :class:`ObjectStorage`: the
bounded streaming of a private source into a secure temporary file
(:class:`~app.ai.streamed_source.StreamedSource`, with ownership, size, MIME
and SHA-256 verification without accumulating the object in memory) and the
just-in-time managed download-URL minting for retained private sources
(:func:`~app.ai.managed_url.mint_managed_download_url`, with identity
re-validation, bounded TTL and query-string redaction). The database-backed
durable-reference lifecycle and the orchestrator live in
``test_ai_reference_db.py``.

These tests are hermetic: they run against the in-memory fake storage and
never touch a provider or PostgreSQL.
"""

from __future__ import annotations

import hashlib
import io
import stat
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.ai.errors import AIInputValidationError, TransferSourceError
from app.ai.managed_url import mint_managed_download_url, redact_signed_url
from app.ai.staging import ExternalFileReference
from app.ai.streamed_source import StreamedSource, iter_streamed_chunks
from app.ai.transfer import (
    MANAGED_URL_DEFAULT_TTL_SECONDS,
    MANAGED_URL_MAX_TTL_SECONDS,
    NON_INLINE_MIME_TYPES,
    SourceLifecycle,
    TransferMode,
)
from app.storage.fake import FakeObjectStorage
from app.storage.types import ObjectInfo

_ORGANISATION_ID = UUID("11111111-1111-7111-8111-111111111111")


def _source_key(organisation_id: UUID = _ORGANISATION_ID) -> str:
    return f"organisations/{organisation_id}/documents/lease.pdf"


async def _seed_source(
    storage: FakeObjectStorage,
    *,
    content: bytes = b"%PDF-1.7 fixture body" * 100,
    content_type: str = "application/pdf",
) -> str:
    """Declare and store one private source object; returns its key."""
    key = _source_key()
    await storage.create_upload_url(file_id=uuid4(), object_key=key, content_type=content_type, size_bytes=len(content))
    await storage.put(key, content, content_type=content_type)
    return key


def _reference(**overrides: Any) -> ExternalFileReference:
    """A durable managed-signed-url reference for a retained source."""
    values: dict[str, Any] = {
        "mode": TransferMode.MANAGED_SIGNED_URL,
        "provider": "openai",
        "external_id": _source_key(),
        "source_reference": _source_key(),
        "source_digest": "a" * 64,
        "size_bytes": 1600,
        "mime_type": "application/pdf",
        "source_lifecycle": SourceLifecycle.RETAINED,
        "region": "eu-west-1",
        "organisation_id": _ORGANISATION_ID,
        "logical_request_id": "req-123",
        "idempotency_key": "key-123",
        "created_at": datetime.now(UTC),
    }
    values.update(overrides)
    return ExternalFileReference(**values)


# --- Fake storage stream_object ---------------------------------------------


async def test_fake_stream_object_round_trips_stored_bytes() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    key = await _seed_source(storage, content=b"hello world")
    buffer = io.BytesIO()
    await storage.stream_object(key, destination=buffer)
    assert buffer.getvalue() == b"hello world"


async def test_fake_stream_object_missing_raises_key_error() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    with pytest.raises(KeyError):
        await storage.stream_object("organisations/x/absent.pdf", destination=io.BytesIO())


async def test_fake_stream_object_is_bounded_by_max_bytes() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    key = await _seed_source(storage, content=b"0123456789")
    buffer = io.BytesIO()
    with pytest.raises(ValueError):
        await storage.stream_object(key, destination=buffer, max_bytes=5)
    assert buffer.getvalue() == b""  # nothing was written before the failure


# --- StreamedSource ---------------------------------------------------------


async def test_streamed_source_verifies_metadata_and_digest() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    content = b"%PDF-1.7 \xe2\x9c\x93 fixture" * 200
    key = await _seed_source(storage, content=content)
    async with StreamedSource(
        storage=storage,
        reference=key,
        organisation_id=_ORGANISATION_ID,
        max_bytes=5_000_000,
    ) as source:
        assert source.size_bytes == len(content)
        assert source.mime_type == "application/pdf"
        assert source.sha256_digest == hashlib.sha256(content).hexdigest()
        assert source.path.read_bytes() == content
        # The temporary file is private (0600), never world-readable.
        assert stat.S_IMODE(source.path.stat().st_mode) == 0o600
    # The temporary file is removed on exit.
    with pytest.raises(RuntimeError):
        _ = source.path


async def test_streamed_source_denies_cross_organisation_reference() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    key = await _seed_source(storage)
    other = UUID("22222222-2222-7222-8222-222222222222")
    with pytest.raises(AIInputValidationError, match="not accessible"):
        async with StreamedSource(
            storage=storage, reference=key, organisation_id=other, max_bytes=5_000_000
        ):
            pass  # pragma: no cover


async def test_streamed_source_denies_unscoped_reference() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    with pytest.raises(AIInputValidationError, match="not accessible"):
        async with StreamedSource(
            storage=storage,
            reference="some-other/key.pdf",
            organisation_id=_ORGANISATION_ID,
            max_bytes=5_000_000,
        ):
            pass  # pragma: no cover


async def test_streamed_source_missing_object_fails_closed() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    with pytest.raises(AIInputValidationError, match="does not exist"):
        async with StreamedSource(
            storage=storage,
            reference=_source_key(),
            organisation_id=_ORGANISATION_ID,
            max_bytes=5_000_000,
        ):
            pass  # pragma: no cover


async def test_streamed_source_rejects_oversized_object_from_head() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    key = await _seed_source(storage, content=b"x" * 4096)
    with pytest.raises(AIInputValidationError, match="too large"):
        async with StreamedSource(
            storage=storage,
            reference=key,
            organisation_id=_ORGANISATION_ID,
            max_bytes=1024,
        ):
            pass  # pragma: no cover


async def test_streamed_source_gates_mime_type() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    key = await _seed_source(storage, content_type="text/plain")
    with pytest.raises(AIInputValidationError, match="unsupported content type"):
        async with StreamedSource(
            storage=storage,
            reference=key,
            organisation_id=_ORGANISATION_ID,
            max_bytes=5_000_000,
            allowed_mime_types=NON_INLINE_MIME_TYPES,
        ):
            pass  # pragma: no cover


async def test_streamed_source_detects_head_read_race() -> None:
    class _GrowingSource(FakeObjectStorage):
        """Reports a smaller head size than the object actually stores."""

        async def head_object(self, object_key: str) -> Any:
            info = await super().head_object(object_key)
            if info is not None:
                info = ObjectInfo(
                    object_key=info.object_key,
                    size_bytes=info.size_bytes // 2,
                    content_type=info.content_type,
                    checksum=info.checksum,
                    last_modified=info.last_modified,
                )
            return info

    storage = _GrowingSource(bucket="test-bucket")
    key = await _seed_source(storage, content=b"y" * 2048)
    with pytest.raises(AIInputValidationError, match="changed while being read"):
        async with StreamedSource(
            storage=storage,
            reference=key,
            organisation_id=_ORGANISATION_ID,
            max_bytes=5_000_000,
        ):
            pass  # pragma: no cover


async def test_streamed_source_stream_failure_is_cleaned_up() -> None:
    class _FailingStream(FakeObjectStorage):
        async def stream_object(
            self, object_key: str, *, destination: Any, max_bytes: int | None = None
        ) -> None:
            raise ValueError("provider read failed mid-stream")

    storage = _FailingStream(bucket="test-bucket")
    key = await _seed_source(storage)
    with pytest.raises(AIInputValidationError, match="could not be read"):
        async with StreamedSource(
            storage=storage,
            reference=key,
            organisation_id=_ORGANISATION_ID,
            max_bytes=5_000_000,
        ):
            pass  # pragma: no cover


async def test_iter_streamed_chunks_yields_the_whole_body() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    content = bytes(range(256)) * 40  # 10 KB
    key = await _seed_source(storage, content=content)
    async with StreamedSource(
        storage=storage,
        reference=key,
        organisation_id=_ORGANISATION_ID,
        max_bytes=5_000_000,
    ) as source:
        assert b"".join(iter_streamed_chunks(source, chunk_size=1024)) == content


# --- Managed download-URL minting -------------------------------------------


async def test_redact_signed_url_strips_query_and_fragment() -> None:
    url = "https://storage.example.invalid/download/organisations/1/x.pdf?X-Amz-Signature=abc&X-Amz-Expires=900#frag"
    redacted = redact_signed_url(url)
    assert redacted == "https://storage.example.invalid/download/organisations/1/x.pdf"
    assert "abc" not in redacted
    assert "900" not in redacted


async def test_mint_managed_url_verifies_identity_and_returns_short_https_get() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    content = b"%PDF-1.7 managed" * 100
    key = await _seed_source(storage, content=content)
    reference = _reference(
        size_bytes=len(content), source_digest=hashlib.sha256(content).hexdigest()
    )
    signed = await mint_managed_download_url(storage=storage, reference=reference, ttl_seconds=1200)
    assert signed.method == "GET"
    assert signed.url.startswith("https://")
    assert signed.url.startswith(f"https://storage.example.invalid/download/{key}")
    # TTL is bounded to the reviewed window and enforced by the minter.
    assert (
        MANAGED_URL_DEFAULT_TTL_SECONDS
        <= (signed.expires_at - datetime.now(UTC)).total_seconds()
        <= MANAGED_URL_MAX_TTL_SECONDS
    )


async def test_mint_managed_url_defaults_to_the_reviewed_ttl() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    content = b"%PDF-1.7" * 100
    await _seed_source(storage, content=content)
    reference = _reference(
        size_bytes=len(content), source_digest=hashlib.sha256(content).hexdigest()
    )
    signed = await mint_managed_download_url(storage=storage, reference=reference)
    remaining = (signed.expires_at - datetime.now(UTC)).total_seconds()
    assert MANAGED_URL_DEFAULT_TTL_SECONDS - 5 <= remaining <= MANAGED_URL_DEFAULT_TTL_SECONDS


async def test_mint_managed_url_rejects_out_of_bounds_ttl() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    content = b"%PDF-1.7" * 100
    reference = _reference(size_bytes=len(content))
    with pytest.raises(TransferSourceError, match="TTL"):
        await mint_managed_download_url(storage=storage, reference=reference, ttl_seconds=60)
    with pytest.raises(TransferSourceError, match="TTL"):
        await mint_managed_download_url(storage=storage, reference=reference, ttl_seconds=3600)


async def test_mint_managed_url_requires_managed_mode_and_retained_lifecycle() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    content = b"%PDF-1.7" * 100
    upload = _reference(mode=TransferMode.PROVIDER_UPLOAD, size_bytes=len(content))
    with pytest.raises(TransferSourceError, match="managed-signed-url mode"):
        await mint_managed_download_url(storage=storage, reference=upload)
    transient = _reference(source_lifecycle=SourceLifecycle.TRANSIENT, size_bytes=len(content))
    with pytest.raises(TransferSourceError, match="retained"):
        await mint_managed_download_url(storage=storage, reference=transient)


async def test_mint_managed_url_denies_cross_organisation_object() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    content = b"%PDF-1.7" * 100
    reference = _reference(
        organisation_id=UUID("22222222-2222-7222-8222-222222222222"), size_bytes=len(content)
    )
    with pytest.raises(TransferSourceError, match="not accessible"):
        await mint_managed_download_url(storage=storage, reference=reference)


async def test_mint_managed_url_rejects_missing_or_changed_object() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    content = b"%PDF-1.7" * 100
    await _seed_source(storage, content=content)
    missing = _reference(size_bytes=len(content), source_reference=_source_key() + "-missing")
    with pytest.raises(TransferSourceError, match="does not exist"):
        await mint_managed_download_url(storage=storage, reference=missing)
    changed_size = _reference(size_bytes=len(content) + 1)
    with pytest.raises(TransferSourceError, match="changed"):
        await mint_managed_download_url(storage=storage, reference=changed_size)
    changed_mime = _reference(size_bytes=len(content), mime_type="text/plain")
    with pytest.raises(TransferSourceError, match="changed"):
        await mint_managed_download_url(storage=storage, reference=changed_mime)


async def test_mint_managed_url_is_never_embedded_in_errors() -> None:
    storage = FakeObjectStorage(bucket="test-bucket")
    content = b"%PDF-1.7" * 100
    await _seed_source(storage, content=content)
    reference = _reference(size_bytes=len(content) + 1)
    with pytest.raises(TransferSourceError) as exc_info:
        await mint_managed_download_url(storage=storage, reference=reference)
    assert "X-Amz" not in str(exc_info.value)
    assert "Signature" not in str(exc_info.value)
    assert "storage.example" not in str(exc_info.value)


async def test_mint_managed_url_rejects_replaced_content_with_same_size_and_mime() -> None:
    """Exact identity is the digest, not just size and MIME (Scope §2.3).

    An object replaced at the same key with different bytes of the same length
    and content type passes the head check, so the minter must re-stream and
    re-digest the object: a valid URL may never be minted for content whose
    SHA-256 no longer matches the durable reference.
    """
    storage = FakeObjectStorage(bucket="test-bucket")
    original = b"%PDF-1.7 original-body!" * 8
    key = await _seed_source(storage, content=original)
    reference = _reference(
        size_bytes=len(original), source_digest=hashlib.sha256(original).hexdigest()
    )
    # The object at the same key is replaced with DIFFERENT bytes of the SAME
    # length and the same content type: head size and MIME still match.
    replacement = b"%PDF-1.7 REPLACED body?" * 8
    assert len(replacement) == len(original)
    assert hashlib.sha256(replacement).hexdigest() != reference.source_digest
    await storage.put(key, replacement, content_type="application/pdf")
    with pytest.raises(TransferSourceError, match="changed"):
        await mint_managed_download_url(storage=storage, reference=reference)
