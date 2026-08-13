"""Fake-backed Vertex GCS staging tests (v0.8 Scope §2.4, §6.4 checkbox 4).

The Vertex large-file path stages a verified source into a user-provisioned,
non-public, same-region GCS staging bucket and references it as ``gs://``
``fileData``. These tests exercise the whole contract hermetically through the
provider-neutral rules and the deterministic :class:`FakeGcsStagingStore` —
bucket validation (multi-region, cross-region, foreign project, public access,
name mismatch) failing closed before any upload, idempotent stage/reference/
use, retry-only reuse, expiry and best-effort deletion that never touches the
feature-owned source — plus the Vertex adapter's ``fileData`` dispatch form.
The real ``GcsTransferStore`` shares the exact same validation rules, so the
fake and the adapter cannot drift; live Google behavior is covered by the
opt-in ``ai_contracts`` test in ``test_ai_contracts.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from app.ai.attachments import Attachment
from app.ai.errors import AIInputValidationError, TransferStagingError
from app.ai.providers.base import ProviderRequest
from app.ai.providers.vertex import VertexAIAdapter
from app.ai.providers.vertex_gcs import GcsTransferStore
from app.ai.staging import ExternalFileReference, ExternalReferenceStatus, StagedFile
from app.ai.transfer import SourceLifecycle, TransferMode, derive_idempotency_key
from app.ai.vertex_staging import (
    VERTEX_STAGING_MAX_BYTES,
    VERTEX_STAGING_PREFIX_TEMPLATE,
    FakeGcsStagingStore,
    GcsBucketLocationType,
    StagedObjectMetadata,
    StagingBucketMetadata,
    parse_gs_uri,
    validate_gcs_bucket_name,
    validate_vertex_staged_object,
    validate_vertex_staging_bucket,
    vertex_staging_object_key,
)
from app.storage.fake import FakeObjectStorage

_ORGANISATION_ID = uuid.uuid4()
_PROJECT = "fixture-project"
_LOCATION = "europe-west1"
_BUCKET = "fixture-ai-staging"

_SOURCE_KEY = f"organisations/{_ORGANISATION_ID}/documents/lease.pdf"


def _pdf_content() -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def _stage_args(**overrides: object) -> dict[str, Any]:
    args: dict[str, Any] = {
        "mode": TransferMode.STORAGE_REFERENCE,
        "organisation_id": _ORGANISATION_ID,
        "logical_request_id": "req-vertex-1",
        "source_reference": _SOURCE_KEY,
        "source_digest": "a" * 64,
        "mime_type": "application/pdf",
        "size_bytes": 1600,
        "source_lifecycle": SourceLifecycle.RETAINED,
        "region": _LOCATION,
        "expires_at": None,
    }
    args.update(overrides)
    return args


def _store(**overrides: Any) -> FakeGcsStagingStore:
    return FakeGcsStagingStore(
        bucket=_BUCKET,
        project=_PROJECT,
        location=_LOCATION,
        **overrides,
    )


def _valid_bucket_metadata() -> StagingBucketMetadata:
    return StagingBucketMetadata(name=_BUCKET, project=_PROJECT, location=_LOCATION)


def _staged_metadata(**overrides: object) -> StagedObjectMetadata:
    metadata = StagedObjectMetadata(
        name=f"{_BUCKET}/obj", size_bytes=1600, content_type="application/pdf"
    )
    return metadata.model_copy(update=overrides)


# --- GCS bucket-name and gs:// reference rules -------------------------------


@pytest.mark.parametrize(
    "name",
    ["valid-bucket", "bucket1", "a.b-c_d", "x" * 63, "staging-2026"],
)
def test_valid_gcs_bucket_names(name: str) -> None:
    validate_gcs_bucket_name(name)  # must not raise


@pytest.mark.parametrize(
    "name",
    [
        "",
        "ab",
        "x" * 64,
        "UPPER-case",
        "has space",
        "-leading",
        "trailing-",
        ".leading-dot",
        "googlereserved",
        "goog-prefix",
        "mygooglebucket",
        "192.168.5.4",
        "1.2.3.4",
        "xn--punycode",
    ],
)
def test_invalid_gcs_bucket_names_fail_closed(name: str) -> None:
    with pytest.raises(TransferStagingError):
        validate_gcs_bucket_name(name)


def test_parse_gs_uri_accepts_private_reference() -> None:
    assert parse_gs_uri(f"gs://{_BUCKET}/organisations/x/ai/vertex-staging/a.pdf") == (
        _BUCKET,
        "organisations/x/ai/vertex-staging/a.pdf",
    )


@pytest.mark.parametrize(
    "uri", ["https://example.com/x.pdf", "s3://bucket/obj", f"gs://{_BUCKET}", "gs:///obj"]
)
def test_parse_gs_uri_rejects_other_reference_forms(uri: str) -> None:
    with pytest.raises(TransferStagingError):
        parse_gs_uri(uri)


def test_vertex_staging_object_key_is_org_scoped_and_deterministic() -> None:
    key = vertex_staging_object_key(
        organisation_id=_ORGANISATION_ID, logical_request_id="req-1", source_digest="b" * 64
    )
    assert key.startswith(VERTEX_STAGING_PREFIX_TEMPLATE.format(organisation_id=_ORGANISATION_ID))
    assert key.endswith(".pdf")
    assert "req-1" in key and ("b" * 32) in key
    again = vertex_staging_object_key(
        organisation_id=_ORGANISATION_ID, logical_request_id="req-1", source_digest="b" * 64
    )
    assert again == key
    other_digest = vertex_staging_object_key(
        organisation_id=_ORGANISATION_ID, logical_request_id="req-1", source_digest="c" * 64
    )
    assert other_digest != key


# --- Fail-closed bucket validation (Scope §2.4, §5.7) -------------------------


def test_bucket_validation_accepts_private_same_region_same_project() -> None:
    validate_vertex_staging_bucket(
        bucket_name=_BUCKET,
        metadata=_valid_bucket_metadata(),
        configured_project=_PROJECT,
        configured_location=_LOCATION,
    )  # must not raise


def test_bucket_validation_fails_closed_multi_region() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staging_bucket(
            bucket_name=_BUCKET,
            metadata=_valid_bucket_metadata().model_copy(
                update={"location_type": GcsBucketLocationType.MULTI_REGION}
            ),
            configured_project=_PROJECT,
            configured_location=_LOCATION,
        )


def test_bucket_validation_fails_closed_dual_region() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staging_bucket(
            bucket_name=_BUCKET,
            metadata=_valid_bucket_metadata().model_copy(
                update={"location_type": GcsBucketLocationType.DUAL_REGION}
            ),
            configured_project=_PROJECT,
            configured_location=_LOCATION,
        )


def test_bucket_validation_fails_closed_cross_region() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staging_bucket(
            bucket_name=_BUCKET,
            metadata=_valid_bucket_metadata().model_copy(update={"location": "us-central1"}),
            configured_project=_PROJECT,
            configured_location=_LOCATION,
        )


def test_bucket_validation_fails_closed_foreign_project() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staging_bucket(
            bucket_name=_BUCKET,
            metadata=_valid_bucket_metadata().model_copy(update={"project": "other-project"}),
            configured_project=_PROJECT,
            configured_location=_LOCATION,
        )


def test_bucket_validation_fails_closed_public_access() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staging_bucket(
            bucket_name=_BUCKET,
            metadata=_valid_bucket_metadata().model_copy(update={"has_public_read": True}),
            configured_project=_PROJECT,
            configured_location=_LOCATION,
        )


def test_bucket_validation_fails_closed_without_uniform_access() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staging_bucket(
            bucket_name=_BUCKET,
            metadata=_valid_bucket_metadata().model_copy(
                update={"uniform_bucket_level_access": False}
            ),
            configured_project=_PROJECT,
            configured_location=_LOCATION,
        )


def test_bucket_validation_fails_closed_on_name_mismatch() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staging_bucket(
            bucket_name=_BUCKET,
            metadata=_valid_bucket_metadata().model_copy(update={"name": "other-bucket"}),
            configured_project=_PROJECT,
            configured_location=_LOCATION,
        )


# --- Staged-object validation (Scope §2.4 checkbox 2) -------------------------


def test_staged_object_validation_accepts_matching_copy() -> None:
    validate_vertex_staged_object(
        metadata=_staged_metadata(), expected_size=1600, expected_mime="application/pdf"
    )  # must not raise


def test_staged_object_validation_fails_closed_on_size_mismatch() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staged_object(
            metadata=_staged_metadata(), expected_size=2000, expected_mime="application/pdf"
        )


def test_staged_object_validation_fails_closed_on_mime_mismatch() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staged_object(
            metadata=_staged_metadata(),
            expected_size=1600,
            expected_mime="application/json",
        )


def test_staged_object_validation_fails_closed_on_digest_mismatch() -> None:
    with pytest.raises(TransferStagingError):
        validate_vertex_staged_object(
            metadata=_staged_metadata(md5_hash="AAAA"),
            expected_size=1600,
            expected_mime="application/pdf",
            expected_md5_b64="BBBB",
        )


# --- FakeGcsStagingStore: stage/reuse/expire/delete ---------------------------


async def test_fake_stage_creates_private_gs_reference_under_approved_prefix() -> None:
    store = _store()
    reference = await store.stage(**_stage_args())
    assert reference.provider == "vertex"
    assert reference.mode is TransferMode.STORAGE_REFERENCE
    assert reference.region == _LOCATION
    assert reference.source_lifecycle is SourceLifecycle.RETAINED
    bucket, object_key = parse_gs_uri(reference.external_id)
    assert bucket == _BUCKET
    assert object_key.startswith(
        VERTEX_STAGING_PREFIX_TEMPLATE.format(organisation_id=_ORGANISATION_ID)
    )
    assert reference.external_id.startswith("gs://")
    assert "signed" not in reference.external_id.lower()
    assert reference.idempotency_key == derive_idempotency_key(
        provider="vertex",
        mode=TransferMode.STORAGE_REFERENCE,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-vertex-1",
        source_digest="a" * 64,
        region=_LOCATION,
    )
    assert store.uploads == [object_key]


async def test_fake_stage_is_idempotent_per_logical_transfer() -> None:
    store = _store()
    first = await store.stage(**_stage_args())
    second = await store.stage(**_stage_args())
    assert second.external_id == first.external_id
    assert second.idempotency_key == first.idempotency_key
    # One upload, one staged copy, one durable reference.
    assert len(store.uploads) == 1
    assert len(store.staged_objects) == 1


async def test_fake_stage_digest_change_creates_a_new_transfer() -> None:
    store = _store()
    first = await store.stage(**_stage_args(source_digest="a" * 64))
    changed = await store.stage(**_stage_args(source_digest="b" * 64))
    assert changed.idempotency_key != first.idempotency_key
    assert changed.external_id != first.external_id
    assert len(store.uploads) == 2


async def test_fake_stage_refuses_non_storage_reference_mode() -> None:
    store = _store()
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(mode=TransferMode.PROVIDER_UPLOAD))
    assert store.uploads == []


async def test_fake_stage_fails_closed_on_region_mismatch() -> None:
    store = _store()
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(region="us-central1"))
    assert store.uploads == []


async def test_fake_stage_rejects_non_pdf_and_oversized_objects() -> None:
    store = _store()
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(mime_type="image/png"))
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args(size_bytes=VERTEX_STAGING_MAX_BYTES + 1))
    assert store.uploads == []


@pytest.mark.parametrize(
    "updates",
    [
        {"location_type": GcsBucketLocationType.MULTI_REGION},
        {"location": "us-central1"},
        {"project": "other-project"},
        {"has_public_read": True},
        {"uniform_bucket_level_access": False},
    ],
)
async def test_fake_stage_fails_closed_on_invalid_bucket_before_upload(
    updates: dict[str, object],
) -> None:
    store = _store()
    store.set_bucket_metadata(**updates)
    with pytest.raises(TransferStagingError):
        await store.stage(**_stage_args())
    # The unsafe bucket never receives a copy: nothing was uploaded or staged.
    assert store.uploads == []
    assert store.staged_objects == []


async def test_fake_find_reusable_is_scoped_to_one_logical_request() -> None:
    store = _store()
    reference = await store.stage(**_stage_args())
    hit = await store.find_reusable(
        mode=TransferMode.STORAGE_REFERENCE,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-vertex-1",
        source_digest="a" * 64,
        region=_LOCATION,
    )
    assert hit is not None and hit.external_id == reference.external_id
    miss = await store.find_reusable(
        mode=TransferMode.STORAGE_REFERENCE,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-vertex-2",
        source_digest="a" * 64,
        region=_LOCATION,
    )
    assert miss is None


async def test_fake_delete_removes_staged_copy_and_never_the_source() -> None:
    store = _store()
    source = FakeObjectStorage(bucket="feature-bucket")
    await source.put(_SOURCE_KEY, _pdf_content(), content_type="application/pdf")
    reference = await store.stage(**_stage_args())
    await store.delete(reference)
    assert reference.status is ExternalReferenceStatus.DELETED
    assert reference.deleted_at is not None
    assert store.staged_objects == []
    assert len(store.deleted) == 1
    # The feature-owned source object is untouched (Scope §2.5).
    head = await source.head_object(_SOURCE_KEY)
    assert head is not None and head.size_bytes == len(_pdf_content())


async def test_fake_delete_is_best_effort_and_idempotent() -> None:
    store = _store()
    reference = await store.stage(**_stage_args())
    await store.delete(reference)
    await store.delete(reference)  # second delete is a no-op
    assert len(store.deleted) == 1
    assert reference.status is ExternalReferenceStatus.DELETED


async def test_fake_delete_ignores_unknown_references_and_keeps_staged_objects() -> None:
    store = _store()
    reference = await store.stage(**_stage_args())
    # A reference this store never staged (unknown key, different bucket) is a
    # no-op: deletion is authoritative over this store's own records and can
    # never remove an object from a bucket it does not stage into.
    foreign = ExternalFileReference.model_validate(
        reference.model_dump(exclude={"is_live"})
        | {"external_id": f"gs://{_BUCKET}-other/obj.pdf", "idempotency_key": "unknown-key"}
    )
    await store.delete(foreign)
    assert len(store.staged_objects) == 1
    assert store.deleted == []
    assert reference.status is ExternalReferenceStatus.LIVE


async def test_fake_delete_fails_closed_on_foreign_bucket_staged_object() -> None:
    store = _store()
    reference = await store.stage(**_stage_args())
    # A staged record can never name a foreign bucket, but defense-in-depth:
    # deleting a record whose gs:// points elsewhere fails closed and leaves
    # both the record and the object in place.
    record = reference.model_copy(update={"external_id": "gs://foreign-bucket/obj.pdf"})
    store._records[reference.idempotency_key] = record  # type: ignore[attr-defined]
    with pytest.raises(TransferStagingError):
        await store.delete(reference)
    assert len(store.staged_objects) == 1
    assert store.deleted == []


async def test_fake_expiry_makes_reference_unreusable_and_replaced() -> None:
    store = _store()
    now = datetime.now(UTC)
    past = now - timedelta(minutes=1)
    reference = await store.stage(**_stage_args(expires_at=past))
    assert store.expire_due(now=now) == 1
    assert reference.status is ExternalReferenceStatus.EXPIRED
    hit = await store.find_reusable(
        mode=TransferMode.STORAGE_REFERENCE,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-vertex-1",
        source_digest="a" * 64,
        region=_LOCATION,
    )
    assert hit is None
    # A retry after expiry stages a fresh live reference. The staged object
    # itself still exists in the bucket (the deployer-owned lifecycle removes
    # it, Scope §2.5), so the idempotent stage reuses the object without a new
    # upload.
    replacement = await store.stage(**_stage_args(expires_at=past))
    assert replacement.status is ExternalReferenceStatus.LIVE
    assert replacement.idempotency_key == reference.idempotency_key
    assert len(store.uploads) == 1


# --- Vertex adapter fileData dispatch form (Scope §2.4) ------------------------


class _FakeCredentials:
    token = "test-vertex-token"
    valid = True


def _fake_google_auth(scopes: Any = None) -> tuple[Any, None]:
    return _FakeCredentials(), None


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json_response(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=data)


def _vertex_response() -> httpx.Response:
    return _json_response(
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"category": "lease"}'}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 3},
        }
    )


async def test_vertex_adapter_builds_filedata_for_staged_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.ai.providers._google_credentials.google_auth_default",
        _fake_google_auth,
    )
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _vertex_response()

    adapter = VertexAIAdapter(
        project=_PROJECT,
        location=_LOCATION,
        client=_client(handler),
    )
    staged_uri = f"gs://{_BUCKET}/organisations/{_ORGANISATION_ID}/ai/vertex-staging/req-1/a.pdf"
    request = ProviderRequest(
        task="document.classify",
        model="gemini-2.0-flash",
        prompt="Classify.",
        staged_file=StagedFile(external_id=staged_uri, mime_type="application/pdf"),
    )
    await adapter.complete(request)
    payload = json.loads(captured[0].content)
    parts = payload.get("contents", [{}])[0].get("parts", [])
    file_parts = [part for part in parts if "fileData" in part]
    assert file_parts == [{"fileData": {"fileUri": staged_uri, "mimeType": "application/pdf"}}]
    inline_parts = [part for part in parts if "inlineData" in part]
    assert inline_parts == []
    await adapter.aclose()


async def test_vertex_adapter_rejects_non_gs_staged_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.ai.providers._google_credentials.google_auth_default",
        _fake_google_auth,
    )
    adapter = VertexAIAdapter(
        project=_PROJECT, location=_LOCATION, client=_client(_vertex_response)
    )
    request = ProviderRequest(
        task="document.classify",
        model="gemini-2.0-flash",
        prompt="Classify.",
        staged_file=StagedFile(
            external_id="https://example.com/x.pdf", mime_type="application/pdf"
        ),
    )
    with pytest.raises(AIInputValidationError):
        await adapter.complete(request)
    await adapter.aclose()


async def test_vertex_adapter_rejects_staged_file_combined_with_inline_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.ai.providers._google_credentials.google_auth_default",
        _fake_google_auth,
    )
    adapter = VertexAIAdapter(
        project=_PROJECT, location=_LOCATION, client=_client(_vertex_response)
    )
    request = ProviderRequest(
        task="document.classify",
        model="gemini-2.0-flash",
        prompt="Classify.",
        staged_file=StagedFile(external_id=f"gs://{_BUCKET}/a.pdf", mime_type="application/pdf"),
        attachments=[
            Attachment(display_name="a.pdf", mime_type="application/pdf", content=b"%PDF-1.7")
        ],
    )
    with pytest.raises(AIInputValidationError):
        await adapter.complete(request)
    await adapter.aclose()


# --- Real GcsTransferStore hermetic HTTP-mocked tests (Scope §6.4) ----------
#
# The fake exercises the provider-neutral rules; these tests drive the real
# ``GcsTransferStore`` over an in-memory GCS JSON API (httpx MockTransport) so
# the adapter's bucket/IAM validation, bounded upload + head digest
# verification, object-reuse integrity and best-effort deletion are covered
# without live Google credentials.


def _bucket_json() -> dict[str, Any]:
    return {
        "name": _BUCKET,
        "location": _LOCATION,
        "locationType": "SINGLE_REGION",
        "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}},
        "versioning": {"enabled": False},
    }


def _iam_json(members: list[str] | None = None) -> dict[str, Any]:
    if members:
        return {"bindings": [{"role": "roles/storage.objectViewer", "members": members}]}
    return {"bindings": []}


def _source_file(tmp_path: Path, content: bytes | None = None) -> tuple[Path, str, str, int]:
    """Write a verified PDF temp file; return ``(path, sha256_hex, md5_b64, size)``."""
    data = content if content is not None else _pdf_content()
    path = tmp_path / "lease.pdf"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
    return path, digest, md5, len(data)


class _GcsServer:
    """Minimal in-memory GCS JSON API for hermetic ``GcsTransferStore`` tests."""

    def __init__(self) -> None:
        self.bucket: dict[str, Any] = _bucket_json()
        self.iam: dict[str, Any] = _iam_json()
        self.bucket_status: int = 200
        self.upload_status: int = 200
        #: md5/size reported for an object this upload creates.
        self.uploaded_md5: str = ""
        self.uploaded_size: int = 0
        #: objects present in the bucket, keyed by the decoded object name.
        self.objects: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def _object_name(self, path: str) -> str:
        marker = f"/b/{_BUCKET}/o/"
        index = path.find(marker)
        if index < 0:
            return ""
        return unquote(path[index + len(marker) :])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append(f"{method} {path}")
        if path.endswith(f"/projects/{_PROJECT}/buckets/{_BUCKET}"):
            if self.bucket_status >= 400:
                return httpx.Response(self.bucket_status)
            return httpx.Response(200, json=self.bucket)
        if path.endswith(f"/b/{_BUCKET}/iam"):
            return httpx.Response(200, json=self.iam)
        if method == "POST" and "/upload/" in path:
            if self.upload_status >= 400:
                return httpx.Response(self.upload_status)
            name = request.url.params.get("name", "")
            body = {
                "name": name,
                "size": str(self.uploaded_size),
                "contentType": "application/pdf",
                "md5Hash": self.uploaded_md5,
            }
            self.objects[name] = body
            return httpx.Response(200, json=body)
        if method == "GET" and f"/b/{_BUCKET}/o/" in path:
            body = self.objects.get(self._object_name(path))
            if body is None:
                return httpx.Response(404)
            return httpx.Response(200, json=body)
        if method == "DELETE" and f"/b/{_BUCKET}/o/" in path:
            self.objects.pop(self._object_name(path), None)
            return httpx.Response(204)
        return httpx.Response(404)


def _gcs_store(monkeypatch: pytest.MonkeyPatch, server: _GcsServer) -> GcsTransferStore:
    monkeypatch.setattr(
        "app.ai.providers._google_credentials.google_auth_default",
        _fake_google_auth,
    )
    return GcsTransferStore(
        project=_PROJECT,
        location=_LOCATION,
        bucket=_BUCKET,
        client=_client(server),
    )


def _object_key_for(digest: str) -> str:
    return vertex_staging_object_key(
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-vertex-1",
        source_digest=digest,
    )


async def test_gcs_store_uploads_and_creates_gs_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_path, digest, md5_b64, size = _source_file(tmp_path)
    object_key = _object_key_for(digest)
    server = _GcsServer()
    server.uploaded_md5 = md5_b64
    server.uploaded_size = size
    store = _gcs_store(monkeypatch, server)

    reference = await store.stage(
        **_stage_args(source_digest=digest, size_bytes=size, source_path=source_path)
    )

    assert object_key in server.objects
    assert server.objects[object_key]["md5Hash"] == md5_b64
    assert reference.external_id == f"gs://{_BUCKET}/{object_key}"
    assert reference.source_digest == digest
    assert reference.region == _LOCATION
    await store.aclose()


async def test_gcs_store_fails_closed_before_upload_on_public_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_path, digest, _md5_b64, size = _source_file(tmp_path)
    server = _GcsServer()
    server.iam = _iam_json(["allUsers"])  # public read binding
    store = _gcs_store(monkeypatch, server)

    with pytest.raises(TransferStagingError):
        await store.stage(
            **_stage_args(source_digest=digest, size_bytes=size, source_path=source_path)
        )

    # No object was ever uploaded to the unsafe bucket.
    assert all("POST" not in call for call in server.calls)
    assert server.objects == {}
    await store.aclose()


async def test_gcs_store_upload_rejects_mismatched_sha256(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_path, _digest, md5_b64, size = _source_file(tmp_path)
    server = _GcsServer()
    server.uploaded_md5 = md5_b64
    server.uploaded_size = size
    store = _gcs_store(monkeypatch, server)

    with pytest.raises(TransferStagingError):
        await store.stage(
            **_stage_args(
                source_digest="0" * 64,  # does not match the verified source bytes
                size_bytes=size,
                source_path=source_path,
            )
        )

    await store.aclose()


async def test_gcs_store_reuse_rejects_mismatched_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An existing same-size/same-MIME object with a wrong digest is not reused.

    Regression test for the v0.8 §6.4 must-fix: the reuse path must re-derive
    the verified source's MD5 and require the staged object's server-reported
    ``md5Hash`` to match before a ``gs://`` reference is created.
    """
    source_path, digest, _matching_md5, size = _source_file(tmp_path)
    object_key = _object_key_for(digest)
    server = _GcsServer()
    # A corrupted/replaced object: right size and MIME, wrong digest.
    server.objects[object_key] = {
        "name": object_key,
        "size": str(size),
        "contentType": "application/pdf",
        "md5Hash": "AAAAAAAAAAAAAAAAAAAAAA==",
    }
    store = _gcs_store(monkeypatch, server)

    with pytest.raises(TransferStagingError):
        await store.stage(
            **_stage_args(source_digest=digest, size_bytes=size, source_path=source_path)
        )

    # The mismatched object was never re-uploaded over.
    assert all("POST" not in call for call in server.calls)
    await store.aclose()


