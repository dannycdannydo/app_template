"""Fake-backed OpenAI upload tests (v0.8 Scope §2.4, §6.5 checkbox 3).

The OpenAI large-file path uploads a verified transient source to the Files
API with ``purpose=user_data`` and the configured ``expires_after``, then
passes the provider file id (or a just-in-time managed URL for retained
sources) through the Responses API ``input_file`` item. These tests exercise
the whole contract hermetically through the provider-neutral rules and the
deterministic :class:`FakeOpenAIUploadStore` — contract validation (mode,
MIME, size, transient lifecycle, region, expiry bounds) failing closed before
any upload, idempotent stage/reference/use, retry-only reuse, expiry and
best-effort deletion that never touches the feature-owned source — plus the
real :class:`OpenAITransferStore` wire format through ``httpx.MockTransport``
(multipart upload with ``purpose=user_data`` and the ``expires_after`` JSON,
digest verification, safe error normalization and terminal delete). The fake
and the real adapter share the exact same validation rules, so they cannot
drift; live OpenAI behavior is covered by the opt-in ``ai_contracts`` test in
``test_ai_contracts.py``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.ai.errors import (
    AIInputValidationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    TransferStagingError,
)
from app.ai.openai_staging import (
    OPENAI_EXPIRES_AFTER_MAX_SECONDS,
    OPENAI_EXPIRES_AFTER_MIN_SECONDS,
    OPENAI_FILES_PURPOSE,
    FakeOpenAIUploadStore,
    validate_openai_upload,
)
from app.ai.providers.openai_upload import OpenAITransferStore
from app.ai.staging import ExternalReferenceStatus
from app.ai.transfer import SourceLifecycle, TransferMode, derive_idempotency_key

_ORGANISATION_ID = uuid.uuid4()
_REGION = ""  # the template's OpenAI deployment default has no region pinning
_SOURCE_KEY = f"organisations/{_ORGANISATION_ID}/ai/scratch/lease.pdf"
_EXPIRY_SECONDS = 3_600


def _pdf_content() -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def _stage_args(**overrides: object) -> dict[str, Any]:
    args: dict[str, Any] = {
        "mode": TransferMode.PROVIDER_UPLOAD,
        "organisation_id": _ORGANISATION_ID,
        "logical_request_id": "req-openai-1",
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


def _fake_store(**overrides: Any) -> FakeOpenAIUploadStore:
    return FakeOpenAIUploadStore(region=_REGION, upload_expiry_seconds=_EXPIRY_SECONDS, **overrides)


def _file_object(*, file_id: str = "file-abc123", expires_at: int | None = None) -> dict[str, Any]:
    return {
        "id": file_id,
        "object": "file",
        "bytes": 1600,
        "created_at": int(datetime.now(UTC).timestamp()),
        "expires_at": expires_at or int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "filename": "attachment.pdf",
        "purpose": OPENAI_FILES_PURPOSE,
    }


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json_response(payload: dict[str, Any], *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://test"))


def _store(**overrides: Any) -> OpenAITransferStore:
    return OpenAITransferStore(
        api_key="sk-test",
        region=_REGION,
        upload_expiry_seconds=_EXPIRY_SECONDS,
        **overrides,
    )


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


def test_validate_openai_upload_accepts_reviewed_contract() -> None:
    validate_openai_upload(
        mode=TransferMode.PROVIDER_UPLOAD,
        mime_type="application/pdf",
        size_bytes=50_000_000,
        source_lifecycle=SourceLifecycle.TRANSIENT,
        region=_REGION,
        configured_region=_REGION,
        upload_expiry_seconds=_EXPIRY_SECONDS,
    )  # must not raise


@pytest.mark.parametrize(
    "updates",
    [
        {"mode": TransferMode.MANAGED_SIGNED_URL},
        {"mime_type": "image/png"},
        {"size_bytes": 50_000_001},
        {"source_lifecycle": SourceLifecycle.RETAINED},
        {"region": "eu"},
        {"upload_expiry_seconds": OPENAI_EXPIRES_AFTER_MIN_SECONDS - 1},
        {"upload_expiry_seconds": OPENAI_EXPIRES_AFTER_MAX_SECONDS + 1},
    ],
)
def test_validate_openai_upload_fails_closed(updates: dict[str, object]) -> None:
    args: dict[str, Any] = {
        "mode": TransferMode.PROVIDER_UPLOAD,
        "mime_type": "application/pdf",
        "size_bytes": 1600,
        "source_lifecycle": SourceLifecycle.TRANSIENT,
        "region": _REGION,
        "configured_region": _REGION,
        "upload_expiry_seconds": _EXPIRY_SECONDS,
    }
    args.update(updates)
    with pytest.raises(TransferStagingError):
        validate_openai_upload(**args)


# --- FakeOpenAIUploadStore: stage/reuse/expire/delete -------------------------


async def test_fake_stage_creates_user_data_reference_with_configured_expiry() -> None:
    store = _fake_store()
    reference = await store.stage(**_stage_args())
    assert reference.provider == "openai"
    assert reference.mode is TransferMode.PROVIDER_UPLOAD
    assert reference.region == _REGION
    assert reference.source_lifecycle is SourceLifecycle.TRANSIENT
    assert reference.external_id.startswith("file-fake-")
    assert "signed" not in reference.external_id.lower()
    assert reference.idempotency_key == derive_idempotency_key(
        provider="openai",
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-openai-1",
        source_digest="a" * 64,
        region=_REGION,
    )
    # The provider enforces the configured expires_after anchored at created_at
    # (Scope §2.4); the durable reference records the same wall-clock expiry.
    assert reference.expires_at is not None
    assert reference.expires_at - reference.created_at == timedelta(seconds=_EXPIRY_SECONDS)
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
        await store.stage(**_stage_args(region="us-central1"))
    assert store.uploads == []


async def test_fake_stage_rejects_non_pdf_oversized_and_retained_sources() -> None:
    store = _fake_store()
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(mime_type="image/png"))
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(size_bytes=50_000_001))
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(source_lifecycle=SourceLifecycle.RETAINED))
    assert store.uploads == []


async def test_fake_find_reusable_is_scoped_to_one_logical_request() -> None:
    store = _fake_store()
    reference = await store.stage(**_stage_args())
    hit = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-openai-1",
        source_digest="a" * 64,
        region=_REGION,
    )
    assert hit is not None and hit.external_id == reference.external_id
    miss = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-openai-2",
        source_digest="a" * 64,
        region=_REGION,
    )
    assert miss is None
    # A changed digest never reuses the earlier upload (Scope §5.4).
    digest_miss = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-openai-1",
        source_digest="b" * 64,
        region=_REGION,
    )
    assert digest_miss is None


async def test_fake_expired_reference_is_replaced_by_a_new_upload() -> None:
    store = _fake_store()
    first = await store.stage(**_stage_args())
    store.expire_due(now=datetime.now(UTC) + timedelta(hours=2))
    stale = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-openai-1",
        source_digest="a" * 64,
        region=_REGION,
    )
    assert stale is None
    replacement = await store.stage(**_stage_args())
    assert replacement.external_id != first.external_id
    assert len(store.uploads) == 2


async def test_fake_delete_removes_only_the_provider_copy() -> None:
    store = _fake_store()
    reference = await store.stage(**_stage_args())
    await store.delete(reference)
    assert store.deleted == [reference.external_id]
    assert reference.status is ExternalReferenceStatus.DELETED
    # A second delete is a no-op (best-effort idempotent terminal cleanup).
    await store.delete(reference)
    assert store.deleted == [reference.external_id]


# --- OpenAITransferStore: wire format, verification, error mapping ------------


def test_store_constructor_requires_key_and_reviewed_expiry() -> None:
    with pytest.raises(AIInputValidationError):
        OpenAITransferStore(api_key="", upload_expiry_seconds=_EXPIRY_SECONDS)
    with pytest.raises(AIInputValidationError):
        OpenAITransferStore(
            api_key="sk-test", upload_expiry_seconds=OPENAI_EXPIRES_AFTER_MIN_SECONDS - 1
        )


async def test_store_stage_uploads_multipart_with_purpose_and_expires_after(
    source_pdf: tuple[Path, bytes],
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_file_object())

    store = _store(client=_client(handler))
    reference = await store.stage(**_real_stage_args(source_pdf))
    sent = captured[0]
    assert sent.url == "https://api.openai.com/v1/files"
    assert sent.headers["authorization"] == "Bearer sk-test"
    # httpx encodes multipart fields; the plain fields carry purpose and the
    # expires_after JSON object (anchor + seconds) verified 2026-08-11.
    text = sent.read().decode("latin-1")
    assert 'name="purpose"' in text and OPENAI_FILES_PURPOSE in text
    expires_payload = json.loads(text.split('name="expires_after"')[1].split("\r\n--")[0].strip())
    assert expires_payload == {"anchor": "created_at", "seconds": _EXPIRY_SECONDS}
    assert reference.external_id == "file-abc123"
    assert reference.expires_at is not None
    _path, content = source_pdf
    assert reference.idempotency_key == derive_idempotency_key(
        provider="openai",
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-openai-1",
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
        return _json_response(_file_object(file_id=f"file-{calls}"))

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
        return _json_response({})

    store = _store(client=_client(handler))
    reference = await store.stage(**_real_stage_args(source_pdf))
    await store.delete(reference)
    assert deleted == ["/v1/files/file-abc123"]
    assert reference.status is ExternalReferenceStatus.DELETED
    # A second delete is a no-op; the provider never receives it again.
    await store.delete(reference)
    assert len(deleted) == 1
    await store.aclose()


async def test_store_delete_tolerates_already_gone_file(
    source_pdf: tuple[Path, bytes],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _json_response(_file_object())
        return _json_response({"error": {"message": "not found"}}, status=404)

    store = _store(client=_client(handler))
    reference = await store.stage(**_real_stage_args(source_pdf))
    await store.delete(reference)  # must not raise
    assert reference.status is ExternalReferenceStatus.DELETED
    await store.aclose()


async def test_store_delete_failure_propagates_for_reconciliation(
    source_pdf: tuple[Path, bytes],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _json_response(_file_object())
        return _json_response({}, status=503)

    store = _store(client=_client(handler))
    reference = await store.stage(**_real_stage_args(source_pdf))
    with pytest.raises(ProviderUnavailableError):
        await store.delete(reference)
    # The row stays live so the §6.7 reconciliation job can cover the orphan.
    assert reference.status is ExternalReferenceStatus.LIVE
    await store.aclose()


async def test_store_find_reusable_scoped_to_one_logical_request(
    source_pdf: tuple[Path, bytes],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(_file_object(file_id=f"file-{calls}"))

    store = _store(client=_client(handler))
    _path, content = source_pdf
    digest = hashlib.sha256(content).hexdigest()
    first = await store.stage(**_real_stage_args(source_pdf))
    hit = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-openai-1",
        source_digest=digest,
        region=_REGION,
    )
    assert hit is not None and hit.external_id == first.external_id
    assert calls == 1  # reuse never re-uploads
    miss = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-openai-9",
        source_digest=digest,
        region=_REGION,
    )
    assert miss is None
    await store.aclose()


async def test_store_parse_failure_is_a_permanent_response_error(
    source_pdf: tuple[Path, bytes],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not json", request=httpx.Request("POST", "http://test")
        )

    store = _store(client=_client(handler))
    with pytest.raises(ProviderResponseError):
        await store.stage(**_real_stage_args(source_pdf))
    await store.aclose()
