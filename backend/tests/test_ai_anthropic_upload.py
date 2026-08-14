"""Fake-backed Anthropic upload tests (v0.8 Scope §2.4, §6.6 checkbox 3).

The Anthropic large-file path uploads a verified transient source to the beta
Files API (delete-only retention: uploaded files persist until
``DELETE /v1/files/{file_id}``) and passes the provider file id (or a
just-in-time managed URL for retained sources) through the Messages API as a
``document`` source. These tests exercise the whole contract hermetically
through the provider-neutral rules and the deterministic
:class:`FakeAnthropicUploadStore` — contract validation (mode, MIME, size,
transient lifecycle, region) failing closed before any upload, idempotent
stage/reference/use, retry-only reuse and best-effort deletion that never
touches the feature-owned source — plus the real
:class:`AnthropicTransferStore` wire format through ``httpx.MockTransport``
(multipart upload with the pinned ``anthropic-beta`` header, digest
verification, safe error normalization and terminal delete), and the
local-transient scratch-GCS no-copy path. The fake and the real adapter share
the exact same validation rules, so they cannot drift; live Anthropic behavior
is covered by the opt-in ``ai_contracts`` test in ``test_ai_contracts.py``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pypdf import PdfWriter

from app.ai.anthropic_staging import (
    ANTHROPIC_FILES_BETA_VERSION,
    ANTHROPIC_UPLOAD_MAX_BYTES,
    FakeAnthropicUploadStore,
    anthropic_pdf_page_ceiling,
    validate_anthropic_upload,
)
from app.ai.errors import (
    AIInputValidationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    TransferStagingError,
)
from app.ai.pdf_inspection import count_pdf_pages
from app.ai.providers.anthropic_upload import AnthropicTransferStore
from app.ai.staging import ExternalReferenceStatus
from app.ai.transfer import SourceLifecycle, TransferMode, derive_idempotency_key

_ORGANISATION_ID = uuid.uuid4()
_REGION = ""  # the template's Anthropic deployment default has no geography pinning
_SOURCE_KEY = f"organisations/{_ORGANISATION_ID}/ai/scratch/lease.pdf"


def _pdf_content() -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def _classic_pdf(objects: list[tuple[int, bytes]]) -> bytes:
    """Assemble indirect objects into a classic xref-table PDF.

    Real cross-reference table, trailer and ``startxref`` so the bounded
    inspector can resolve the effective page tree exactly like a producer
    PDF. Missing object numbers are marked free (``f``) so an unresolvable
    reference fails closed rather than resolving to a wrong offset.
    """
    body = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for number, obj in objects:
        offsets[number] = len(body)
        body += obj
    xref_offset = len(body)
    max_number = max(number for number, _ in objects)
    body += b"xref\n0 %d\n" % (max_number + 1)
    body += b"0000000000 65535 f \n"
    for number in range(1, max_number + 1):
        offset = offsets.get(number)
        if offset is None:
            body += b"0000000000 65535 f \n"
        else:
            body += b"%010d 00000 n \n" % offset
    body += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (max_number + 1)
    body += f"startxref\n{xref_offset}\n%%EOF\n".encode()
    return bytes(body)


def _pdf_with_pages(page_count: int) -> bytes:
    """A minimal classic xref-table PDF with ``page_count`` page-tree leaves:
    objects 1 (catalog) and 2 (Pages node) plus one ``/Type /Page`` leaf per
    page (the shape the bounded inspector walks)."""
    objects: list[tuple[int, bytes]] = [
        (1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"),
        (
            2,
            (
                b"2 0 obj\n<< /Type /Pages /Count %d /Kids [%s] >>\nendobj\n"
                % (page_count, b" ".join(b"%d 0 R" % i for i in range(3, 3 + page_count)))
            ),
        ),
    ]
    objects.extend(
        (i, b"%d 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n" % i)
        for i in range(3, 3 + page_count)
    )
    return _classic_pdf(objects)


def _stage_args(**overrides: object) -> dict[str, Any]:
    args: dict[str, Any] = {
        "mode": TransferMode.PROVIDER_UPLOAD,
        "organisation_id": _ORGANISATION_ID,
        "logical_request_id": "req-anthropic-1",
        "source_reference": _SOURCE_KEY,
        "source_digest": "a" * 64,
        "mime_type": "application/pdf",
        "size_bytes": 1600,
        "source_lifecycle": SourceLifecycle.TRANSIENT,
        "region": _REGION,
        "expires_at": None,
    }
    args.update(overrides)
    return args


def _fake_store(**overrides: Any) -> FakeAnthropicUploadStore:
    return FakeAnthropicUploadStore(region=_REGION, **overrides)


def _file_object(*, file_id: str = "file_011CNha8iCJcU1wXNR6q4V8w") -> dict[str, Any]:
    return {
        "id": file_id,
        "type": "file",
        "filename": "attachment.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1600,
        "created_at": datetime.now(UTC).isoformat(),
        "downloadable": False,
    }


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json_response(payload: dict[str, Any], *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://test"))


def _store(**overrides: Any) -> AnthropicTransferStore:
    return AnthropicTransferStore(api_key="ant-test", region=_REGION, **overrides)


@pytest.fixture
def source_pdf(tmp_path: Path) -> tuple[Path, bytes]:
    """A verified secure temporary file plus its bytes, as the streaming seam
    would hand the store (Scope §2.3)."""
    content = _pdf_content()
    path = tmp_path / "fixture-verified.pdf"
    path.write_bytes(content)
    return path, content


def _real_stage_args(source_pdf: tuple[Path, bytes], **overrides: object) -> dict[str, Any]:
    path, content = source_pdf
    args = _stage_args(
        source_path=path,
        source_digest=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    args.update(overrides)
    return args


# --- Shared contract validation (Scope §2.4, §5.3) ---------------------------


def test_validate_anthropic_upload_accepts_reviewed_contract() -> None:
    validate_anthropic_upload(
        mode=TransferMode.PROVIDER_UPLOAD,
        mime_type="application/pdf",
        size_bytes=ANTHROPIC_UPLOAD_MAX_BYTES,
        source_lifecycle=SourceLifecycle.TRANSIENT,
        region=_REGION,
        configured_region=_REGION,
    )  # must not raise


@pytest.mark.parametrize(
    "updates",
    [
        {"mode": TransferMode.MANAGED_SIGNED_URL},
        {"mime_type": "image/png"},
        {"size_bytes": ANTHROPIC_UPLOAD_MAX_BYTES + 1},
        {"source_lifecycle": SourceLifecycle.RETAINED},
        {"region": "us"},
    ],
)
def test_validate_anthropic_upload_fails_closed(updates: dict[str, object]) -> None:
    args: dict[str, Any] = {
        "mode": TransferMode.PROVIDER_UPLOAD,
        "mime_type": "application/pdf",
        "size_bytes": 1600,
        "source_lifecycle": SourceLifecycle.TRANSIENT,
        "region": _REGION,
        "configured_region": _REGION,
    }
    args.update(updates)
    with pytest.raises(TransferStagingError):
        validate_anthropic_upload(**args)


# --- PDF page/context ceiling (Scope §6.6 checkbox 2) -------------------------


def test_count_pdf_pages_reads_the_authoritative_pages_count(tmp_path: Path) -> None:
    path = tmp_path / "many.pdf"
    path.write_bytes(_pdf_with_pages(101))
    assert count_pdf_pages(path) == 101


def test_count_pdf_pages_walks_nested_page_trees(
    tmp_path: Path,
) -> None:
    """The count comes from the *effective* page tree: nested /Pages nodes are
    walked leaf by leaf rather than trusting declared /Count metadata."""
    objects: list[tuple[int, bytes]] = [
        (1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"),
        # Root pages node: one nested subtree (object 3, two leaves) plus
        # two direct leaves -> 4 leaves total.
        (2, b"2 0 obj\n<< /Type /Pages /Count 4 /Kids [3 0 R 6 0 R 7 0 R] >>\nendobj\n"),
        (3, b"3 0 obj\n<< /Type /Pages /Count 2 /Kids [4 0 R 5 0 R] >>\nendobj\n"),
        (4, b"4 0 obj\n<< /Type /Page /Parent 3 0 R >>\nendobj\n"),
        (5, b"5 0 obj\n<< /Type /Page /Parent 3 0 R >>\nendobj\n"),
        (6, b"6 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"),
        (7, b"7 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"),
    ]
    path = tmp_path / "nested.pdf"
    path.write_bytes(_classic_pdf(objects))
    assert count_pdf_pages(path) == 4


def test_count_pdf_pages_fails_closed_without_a_resolvable_page_tree(
    tmp_path: Path,
) -> None:
    """A PDF with /Type /Page objects but no page tree reachable from the
    trailer catalog fails closed instead of guessing: the effective tree is
    the only trustworthy source for the count."""
    content = (
        b"%PDF-1.7\n1 0 obj\n<< /Type /Page >>\nendobj\n"
        b"2 0 obj\n<< /Type /Page >>\nendobj\n"
        b"trailer\n<< /Root 9 0 R >>\n%%EOF\n"
    )
    path = tmp_path / "flat.pdf"
    path.write_bytes(content)
    with pytest.raises(AIInputValidationError, match="could not be inspected safely"):
        count_pdf_pages(path)


def test_count_pdf_pages_rejects_non_pdf_content(tmp_path: Path) -> None:
    path = tmp_path / "not-a-pdf.txt"
    path.write_bytes(b"hello world")
    with pytest.raises(AIInputValidationError, match="could not be inspected safely"):
        count_pdf_pages(path)


def test_count_pdf_pages_fails_closed_when_the_count_cannot_be_verified(
    tmp_path: Path,
) -> None:
    """Safe context-error normalization: a PDF whose page count cannot be
    verified (no cross-reference table) raises a normalized input error,
    never a raw exception (BP §28)."""
    path = tmp_path / "opaque.pdf"
    path.write_bytes(_pdf_content())
    with pytest.raises(AIInputValidationError, match="could not be inspected safely"):
        count_pdf_pages(path)


def test_count_pdf_pages_uses_reachable_leaves_not_declared_count(tmp_path: Path) -> None:
    """A stale/hostile /Count cannot smuggle leaves past the gate: inspection
    counts the effective page-tree leaves instead of trusting metadata."""
    objects: list[tuple[int, bytes]] = [
        (1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"),
        # /Count says 5 but six leaves are reachable.
        (
            2,
            b"2 0 obj\n<< /Type /Pages /Count 5 /Kids [3 0 R 4 0 R 5 0 R 6 0 R 7 0 R 8 0 R] >>\nendobj\n",
        ),
    ]
    objects.extend(
        (i, b"%d 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n" % i) for i in range(3, 9)
    )
    path = tmp_path / "under-declared.pdf"
    path.write_bytes(_classic_pdf(objects))
    assert count_pdf_pages(path) == 6


def test_count_pdf_pages_rejects_a_cyclic_page_tree(tmp_path: Path) -> None:
    """A /Kids chain that loops back on itself fails closed instead of walking
    forever (bounded traversal)."""
    objects: list[tuple[int, bytes]] = [
        (1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"),
        # Object 2's kid is object 3, whose kid is object 2 again.
        (2, b"2 0 obj\n<< /Type /Pages /Count 1 /Kids [3 0 R] >>\nendobj\n"),
        (3, b"3 0 obj\n<< /Type /Pages /Count 1 /Kids [2 0 R] >>\nendobj\n"),
    ]
    path = tmp_path / "cyclic.pdf"
    path.write_bytes(_classic_pdf(objects))
    with pytest.raises(AIInputValidationError, match="could not be inspected safely"):
        count_pdf_pages(path)


def test_count_pdf_pages_rejects_an_unresolvable_kid(tmp_path: Path) -> None:
    """A /Kids reference to an object absent from the cross-reference table
    (free/missing) fails closed: the effective tree cannot be proven."""
    objects: list[tuple[int, bytes]] = [
        (1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"),
        # Kid 3 0 R is not among the defined objects (only 1 and 2 exist).
        (2, b"2 0 obj\n<< /Type /Pages /Count 1 /Kids [3 0 R] >>\nendobj\n"),
    ]
    path = tmp_path / "missing-kid.pdf"
    path.write_bytes(_classic_pdf(objects))
    with pytest.raises(AIInputValidationError, match="could not be inspected safely"):
        count_pdf_pages(path)


def test_count_pdf_pages_accepts_incremental_updates(tmp_path: Path) -> None:
    """Incremental updates are a normal PDF feature and remain inspectable."""
    path = tmp_path / "incremental.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(path)
    writer = PdfWriter(path, incremental=True)
    writer.add_metadata({"/Title": "updated without rewriting the original revision"})
    writer.write(path)

    assert path.read_bytes().count(b"%%EOF") == 2
    assert count_pdf_pages(path) == 1


def test_count_pdf_pages_rejects_over_deep_page_trees(tmp_path: Path) -> None:
    """The traversal depth is bounded: a pathological chain nested beyond the
    cap fails closed instead of recursing unbounded."""
    objects: list[tuple[int, bytes]] = [
        (1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    ]
    depth = 70
    for level in range(depth):
        number = 2 + level
        next_number = 2 + level + 1
        objects.append(
            (
                number,
                b"%d 0 obj\n<< /Type /Pages /Count 1 /Kids [%d 0 R] >>\nendobj\n"
                % (number, next_number),
            )
        )
    leaf = 2 + depth
    objects.append(
        (leaf, b"%d 0 obj\n<< /Type /Page /Parent %d 0 R >>\nendobj\n" % (leaf, leaf - 1))
    )
    path = tmp_path / "deep.pdf"
    path.write_bytes(_classic_pdf(objects))
    with pytest.raises(AIInputValidationError, match="could not be inspected safely"):
        count_pdf_pages(path)


def test_count_pdf_pages_peak_reads_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded-input evidence (Scope §2.3/§5.3): the inspector never
    accumulates the source. The largest single read is one fixed window (the
    1 MB per-object cap), far below the provider's 32 MB ceiling — the file is
    streamed in chunks even when it is provider-sized."""

    class _RecordingFile:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.max_read = 0

        def read(self, size: int = -1) -> bytes:
            buf = self._inner.read(size)
            self.max_read = max(self.max_read, len(buf))
            return buf

        def write(self, data: bytes) -> int:
            return self._inner.write(data)

        def seek(self, offset: int, whence: int = 0) -> int:
            return self._inner.seek(offset, whence)

        def tell(self) -> int:
            return self._inner.tell()

        def __enter__(self) -> _RecordingFile:
            self._inner.__enter__()
            return self

        def __exit__(self, *exc: object) -> None:
            self._inner.__exit__(*exc)

        def close(self) -> None:
            self._inner.close()

    import app.ai.pdf_inspection as pdf_inspection

    original_open = pdf_inspection.Path.open
    recordings: list[_RecordingFile] = []

    def recording_open(self: Path, *args: Any, **kwargs: Any) -> _RecordingFile:
        rec = _RecordingFile(original_open(self, *args, **kwargs))
        recordings.append(rec)
        return rec

    monkeypatch.setattr(pdf_inspection.Path, "open", recording_open)

    # A provider-sized source: the page tree plus an 8 MB stream object, so
    # the file is far larger than any fixed read window while the trailer,
    # xref table and startxref stay at the end where a real PDF keeps them.
    big = 8 * 1024 * 1024
    stream_obj = (
        b"6 0 obj\n<< /Length %d >>\nstream\n" % big + b" " * big + b"\nendstream\nendobj\n"
    )
    content = _classic_pdf(
        [
            (1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"),
            (2, b"2 0 obj\n<< /Type /Pages /Count 3 /Kids [3 0 R 4 0 R 5 0 R] >>\nendobj\n"),
            (3, b"3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"),
            (4, b"4 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"),
            (5, b"5 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"),
            (6, stream_obj),
        ]
    )
    path = tmp_path / "provider-sized.pdf"
    path.write_bytes(content)

    assert count_pdf_pages(path, cap=100) == 3
    assert recordings
    # Every single read is at most one fixed window; the 8 MB source is never
    # accumulated (peak memory stays far below the provider's 32 MB ceiling).
    assert all(rec.max_read <= 1024 * 1024 for rec in recordings)


def test_anthropic_pdf_page_ceiling_derives_from_the_reviewed_contract() -> None:
    """The effective ceiling comes from providers.yaml `pdf_pages` and the
    model's context window: below 1M tokens the tighter 100-page ceiling
    applies, at/above it the 600-page ceiling (Scope §6.6 checkbox 2)."""
    assert anthropic_pdf_page_ceiling(200_000) == 100  # claude-sonnet-4-6
    assert anthropic_pdf_page_ceiling(999_999) == 100
    assert anthropic_pdf_page_ceiling(1_000_000) == 600
    assert anthropic_pdf_page_ceiling(None) == 100  # conservative


# --- FakeAnthropicUploadStore: stage/reuse/delete -----------------------------


async def test_fake_stage_creates_delete_only_reference() -> None:
    store = _fake_store()
    reference = await store.stage(**_stage_args())
    assert reference.provider == "anthropic"
    assert reference.mode is TransferMode.PROVIDER_UPLOAD
    assert reference.region == _REGION
    assert reference.source_lifecycle is SourceLifecycle.TRANSIENT
    assert reference.external_id.startswith("file-fake-")
    assert "signed" not in reference.external_id.lower()
    assert reference.idempotency_key == derive_idempotency_key(
        provider="anthropic",
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-anthropic-1",
        source_digest="a" * 64,
        region=_REGION,
    )
    # Delete-only retention kind (``until_deleted``): Anthropic has no
    # automatic expiry, so the durable reference records none (providers.yaml).
    assert reference.expires_at is None
    assert store.uploads == [reference.external_id]


async def test_fake_stage_is_idempotent_per_logical_transfer() -> None:
    store = _fake_store()
    first = await store.stage(**_stage_args())
    second = await store.stage(**_stage_args())
    assert second.external_id == first.external_id
    assert second.idempotency_key == first.idempotency_key
    # One upload, one durable reference.
    assert len(store.uploads) == 1
    assert len(store.records) == 1


async def test_fake_stage_digest_change_creates_a_new_transfer() -> None:
    store = _fake_store()
    first = await store.stage(**_stage_args(source_digest="a" * 64))
    changed = await store.stage(**_stage_args(source_digest="b" * 64))
    assert changed.idempotency_key != first.idempotency_key
    assert changed.external_id != first.external_id
    assert len(store.uploads) == 2


async def test_fake_stage_refuses_non_provider_upload_mode() -> None:
    store = _fake_store()
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(mode=TransferMode.STORAGE_REFERENCE))
    assert store.uploads == []


