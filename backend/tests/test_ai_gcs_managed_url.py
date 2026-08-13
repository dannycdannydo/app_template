"""Dev managed-URL staging tests (v0.8 Scope §2.3, §6.4/§6.5).

The dev seam for a local storage seam (plain-HTTP MinIO): a retained source is
re-verified, staged into the private GCS temp bucket through the real
``GcsTransferStore`` and exposed as a GCS v4 RSA-signed HTTPS URL. These tests
exercise the wiring hermetically: the GCS JSON/upload API is answered by a
deterministic in-test transport (the same endpoint shapes the real store uses,
whose live behavior is covered by the opt-in ``ai_contracts`` suite), and the
signer is a freshly generated RSA service-account key, so no Google
credentials or network are needed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.oauth2 import service_account  # pyright: ignore[reportUnknownVariableType]

from app.ai.errors import AIInputValidationError, TransferSourceError
from app.ai.providers import gcs_managed_url
from app.ai.providers.gcs_managed_url import GcsManagedUrlStager, mint_gcs_v4_signed_url
from app.ai.staging import ExternalFileReference
from app.ai.transfer import SourceLifecycle, TransferMode
from app.storage.fake import FakeObjectStorage

_ORGANISATION_ID = uuid.uuid4()
_PROJECT = "fixture-project"
_PROJECT_NUMBER = "123456789012"
_LOCATION = "europe-west1"
_BUCKET = "fixture-ai-staging"
_SOURCE_KEY = f"organisations/{_ORGANISATION_ID}/documents/lease.pdf"
_TTL_SECONDS = 900


def _pdf_content() -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def _service_account_info(private_key: Any, *, client_email: str) -> dict[str, str]:
    return {
        "type": "service_account",
        "project_id": _PROJECT,
        "private_key_id": "k" * 40,
        "private_key": private_key,
        "client_email": client_email,
        "client_id": "123456789012345678901",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def _reference(digest: str, size_bytes: int) -> ExternalFileReference:
    return ExternalFileReference(
        mode=TransferMode.MANAGED_SIGNED_URL,
        provider="fake",
        external_id=_SOURCE_KEY,
        source_reference=_SOURCE_KEY,
        source_digest=digest,
        size_bytes=size_bytes,
        mime_type="application/pdf",
        source_lifecycle=SourceLifecycle.RETAINED,
        region=_LOCATION,
        organisation_id=_ORGANISATION_ID,
        logical_request_id="req-managed-url-1",
        idempotency_key="idem-key",
        created_at=datetime.now(UTC),
    )


class _GcsTransport(httpx.AsyncBaseTransport):
    """Deterministic GCS JSON/upload API for the staging calls the store makes."""

    def __init__(self, *, pdf: bytes) -> None:
        self._pdf = pdf
        # GCS serves the md5 of the uploaded bytes. httpx pre-buffers the
        # request body before handing the request to a custom transport, so
        # the transport cannot re-read the stream — the staged content is the
        # verified source by construction and the store independently checks
        # the incremental SHA-256 of the bytes it actually sent.
        self._staged_md5 = (
            __import__("base64")
            .b64encode(__import__("hashlib").md5(self._pdf).digest())
            .decode("ascii")
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/storage/v1/projects/{_PROJECT}/serviceAccount":
            return httpx.Response(
                200,
                json={
                    "email_address": (
                        f"service-{_PROJECT_NUMBER}@gcp-sa-storage.iam.gserviceaccount.com"
                    )
                },
                request=request,
            )
        if path == f"/storage/v1/b/{_BUCKET}/iam":
            return httpx.Response(200, json={"bindings": []}, request=request)
        if path == f"/storage/v1/b/{_BUCKET}":
            return httpx.Response(
                200,
                json={
                    "name": _BUCKET,
                    "projectNumber": _PROJECT_NUMBER,
                    "location": _LOCATION.upper(),
                    "locationType": "region",
                    "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}},
                    "versioning": {"enabled": False},
                },
                request=request,
            )
        if path.startswith(f"/upload/storage/v1/b/{_BUCKET}/o"):
            return httpx.Response(
                200,
                json={"name": unquote(str(request.url.params.get("name") or ""))},
                request=request,
            )
        if path.startswith(f"/storage/v1/b/{_BUCKET}/o/"):
            return httpx.Response(
                200,
                json={
                    "name": path.split(f"/storage/v1/b/{_BUCKET}/o/", 1)[1],
                    "size": str(len(self._pdf)),
                    "contentType": "application/pdf",
                    "md5Hash": self._staged_md5,
                },
                request=request,
            )
        return httpx.Response(
            404, text=f"unexpected: {request.method} {request.url}", request=request
        )


@pytest.fixture
def gcs_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A generated service-account key file + the auth-header stub."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    info = _service_account_info(
        pem, client_email="fixture@fixture-project.iam.gserviceaccount.com"
    )
    key_path = tmp_path / "sakey.json"
    key_path.write_text(json.dumps(info), encoding="utf-8")
    # The store refreshes a Bearer token through the network; the auth header
    # seam is unit-covered elsewhere, so the transport test stubs it.

    def _auth_header(_credentials: object) -> str:
        return "Bearer test"

    monkeypatch.setattr(
        "app.ai.providers.vertex_gcs.google_authorization_header",
        _auth_header,
    )
    return key_path, info["client_email"]


def _stager(key_path: Path, transport: _GcsTransport) -> GcsManagedUrlStager:
    client = httpx.AsyncClient(transport=transport)
    return GcsManagedUrlStager(
        project=_PROJECT,
        location=_LOCATION,
        bucket=_BUCKET,
        credentials_path=str(key_path),
        timeout_seconds=10,
        client=client,
    )


async def test_stager_mints_gcs_https_url_for_a_retained_source(
    gcs_setup: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path, _ = gcs_setup
    pdf = _pdf_content()
    storage = FakeObjectStorage(bucket="local-source")
    await storage.put(_SOURCE_KEY, pdf, content_type="application/pdf")
    reference = _reference(digest=hashlib.sha256(pdf).hexdigest(), size_bytes=len(pdf))

    stager = _stager(key_path, _GcsTransport(pdf=pdf))
    signed = await stager.mint(
        reference=reference, ttl_seconds=_TTL_SECONDS, source_storage=storage
    )

    assert signed.method == "GET"
    assert signed.expires_at is not None
    url = signed.url
    assert url.startswith("https://storage.googleapis.com/")
    parsed = urlparse(url)
    # The staged copy lives under the approved org-scoped Vertex staging
    # prefix, never in a feature-owned namespace (Scope §2.4); the canonical
    # URI preserves the object's path separators.
    assert f"/{_BUCKET}/organisations/{_ORGANISATION_ID}/ai/vertex-staging/" in parsed.path
    assert "documents/lease.pdf" not in parsed.path
    query = parse_qs(parsed.query)
    assert query["X-Goog-Algorithm"] == ["GOOG4-RSA-SHA256"]
    assert query["X-Goog-Date"][0].startswith("20")
    assert query["X-Goog-Expires"] == [str(_TTL_SECONDS)]
    assert query["X-Goog-SignedHeaders"] == ["host"]
    assert "X-Goog-Signature" in query
    assert "X-Goog-Credential" in query
    # The bearer material is in the query string only, never logged by the
    # stager; the source object is untouched.
    assert await storage.head_object(_SOURCE_KEY) is not None


async def test_stager_rejects_a_changed_source(
    gcs_setup: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An object replaced with different bytes of the same length never gets a URL."""
    key_path, _ = gcs_setup
    original = _pdf_content()
    replacement = b"%PDF-1.7\n" + b"z" * (len(original) - len(b"%PDF-1.7\n"))
    storage = FakeObjectStorage(bucket="local-source")
    await storage.put(_SOURCE_KEY, replacement, content_type="application/pdf")
    # The durable reference verified the ORIGINAL bytes.
    reference = _reference(digest=hashlib.sha256(original).hexdigest(), size_bytes=len(original))

    stager = _stager(key_path, _GcsTransport(pdf=original))
    with pytest.raises(TransferSourceError, match="changed since it was verified"):
        await stager.mint(reference=reference, ttl_seconds=_TTL_SECONDS, source_storage=storage)