async def test_gcs_store_reuse_accepts_matching_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_path, digest, md5_b64, size = _source_file(tmp_path)
    object_key = _object_key_for(digest)
    server = _GcsServer()
    server.objects[object_key] = {
        "name": object_key,
        "size": str(size),
        "contentType": "application/pdf",
        "md5Hash": md5_b64,
    }
    store = _gcs_store(monkeypatch, server)

    reference = await store.stage(
        **_stage_args(source_digest=digest, size_bytes=size, source_path=source_path)
    )

    # Existing object reused — no upload performed.
    assert all("POST" not in call for call in server.calls)
    assert reference.external_id == f"gs://{_BUCKET}/{object_key}"
    await store.aclose()


async def test_gcs_store_deletes_staged_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_path, digest, md5_b64, size = _source_file(tmp_path)
    object_key = _object_key_for(digest)
    server = _GcsServer()
    server.uploaded_md5 = md5_b64
    server.uploaded_size = size
    store = _gcs_store(monkeypatch, server)

    reference = await store.stage(
        **_stage_args(source_digest=digest, size_bytes=size, source_path=source_path)
    )
    assert object_key in server.objects

    await store.delete(reference)

    assert object_key not in server.objects
    assert reference.status is ExternalReferenceStatus.DELETED
    await store.aclose()