async def test_fake_stage_fails_closed_on_region_mismatch() -> None:
    store = _fake_store()
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(region="us"))
    assert store.uploads == []


async def test_fake_stage_rejects_non_pdf_oversized_and_retained_sources() -> None:
    store = _fake_store()
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(mime_type="image/png"))
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(size_bytes=ANTHROPIC_UPLOAD_MAX_BYTES + 1))
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(source_lifecycle=SourceLifecycle.RETAINED))
    assert store.uploads == []


async def test_fake_find_reusable_is_scoped_to_one_logical_request() -> None:
    store = _fake_store()
    reference = await store.stage(**_stage_args())
    hit = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-anthropic-1",
        source_digest="a" * 64,
        region=_REGION,
    )
    assert hit is not None and hit.external_id == reference.external_id
    miss = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-anthropic-2",
        source_digest="a" * 64,
        region=_REGION,
    )
    assert miss is None
    # A changed digest never reuses the earlier upload (Scope §5.4).
    digest_miss = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-anthropic-1",
        source_digest="b" * 64,
        region=_REGION,
    )
    assert digest_miss is None


async def test_fake_delete_removes_only_the_provider_copy() -> None:
    store = _fake_store()
    reference = await store.stage(**_stage_args())
    await store.delete(reference)
    assert store.deleted == [reference.external_id]
    assert reference.status is ExternalReferenceStatus.DELETED
    # A second delete is a no-op (best-effort idempotent terminal cleanup).
    await store.delete(reference)
    assert store.deleted == [reference.external_id]