async def test_stager_rejects_a_non_managed_reference(
    gcs_setup: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path, _ = gcs_setup
    pdf = _pdf_content()
    storage = FakeObjectStorage(bucket="local-source")
    await storage.put(_SOURCE_KEY, pdf, content_type="application/pdf")
    reference = _reference(digest=hashlib.sha256(pdf).hexdigest(), size_bytes=len(pdf))
    reference = reference.model_copy(
        update={
            "mode": TransferMode.STORAGE_REFERENCE,
            "source_lifecycle": SourceLifecycle.TRANSIENT,
        }
    )

    stager = _stager(key_path, _GcsTransport(pdf=pdf))
    with pytest.raises(TransferSourceError, match="managed-signed-url mode"):
        await stager.mint(reference=reference, ttl_seconds=_TTL_SECONDS, source_storage=storage)


def test_stager_requires_a_signer(
    gcs_setup: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credentials without a private-key signer cannot sign v4 URLs: fail fast
    at wiring rather than at dispatch."""
    key_path, _ = gcs_setup

    class _NoSignerCredentials:
        signer = None
        signer_email = "fixture@fixture-project.iam.gserviceaccount.com"

    def _no_signer(_path: str) -> _NoSignerCredentials:
        return _NoSignerCredentials()

    monkeypatch.setattr(gcs_managed_url, "load_google_credentials", _no_signer)
    with pytest.raises(AIInputValidationError, match="service-account key"):
        GcsManagedUrlStager(
            project=_PROJECT,
            location=_LOCATION,
            bucket=_BUCKET,
            credentials_path=str(key_path),
            timeout_seconds=10,
        )


def test_gcs_v4_signed_url_structure_and_tll_bounds(
    gcs_setup: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v4 canonical-request/signature construction is deterministic and the
    TTL is bounded to the reviewed window."""
    key_path, _ = gcs_setup
    credentials = service_account.Credentials.from_service_account_file(  # pyright: ignore[reportUnknownMemberType]
        str(key_path), scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    url = mint_gcs_v4_signed_url(
        credentials=credentials,
        bucket=_BUCKET,
        object_key=_SOURCE_KEY,
        ttl_seconds=_TTL_SECONDS,
    )
    assert url.startswith("https://storage.googleapis.com/")
    query = parse_qs(urlparse(url).query)
    assert query["X-Goog-SignedHeaders"] == ["host"]
    assert query["X-Goog-Expires"] == [str(_TTL_SECONDS)]

    with pytest.raises(TransferSourceError, match="TTL"):
        mint_gcs_v4_signed_url(
            credentials=credentials,
            bucket=_BUCKET,
            object_key=_SOURCE_KEY,
            ttl_seconds=60,
        )


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://localhost:9000", True),
        ("http://127.0.0.1:9000", True),
        ("http://minio:9000", True),  # single-label docker-compose service name
        ("http://192.168.1.10:9000", True),  # private range
        ("https://s3.amazonaws.com", False),
        ("https://minio.example.com", False),
        ("http://public.example.com", False),  # public plain HTTP: never local
        ("", False),
    ],
)
def test_endpoint_is_local_detection(endpoint: str, expected: bool) -> None:
    """The dev managed-URL staging decision: only plain-HTTP loopback/private
    storage endpoints are treated as local."""
    from app.ai.runtime import _endpoint_is_local  # pyright: ignore[reportPrivateUsage]

    assert _endpoint_is_local(endpoint) is expected


async def test_orchestrator_mint_managed_url_routes_through_stager() -> None:
    """A wired dev stager serves the managed URL; without one the orchestrator
    mints directly from the source storage (the direct path is covered by the
    managed-url seam tests)."""

    from app.ai.transfer_orchestrator import TransferOrchestrator
    from app.storage.types import SignedUrl

    class _FakeStager:
        def __init__(self) -> None:
            self.calls: list[tuple[ExternalFileReference, int, Any]] = []
            self.region = _LOCATION

        async def mint(
            self, *, reference: ExternalFileReference, ttl_seconds: int, source_storage: Any
        ) -> SignedUrl:
            self.calls.append((reference, ttl_seconds, source_storage))
            return SignedUrl(
                url="https://staged.example/url",
                method="GET",
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )

    pdf = _pdf_content()
    storage = FakeObjectStorage(bucket="local-source")
    await storage.put(_SOURCE_KEY, pdf, content_type="application/pdf")
    reference = _reference(digest=hashlib.sha256(pdf).hexdigest(), size_bytes=len(pdf))

    class _References:
        """Protocol-compliant no-op reference store (mint never touches it)."""

        async def create_or_adopt(self, reference: ExternalFileReference) -> ExternalFileReference:
            return reference

        async def find_live(self, **kwargs: object) -> ExternalFileReference | None:
            return None

        async def adopt(self, **kwargs: object) -> bool:
            return True

        async def mark_expired(self, **kwargs: object) -> bool:
            return True

        async def mark_deleted(self, **kwargs: object) -> bool:
            return True

        async def resolve_for_deletion(self, **kwargs: object) -> ExternalFileReference | None:
            return None

        async def list_for_request(self, **kwargs: object) -> list[ExternalFileReference]:
            return []

        async def expire_all_for_request(self, **kwargs: object) -> int:
            return 0

    stager = _FakeStager()
    orchestrator = TransferOrchestrator(
        storage=storage,
        references=_References(),
        managed_url_stager=stager,
    )
    signed = await orchestrator.mint_managed_url(reference=reference, ttl_seconds=900)
    assert signed.url == "https://staged.example/url"
    assert len(stager.calls) == 1
    assert stager.calls[0][1] == 900
    assert stager.calls[0][2] is storage
