"""S3 adapter unit tests (Scope §6.2, blueprint §17, ADR-0006).

These run in the default suite and never touch the network: boto3's client
objects are replaced with mocks so the tests exercise the adapter's own logic
(pre-signed URL parameters, error mapping, checksum parsing, lazy bucket
creation, idempotence). The real provider behaviour — a signed upload round
trip through a live S3-compatible server, private-bucket denial and lazy
creation — is proven by ``test_storage_integration.py`` under the
``storage_integration`` marker.

moto was evaluated for these tests and rejected: moto 5.x does not intercept
boto3 clients that pass an explicit ``endpoint_url``, which this adapter always
does (see the handoff in ``.handoff/implementation.md``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from app.ai.attachments import MAX_ATTACHMENT_BYTES
from app.ai.transfer import MANAGED_URL_MAX_TTL_SECONDS
from app.core.config import Settings
from app.storage import SIGV4_MAX_PRESIGN_SECONDS, ObjectStorage, S3Storage
from app.storage.base import DEFAULT_SIGNED_URL_TTL
from app.storage.factory import get_storage
from app.storage.types import ObjectInfo, SignedUrl

_ENDPOINT = "http://storage.local:9000"
_PUBLIC_ENDPOINT = "http://public.local:9000"
_BUCKET = "test-bucket"
_KEY = "organisations/org-1/documents/file-1/original.pdf"
_FILE_ID = uuid.uuid4()


def _make_storage(*, public_endpoint: str | None = None) -> S3Storage:
    return S3Storage(
        bucket=_BUCKET,
        endpoint_url=_ENDPOINT,
        region="us-east-1",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
        public_endpoint_url=public_endpoint,
    )


def _storage_with_mocked_clients(
    *,
    public_endpoint: str | None = None,
    client: Mock | None = None,
    presign_client: Mock | None = None,
) -> S3Storage:
    """Return storage whose boto3 clients are replaced with mocks.

    Client construction stays offline; the mocks take over every SDK call so
    the adapter logic runs without a provider.
    """
    storage = _make_storage(public_endpoint=public_endpoint)
    storage._client = client if client is not None else Mock()  # type: ignore[reportPrivateUsage]
    storage._presign_client = (  # type: ignore[reportPrivateUsage]
        presign_client if presign_client is not None else Mock()
    )
    storage._bucket_ensured = True  # type: ignore[reportPrivateUsage]
    return storage


def _client_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": "boom"}, "ResponseMetadata": {}},
        "Operation",
    )


def _storage_with_real_presign(*, public_endpoint: str | None = None) -> S3Storage:
    """Return storage whose real boto3 clients sign URLs locally.

    Presigning is a pure local computation (no network calls), so a real client
    can prove the produced query parameters without a provider. Bucket creation
    is skipped by marking the bucket ensured.
    """
    storage = _make_storage(public_endpoint=public_endpoint)
    storage._bucket_ensured = True  # type: ignore[reportPrivateUsage]
    return storage


def _query_parameters(url: str) -> dict[str, str]:
    """Return a URL's first-level query parameters as a plain dict."""
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


async def test_s3_is_an_object_storage_implementation() -> None:
    assert isinstance(_make_storage(), ObjectStorage)


async def test_upload_url_presigns_a_put_with_declared_metadata() -> None:
    """create_upload_url signs put_object with the bucket, key and content type."""
    presign = Mock()
    presign.generate_presigned_url.return_value = "https://presigned/upload"
    storage = _storage_with_mocked_clients(presign_client=presign)
    upload = await storage.create_upload_url(
        file_id=_FILE_ID,
        object_key=_KEY,
        content_type="application/pdf",
        size_bytes=1024,
    )
    presign.generate_presigned_url.assert_called_once_with(
        ClientMethod="put_object",
        Params={"Bucket": _BUCKET, "Key": _KEY, "ContentType": "application/pdf"},
        ExpiresIn=int(DEFAULT_SIGNED_URL_TTL.total_seconds()),
    )
    assert isinstance(upload, SignedUrl)
    assert upload.method == "PUT"
    assert upload.url == "https://presigned/upload"
    remaining = upload.expires_at - datetime.now(UTC)
    assert remaining <= DEFAULT_SIGNED_URL_TTL
    assert remaining > timedelta(minutes=14, seconds=55)