# --- AnthropicTransferStore: wire format, verification, error mapping --------


def test_store_constructor_requires_key() -> None:
    with pytest.raises(AIInputValidationError):
        AnthropicTransferStore(api_key="")


async def test_store_stage_uploads_multipart_with_pinned_beta_header(
    source_pdf: tuple[Path, bytes],
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_file_object())

    store = _store(client=_client(handler))
    reference = await store.stage(**_real_stage_args(source_pdf))
    sent = captured[0]
    assert sent.url == "https://api.anthropic.com/v1/files"
    assert sent.headers["x-api-key"] == "ant-test"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    # The reviewed beta header/version, pinned in one place (Scope §6.6
    # checkbox 1).
    assert sent.headers["anthropic-beta"] == ANTHROPIC_FILES_BETA_VERSION
    text = sent.read().decode("latin-1")
    assert 'name="file"' in text
    assert reference.external_id == "file_011CNha8iCJcU1wXNR6q4V8w"
    # Delete-only retention: no automatic expiry on the durable reference.
    assert reference.expires_at is None
    _path, content = source_pdf
    assert reference.idempotency_key == derive_idempotency_key(
        provider="anthropic",
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-anthropic-1",
        source_digest=hashlib.sha256(content).hexdigest(),
        region=_REGION,
    )
    await store.aclose()


async def test_store_stage_is_idempotent_without_a_second_upload(
    source_pdf: tuple[Path, bytes],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(_file_object(file_id=f"file_{calls}"))

    store = _store(client=_client(handler))
    first = await store.stage(**_real_stage_args(source_pdf))
    second = await store.stage(**_real_stage_args(source_pdf))
    assert second.external_id == first.external_id
    assert calls == 1
    await store.aclose()


async def test_store_stage_verifies_the_uploaded_digest(
    source_pdf: tuple[Path, bytes],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_file_object())

    store = _store(client=_client(handler))
    # The temp file does not match the claimed digest: the uploaded copy was
    # not byte-identical to the verified source, so no reference is created.
    with pytest.raises(TransferStagingError):
        await store.stage(**_real_stage_args(source_pdf, source_digest="b" * 64))
    await store.aclose()


async def test_store_stage_deletes_provider_file_when_post_upload_verification_fails(
    source_pdf: tuple[Path, bytes],
) -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _json_response(_file_object())
        deleted.append(request.url.path)
        assert request.headers["anthropic-beta"] == ANTHROPIC_FILES_BETA_VERSION
        return _json_response({"id": "file_011CNha8iCJcU1wXNR6q4V8w", "type": "file_deleted"})

    store = _store(client=_client(handler))
    # The upload already succeeded; the digest check then fails, so the
    # untracked provider copy must be deleted best-effort (the orchestrator's
    # compensation only runs once stage() returns, and no durable row exists).
    with pytest.raises(TransferStagingError):
        await store.stage(**_real_stage_args(source_pdf, source_digest="b" * 64))
    assert deleted == ["/v1/files/file_011CNha8iCJcU1wXNR6q4V8w"]
    await store.aclose()


async def test_store_stage_unparseable_response_leaves_no_file_to_delete(
    source_pdf: tuple[Path, bytes],
) -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
        return httpx.Response(
            200, content=b"not json", request=httpx.Request("POST", "http://test")
        )

    store = _store(client=_client(handler))
    with pytest.raises(ProviderResponseError):
        await store.stage(**_real_stage_args(source_pdf))
    # No file id is discoverable, so nothing addressable is deleted.
    assert deleted == []
    await store.aclose()


async def test_store_stage_missing_file_id_leaves_no_file_to_delete(
    source_pdf: tuple[Path, bytes],
) -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
        return _json_response({"object": "file"})

    store = _store(client=_client(handler))
    with pytest.raises(ProviderResponseError):
        await store.stage(**_real_stage_args(source_pdf))
    assert deleted == []
    await store.aclose()


async def test_store_stage_requires_the_verified_source_file() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_file_object())

    store = _store(client=_client(handler))
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args())
    await store.aclose()