async def test_upload_url_ttl_can_be_overridden() -> None:
    presign = Mock()
    presign.generate_presigned_url.return_value = "https://presigned/upload"
    storage = _storage_with_mocked_clients(presign_client=presign)
    await storage.create_upload_url(
        file_id=_FILE_ID,
        object_key=_KEY,
        content_type="application/pdf",
        size_bytes=1024,
        expires_in=timedelta(minutes=5),
    )
    presign.generate_presigned_url.assert_called_once_with(
        ClientMethod="put_object",
        Params={"Bucket": _BUCKET, "Key": _KEY, "ContentType": "application/pdf"},
        ExpiresIn=300,
    )


async def test_download_url_presigns_a_get() -> None:
    presign = Mock()
    presign.generate_presigned_url.return_value = "https://presigned/download"
    storage = _storage_with_mocked_clients(presign_client=presign)
    download = await storage.create_download_url(object_key=_KEY)
    presign.generate_presigned_url.assert_called_once_with(
        ClientMethod="get_object",
        Params={"Bucket": _BUCKET, "Key": _KEY},
        ExpiresIn=int(DEFAULT_SIGNED_URL_TTL.total_seconds()),
    )
    assert download.method == "GET"
    assert download.url == "https://presigned/download"


async def test_upload_and_download_urls_use_sigv4() -> None:
    """Plan P4: presigned URLs are SigV4, not the deprecated SigV2 scheme.

    Presigning is local, so the real botocore client (not a mock) produces the
    URL and the test asserts the actual query authentication: the algorithm, a
    host-bound signed-header set, and the absence of SigV2 parameters.
    """
    storage = _storage_with_real_presign()
    upload = await storage.create_upload_url(
        file_id=_FILE_ID,
        object_key=_KEY,
        content_type="application/pdf",
        size_bytes=1024,
    )
    download = await storage.create_download_url(object_key=_KEY)

    upload_parameters = _query_parameters(upload.url)
    download_parameters = _query_parameters(download.url)
    for parameters in (upload_parameters, download_parameters):
        assert parameters["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
        assert parameters["X-Amz-Credential"]
        assert parameters["X-Amz-Date"]
        assert parameters["X-Amz-Signature"]
        assert "host" in parameters["X-Amz-SignedHeaders"].split(";")
        # SigV2 query authentication must not be used.
        assert "AWSAccessKeyId" not in parameters
        assert "Signature" not in parameters
    # The upload binds the declared content type; the download only binds host.
    assert "content-type" in upload_parameters["X-Amz-SignedHeaders"].split(";")
    assert upload_parameters["X-Amz-Expires"] == str(int(DEFAULT_SIGNED_URL_TTL.total_seconds()))


async def test_sigv4_expiry_follows_an_overridden_ttl() -> None:
    storage = _storage_with_real_presign()
    upload = await storage.create_upload_url(
        file_id=_FILE_ID,
        object_key=_KEY,
        content_type="application/pdf",
        size_bytes=1024,
        expires_in=timedelta(minutes=5),
    )
    assert _query_parameters(upload.url)["X-Amz-Expires"] == "300"


async def test_empty_region_signs_the_us_east_1_scope() -> None:
    """SigV4 needs a region, and an unset region must not be left to botocore's
    ambient configuration (the adapter documents a us-east-1 fallback)."""
    storage = S3Storage(
        bucket=_BUCKET,
        endpoint_url=_ENDPOINT,
        region="",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
    )
    storage._bucket_ensured = True  # type: ignore[reportPrivateUsage]
    download = await storage.create_download_url(object_key=_KEY)
    credential = _query_parameters(download.url)["X-Amz-Credential"]
    assert credential.split("/")[2] == "us-east-1"


async def test_split_public_endpoint_signs_sigv4_against_the_public_host() -> None:
    """Both clients carry the SigV4 config, so the public-endpoint pre-signer
    also produces a host-bound SigV4 URL."""
    storage = _storage_with_real_presign(public_endpoint=_PUBLIC_ENDPOINT)
    download = await storage.create_download_url(object_key=_KEY)
    assert urlparse(download.url).netloc == "public.local:9000"
    parameters = _query_parameters(download.url)
    assert parameters["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert "host" in parameters["X-Amz-SignedHeaders"].split(";")


def test_both_clients_are_configured_for_sigv4() -> None:
    storage = _make_storage(public_endpoint=_PUBLIC_ENDPOINT)
    assert storage._client.meta.config.signature_version == "s3v4"  # type: ignore[reportPrivateUsage]
    assert storage._presign_client.meta.config.signature_version == "s3v4"  # type: ignore[reportPrivateUsage]


def test_configured_signed_url_ttl_bounds_stay_under_the_sigv4_cap() -> None:
    """Plan P4: no configured presign lifetime may reach SigV4's 7-day cap."""
    assert DEFAULT_SIGNED_URL_TTL.total_seconds() <= SIGV4_MAX_PRESIGN_SECONDS
    assert MANAGED_URL_MAX_TTL_SECONDS <= SIGV4_MAX_PRESIGN_SECONDS
    upload_ttl_field = Settings.model_fields["storage_upload_url_ttl_seconds"]
    upper_bounds = [
        bound.le  # type: ignore[attr-defined]
        for bound in upload_ttl_field.metadata
        if getattr(bound, "le", None) is not None
    ]
    assert upper_bounds, "storage_upload_url_ttl_seconds must declare an upper bound"
    assert max(upper_bounds) <= SIGV4_MAX_PRESIGN_SECONDS


async def test_head_object_maps_metadata() -> None:
    """A 200 head becomes ObjectInfo with size, content type and ETag checksum."""
    client = Mock()
    client.head_object.return_value = {
        "ContentLength": 1234,
        "ContentType": "application/pdf",
        "ETag": '"abc123"',
    }
    storage = _storage_with_mocked_clients(client=client)
    info = await storage.head_object(_KEY)
    client.head_object.assert_called_once_with(Bucket=_BUCKET, Key=_KEY)
    assert isinstance(info, ObjectInfo)
    assert info.object_key == _KEY
    assert info.size_bytes == 1234
    assert info.content_type == "application/pdf"
    assert info.checksum == "abc123"  # ETag quotes stripped


async def test_head_object_missing_returns_none() -> None:
    for code in ("404", "NoSuchKey", "NotFound"):
        client = Mock()
        client.head_object.side_effect = _client_error(code)
        storage = _storage_with_mocked_clients(client=client)
        assert await storage.head_object(_KEY) is None


async def test_head_object_reports_other_errors() -> None:
    client = Mock()
    client.head_object.side_effect = _client_error("AccessDenied")
    storage = _storage_with_mocked_clients(client=client)
    with pytest.raises(Exception, match="AccessDenied"):
        await storage.head_object(_KEY)


async def test_read_object_reads_the_body_off_thread() -> None:
    """read_object reads get_object's body through asyncio.to_thread so the
    event loop is never blocked (v0.7 Scope §6.4 AI storage-reference seam)."""
    client = Mock()
    body = Mock()
    body.read.return_value = b"%PDF-1.7 analysis fixture"
    client.get_object.return_value = {"Body": body}
    storage = _storage_with_mocked_clients(client=client)
    data = await storage.read_object(_KEY)
    client.get_object.assert_called_once_with(Bucket=_BUCKET, Key=_KEY)
    body.read.assert_called_once_with()
    body.close.assert_called_once_with()
    assert data == b"%PDF-1.7 analysis fixture"


async def test_read_object_is_bounded_by_max_bytes() -> None:
    """The bounded read (v0.7 Scope §6.4): the body is read with a hard cap, an
    oversized body raises ValueError instead of being allocated, and the
    streaming body is closed even on the bounded-read failure so repeated AI
    reads never leak HTTP connections."""
    client = Mock()
    body = Mock()
    body.read.return_value = b"x" * (MAX_ATTACHMENT_BYTES + 1)
    client.get_object.return_value = {"Body": body}
    storage = _storage_with_mocked_clients(client=client)
    with pytest.raises(ValueError, match="read limit"):
        await storage.read_object(_KEY, max_bytes=MAX_ATTACHMENT_BYTES)
    # The body was read with the cap, not fully: at most max_bytes + 1 bytes.
    body.read.assert_called_once_with(MAX_ATTACHMENT_BYTES + 1)
    body.close.assert_called_once_with()


async def test_read_object_within_the_bound_returns_the_body_and_closes_it() -> None:
    client = Mock()
    body = Mock()
    body.read.return_value = b"%PDF-1.7 analysis fixture"
    client.get_object.return_value = {"Body": body}
    storage = _storage_with_mocked_clients(client=client)
    data = await storage.read_object(_KEY, max_bytes=1024)
    body.read.assert_called_once_with(1025)
    body.close.assert_called_once_with()
    assert data == b"%PDF-1.7 analysis fixture"


async def test_read_object_missing_raises_key_error() -> None:
    for code in ("404", "NoSuchKey", "NotFound"):
        client = Mock()
        client.get_object.side_effect = _client_error(code)
        storage = _storage_with_mocked_clients(client=client)
        with pytest.raises(KeyError):
            await storage.read_object(_KEY)


async def test_read_object_reports_other_errors() -> None:
    client = Mock()
    client.get_object.side_effect = _client_error("AccessDenied")
    storage = _storage_with_mocked_clients(client=client)
    with pytest.raises(Exception, match="AccessDenied"):
        await storage.read_object(_KEY)


async def test_delete_object_is_idempotent() -> None:
    for code in ("404", "NoSuchKey", "NotFound"):
        client = Mock()
        client.delete_object.side_effect = _client_error(code)
        storage = _storage_with_mocked_clients(client=client)
        await storage.delete_object(_KEY)  # must not raise
        client.delete_object.assert_called_once_with(Bucket=_BUCKET, Key=_KEY)


async def test_delete_object_reports_other_errors() -> None:
    client = Mock()
    client.delete_object.side_effect = _client_error("AccessDenied")
    storage = _storage_with_mocked_clients(client=client)
    with pytest.raises(Exception, match="AccessDenied"):
        await storage.delete_object(_KEY)


async def test_list_objects_omits_empty_start_after() -> None:
    """Botocore receives StartAfter only on continuation pages."""

    client = Mock()
    client.list_objects_v2.return_value = {}
    storage = _storage_with_mocked_clients(client=client)

    await storage.list_objects("prefix/")
    await storage.list_objects("prefix/", start_after="prefix/last")

    first_parameters = client.list_objects_v2.call_args_list[0].kwargs
    second_parameters = client.list_objects_v2.call_args_list[1].kwargs
    assert "StartAfter" not in first_parameters
    assert second_parameters["StartAfter"] == "prefix/last"


async def test_ensure_bucket_creates_and_is_idempotent() -> None:
    client = Mock()
    storage = _make_storage()
    storage._client = client  # type: ignore[reportPrivateUsage]
    await storage.ensure_bucket()
    await storage.ensure_bucket()
    # The first call creates the bucket; the second is skipped entirely.
    client.create_bucket.assert_called_once_with(Bucket=_BUCKET)


async def test_ensure_bucket_treats_existing_bucket_as_success() -> None:
    for code in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
        client = Mock()
        client.create_bucket.side_effect = _client_error(code)
        storage = _make_storage()
        storage._client = client  # type: ignore[reportPrivateUsage]
        await storage.ensure_bucket()  # must not raise
        client.create_bucket.assert_called_once_with(Bucket=_BUCKET)


async def test_ensure_bucket_reports_other_errors() -> None:
    client = Mock()
    client.create_bucket.side_effect = _client_error("AccessDenied")
    storage = _make_storage()
    storage._client = client  # type: ignore[reportPrivateUsage]
    with pytest.raises(Exception, match="AccessDenied"):
        await storage.ensure_bucket()


def test_constructor_requires_a_bucket() -> None:
    with pytest.raises(ValueError, match="bucket"):
        S3Storage(bucket="", endpoint_url=_ENDPOINT)


def test_constructor_requires_an_endpoint() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        S3Storage(bucket=_BUCKET, endpoint_url="")


def test_constructor_rejects_non_http_endpoint() -> None:
    with pytest.raises(ValueError, match="http"):
        S3Storage(bucket=_BUCKET, endpoint_url="localhost:9000")


def test_constructor_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError, match="ttl"):
        S3Storage(bucket=_BUCKET, endpoint_url=_ENDPOINT, url_ttl=timedelta(0))


# --- Factory (get_storage, wired from settings) ---


def test_get_storage_returns_cached_s3_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory wires the s3 provider to S3Storage (Scope §6.2)."""
    get_storage.cache_clear()
    monkeypatch.setattr(
        "app.storage.factory.get_settings",
        lambda: Settings(
            app_env="test",
            database_url="postgresql+asyncpg://x",
            storage_provider="s3",
            storage_bucket="test-bucket",
            storage_endpoint_url="http://localhost:9000",
            storage_region="us-east-1",
            storage_access_key_id="seaweedfs",
            storage_secret_access_key="seaweedfs",
        ),
    )
    storage = get_storage()
    assert isinstance(storage, S3Storage)
    assert storage.bucket == "test-bucket"
    assert get_storage() is storage  # lru_cache singleton, like get_settings
    get_storage.cache_clear()


async def test_public_endpoint_uses_separate_presign_client() -> None:
    """When the public endpoint differs, presigning goes through a second client."""
    storage = _make_storage(public_endpoint=_PUBLIC_ENDPOINT)
    assert storage._client is not storage._presign_client  # type: ignore[reportPrivateUsage]
    assert storage._presign_client.meta.endpoint_url == _PUBLIC_ENDPOINT  # type: ignore[reportPrivateUsage]


async def test_same_endpoint_shares_one_client() -> None:
    storage = _make_storage()
    assert storage._client is storage._presign_client  # type: ignore[reportPrivateUsage]