async def test_store_stage_refuses_before_any_upload_when_invalid(
    source_pdf: tuple[Path, bytes],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(_file_object())

    store = _store(client=_client(handler))
    with pytest.raises(TransferStagingError):
        await store.stage(**_real_stage_args(source_pdf, mime_type="image/png"))
    with pytest.raises(TransferStagingError):
        await store.stage(**_real_stage_args(source_pdf, source_lifecycle=SourceLifecycle.RETAINED))
    assert calls == 0
    await store.aclose()


async def test_store_stage_maps_rate_limit_and_server_errors_as_retryable(
    source_pdf: tuple[Path, bytes],
) -> None:
    for status in (429, 503):

        def handler(request: httpx.Request, _status: int = status) -> httpx.Response:
            return _json_response({}, status=_status)

        store = _store(client=_client(handler))
        with pytest.raises((ProviderRateLimitError, ProviderUnavailableError)):
            await store.stage(**_real_stage_args(source_pdf))
        await store.aclose()


async def test_store_stage_maps_transport_timeout_as_retryable(
    source_pdf: tuple[Path, bytes],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    store = _store(client=_client(handler))
    with pytest.raises(ProviderTimeoutError) as excinfo:
        await store.stage(**_real_stage_args(source_pdf))
    assert excinfo.value.retryable is True
    await store.aclose()


async def test_store_stage_maps_permanent_refusal_without_leaking_details(
    source_pdf: tuple[Path, bytes],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"error": {"message": "super-secret-provider-body"}}, status=400)

    store = _store(client=_client(handler))
    with pytest.raises(TransferStagingError) as excinfo:
        await store.stage(**_real_stage_args(source_pdf))
    assert "super-secret-provider-body" not in str(excinfo.value)
    await store.aclose()


async def test_store_delete_removes_the_provider_copy_only(
    source_pdf: tuple[Path, bytes],
) -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _json_response(_file_object())
        deleted.append(request.url.path)
        assert request.headers["anthropic-beta"] == ANTHROPIC_FILES_BETA_VERSION
        return _json_response({"id": "file_011CNha8iCJcU1wXNR6q4V8w", "type": "file_deleted"})

    store = _store(client=_client(handler))
    reference = await store.stage(**_real_stage_args(source_pdf))
    await store.delete(reference)
    assert deleted == ["/v1/files/file_011CNha8iCJcU1wXNR6q4V8w"]
    assert reference.status is ExternalReferenceStatus.DELETED
    # A second delete does not repeat the provider call (idempotent).
    await store.delete(reference)
    assert deleted == ["/v1/files/file_011CNha8iCJcU1wXNR6q4V8w"]
    await store.aclose()


async def test_store_delete_tolerates_an_already_missing_file(
    source_pdf: tuple[Path, bytes],
) -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _json_response(_file_object())
        deleted.append(request.url.path)
        return _json_response({"type": "not_found_error"}, status=404)

    store = _store(client=_client(handler))
    reference = await store.stage(**_real_stage_args(source_pdf))
    await store.delete(reference)  # must not raise
    assert deleted == ["/v1/files/file_011CNha8iCJcU1wXNR6q4V8w"]
    assert reference.status is ExternalReferenceStatus.DELETED
    await store.aclose()


async def test_store_delete_failure_propagates_for_reconciliation(
    source_pdf: tuple[Path, bytes],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _json_response(_file_object())
        return _json_response({}, status=500)

    store = _store(client=_client(handler))
    reference = await store.stage(**_real_stage_args(source_pdf))
    # A genuine deletion failure propagates so the §6.7 reconciliation job can
    # cover the orphan (Scope §2.5).
    with pytest.raises(ProviderUnavailableError):
        await store.delete(reference)
    assert reference.status is ExternalReferenceStatus.LIVE
    await store.aclose()


# --- Local-transient scratch-GCS path (Scope §6.6 lesson learned) -------------


async def test_store_local_transient_stage_skips_the_provider_upload(
    source_pdf: tuple[Path, bytes],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(_file_object())

    store = AnthropicTransferStore(
        api_key="ant-test",
        region=_REGION,
        client=_client(handler),
        stage_transient_locally=True,
    )
    reference = await store.stage(**_real_stage_args(source_pdf))
    assert calls == 0  # no provider file was uploaded
    assert reference.external_id == _SOURCE_KEY  # no-copy shape
    assert reference.source_lifecycle is SourceLifecycle.TRANSIENT
    assert reference.expires_at is None
    # The durable reference is retry-only reusable like any other.
    again = await store.stage(**_real_stage_args(source_pdf))
    assert again.external_id == reference.external_id
    assert calls == 0
    # Terminal cleanup marks the no-copy reference deleted without a provider
    # call: the dispatch-time GCS staged copy is the deployer's age = 1
    # lifecycle backstop (Scope §2.5).
    await store.delete(reference)
    assert reference.status is ExternalReferenceStatus.DELETED
    assert calls == 0
    await store.aclose()


async def test_store_local_transient_still_validates_before_anything(
    source_pdf: tuple[Path, bytes],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(_file_object())

    store = AnthropicTransferStore(
        api_key="ant-test",
        region=_REGION,
        client=_client(handler),
        stage_transient_locally=True,
    )
    # A retained source is never served by the local-transient path: the
    # fail-closed contract rejects it before any staging decision.
    with pytest.raises(TransferStagingError):
        await store.stage(**_real_stage_args(source_pdf, source_lifecycle=SourceLifecycle.RETAINED))
    assert calls == 0
    await store.aclose()


async def test_store_not_local_still_uploads_transient_sources(
    source_pdf: tuple[Path, bytes],
) -> None:
    """A non-local deployment keeps the beta Files API upload for transient
    sources (the local-transient scratch-GCS path is disabled)."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_file_object())

    store = AnthropicTransferStore(api_key="ant-test", region=_REGION, client=_client(handler))
    reference = await store.stage(**_real_stage_args(source_pdf))
    assert len(captured) == 1
    assert captured[0].url == "https://api.anthropic.com/v1/files"
    assert reference.external_id == "file_011CNha8iCJcU1wXNR6q4V8w"
    await store.aclose()
