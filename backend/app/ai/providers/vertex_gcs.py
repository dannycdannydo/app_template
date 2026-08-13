"""Vertex GCS staging transfer store (v0.8 Scope §2.3, §2.4, §6.4).

The Vertex large-file path stages a verified private source into a
user-provisioned, non-public, same-region GCS staging bucket and passes the
resulting ``gs://`` reference as Vertex ``fileData``. This module is the real
:class:`~app.ai.staging.TransferStore` adapter for the ``vertex`` provider,
implemented over the GCS JSON API with the google-auth credentials the Vertex
inference adapter already uses (ADR-0018: Vertex AI only, workload
identity/ADC or an approved service-account key, never a Gemini API key).

Google storage behavior stays behind this adapter (BP §23, ADR-0017): the AI
layer never constructs provider requests or cloud URIs. The adapter:

- validates the **actual bucket** through the project-scoped bucket read and
  the bucket IAM policy *before any upload* — the bucket must be single-region,
  located in the configured Vertex location, owned by the configured project
  and private (uniform bucket-level access, no allUsers/allAuthenticatedUsers
  bindings). Multi-region, cross-region, foreign-project and public buckets
  fail closed with a permanent :class:`TransferStagingError`;
- stages under the approved org-scoped prefix with a **bounded streaming
  upload** from the verified secure temporary file (the 50 MB ceiling is never
  accumulated in Python memory; SHA-256 and MD5 are computed incrementally
  while reading);
- re-heads the staged object and validates size, MIME type and the
  server-reported MD5 against the streamed copy before the ``gs://`` reference
  is created — a staged object that does not match the verified source is
  never referenced (Scope §2.4 checkbox 2);
- is **idempotent** on the derived idempotency key (retry-only reuse within
  one logical request, Scope §2.1) and deletes **best-effort** on terminal
  cleanup, removing only the AI-owned staged copy — never the feature-owned
  source object (Scope §2.5). Deletion failures propagate so the §6.7
  reconciliation job can cover them; the deployer-owned Object Lifecycle
  Management rule (``age = 1``, asynchronous) is the cleanup backstop the
  application never schedules.
"""

from __future__ import annotations

import base64
import collections.abc as collections_abc
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID

import httpx
import structlog

from app.ai.errors import (
    AIInputValidationError,
    ProviderResponseError,
    ProviderUnavailableError,
    TransferStagingError,
)
from app.ai.providers._google_credentials import (
    google_authorization_header,
    load_google_credentials,
)
from app.ai.staging import ExternalFileReference, ExternalReferenceStatus, TransferStore
from app.ai.transfer import SourceLifecycle, TransferMode, derive_idempotency_key
from app.ai.vertex_staging import (
    VERTEX_GCS_UPLOAD_CHUNK_BYTES,
    VERTEX_STAGING_MAX_BYTES,
    VERTEX_STAGING_MIME_TYPES,
    GcsBucketLocationType,
    StagedObjectMetadata,
    StagingBucketMetadata,
    parse_gs_uri,
    validate_gcs_bucket_name,
    validate_vertex_staged_object,
    validate_vertex_staging_bucket,
    vertex_staging_object_key,
)

#: GCS JSON API roots. Metadata/upload/delete go to the regional-independent
#: global endpoint; the *bucket* itself is pinned to the configured Vertex
#: location by validation, and the Vertex inference endpoint stays regional.
_STORAGE_API = "https://storage.googleapis.com/storage/v1"
_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1"

#: The bucket-resource fields the fail-closed validation needs.
_BUCKET_FIELDS = "name,projectNumber,location,locationType,iamConfiguration,versioning"
#: The staged-object metadata fields needed for size/MIME/digest validation.
_OBJECT_FIELDS = "name,size,contentType,md5Hash"

#: Public IAM members that would make the bucket world-readable.
_PUBLIC_IAM_MEMBERS = frozenset({"allUsers", "allAuthenticatedUsers"})


class _FileDigestStream(httpx.AsyncByteStream):
    """A bounded request-body stream reading the verified temp file in chunks.

    Reads at most ``VERTEX_GCS_UPLOAD_CHUNK_BYTES`` per iteration from the
    verified secure temporary file while folding every chunk into running
    SHA-256 and MD5 digests, so a 50 MB source is uploaded without ever being
    accumulated in Python memory (Scope §2.3). ``get_content_length`` reports
    the exact file size so the GCS media-upload request carries a real
    ``Content-Length`` (never chunked transfer encoding).
    """

    def __init__(self, path: Path, *, size_bytes: int, chunk_size: int) -> None:
        self._path = path
        self._size_bytes = size_bytes
        self._chunk_size = chunk_size
        self._handle: Any = None
        self._sha256 = hashlib.sha256()
        self._md5 = hashlib.md5()
        self._sent = 0

    def get_content_length(self) -> int | None:
        return self._size_bytes

    async def __aiter__(self) -> collections_abc.AsyncIterator[bytes]:
        if self._handle is None:
            self._handle = self._path.open("rb")
        while True:
            chunk = self._handle.read(self._chunk_size)
            if not chunk:
                break
            self._sent += len(chunk)
            self._sha256.update(chunk)
            self._md5.update(chunk)
            yield chunk
        self._handle.close()
        self._handle = None
        if self._sent != self._size_bytes:
            raise ValueError("the staged file changed while being uploaded")

    @property
    def sha256_hex(self) -> str:
        """The SHA-256 of exactly the uploaded bytes (hex)."""
        return self._sha256.hexdigest()

    @property
    def md5_base64(self) -> str:
        """The MD5 of exactly the uploaded bytes (base64, GCS md5Hash form)."""
        return base64.b64encode(self._md5.digest()).decode("ascii")


#: Module logger. GCS failure log lines carry the exception category only —
#: never URLs, credentials, object keys or content (BP §28).
logger = structlog.get_logger()


class GcsTransferStore(TransferStore):
    """Vertex ``storage_reference`` staging over the GCS JSON API."""

    provider_id = "vertex"

    def __init__(
        self,
        *,
        project: str,
        location: str,
        bucket: str,
        credentials_path: str = "",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not project:
            raise AIInputValidationError("vertex project is required")
        if not location:
            raise AIInputValidationError("vertex location is required")
        validate_gcs_bucket_name(bucket)
        self._project = project
        self._location = location
        self._bucket = bucket
        # The staging region is the configured Vertex location: a reference can
        # never silently move to another region (Scope §5.7).
        self.region = location
        self._credentials: Any = load_google_credentials(credentials_path)
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        #: Resolved project number of the configured project, cached after the
        #: first bucket verification (ownership is proven by comparing it with
        #: the bucket's reported projectNumber).
        self._resolved_project_number: str | None = None
        #: In-process live reference cache keyed by the derived idempotency
        #: key (retry-only reuse within one logical request, Scope §2.1). The
        #: durable row remains the authoritative dedup across processes.
        self._records: dict[str, ExternalFileReference] = {}

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": google_authorization_header(self._credentials)}

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str],
    ) -> httpx.Response:
        """One GCS GET with transport failures translated to the safe taxonomy.

        A blocked or failing connection (Google unreachable, VPN issue,
        TLS failure) must surface as a retryable provider error, never a raw
        httpx exception that escapes as an opaque 500/502. Only the exception
        category is logged (BP §28).
        """
        try:
            return await self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "ai.gcs.http.transport_failure",
                exception_type=type(exc).__name__,
            )
            raise ProviderUnavailableError("the GCS staging endpoint is unreachable") from exc

    async def _delete(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        """One GCS DELETE with the same transport-error translation as ``_get``."""
        try:
            return await self._client.delete(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "ai.gcs.http.transport_failure",
                exception_type=type(exc).__name__,
            )
            raise ProviderUnavailableError("the GCS staging endpoint is unreachable") from exc

    async def _project_number(self) -> str:
        """Resolve the configured project's numeric id (cached).

        GCS reports bucket ownership as the owning project's *number*, while
        the deployment configuration names the project by id or number. The
        project's GCS service account email embeds the number, so it is
        resolved once through the documented
        ``projects/{project}/serviceAccount`` endpoint and reused for every
        ownership check. An unverifiable project fails closed.
        """
        if self._resolved_project_number is not None:
            return self._resolved_project_number
        url = f"{_STORAGE_API}/projects/{quote(self._project, safe='')}/serviceAccount"
        response = await self._get(url, headers=self._auth_headers())
        if response.is_error:
            raise TransferStagingError("the configured Google Cloud project could not be verified")
        try:
            data = cast(dict[str, Any], response.json())
        except ValueError as exc:
            raise TransferStagingError(
                "the configured Google Cloud project could not be verified"
            ) from exc
        match = re.search(r"service-(\d+)@", str(data.get("email_address") or ""))
        if match is None:
            raise TransferStagingError("the configured Google Cloud project could not be verified")
        number = match.group(1)
        self._resolved_project_number = number
        return number

    async def _bucket_metadata(self) -> StagingBucketMetadata:
        """Read and verify the actual configured bucket (Scope §2.4 checkbox 2).

        The bucket is read through the standard GCS ``b/{bucket}`` endpoint and
        its reported ``projectNumber`` must equal the configured project's
        resolved number — a foreign-project bucket fails closed here. The
        bucket IAM policy is then inspected for public bindings; together with
        the uniform-access requirement this proves the bucket is private before
        any upload.
        """
        url = f"{_STORAGE_API}/b/{quote(self._bucket, safe='')}"
        response = await self._get(
            url, params={"fields": _BUCKET_FIELDS}, headers=self._auth_headers()
        )
        if response.is_error:
            if response.status_code >= 500:
                raise ProviderUnavailableError("the GCS staging bucket could not be verified")
            raise TransferStagingError(
                "the configured GCS staging bucket is not accessible in the configured Google Cloud project"
            )
        try:
            data = cast(dict[str, Any], response.json())
        except ValueError as exc:
            raise ProviderResponseError(
                "the GCS staging bucket returned an unreadable response"
            ) from exc
        if str(data.get("projectNumber") or "") != await self._project_number():
            raise TransferStagingError(
                "the GCS staging bucket belongs to a different Google Cloud project"
            )
        location_type_raw = str(data.get("locationType") or "").strip().lower()
        try:
            location_type = GcsBucketLocationType(location_type_raw)
        except ValueError as exc:
            raise TransferStagingError(
                "the staging bucket has an unknown GCS location type"
            ) from exc
        iam_config_raw = data.get("iamConfiguration")
        iam_config = (
            cast(dict[str, Any], iam_config_raw) if isinstance(iam_config_raw, dict) else {}
        )
        uniform_acl_raw = iam_config.get("uniformBucketLevelAccess")
        uniform_access = (
            cast(dict[str, Any], uniform_acl_raw) if isinstance(uniform_acl_raw, dict) else {}
        )
        versioning_raw = data.get("versioning")
        versioning = (
            cast(dict[str, Any], versioning_raw) if isinstance(versioning_raw, dict) else {}
        )
        return StagingBucketMetadata(
            name=str(data.get("name") or ""),
            project=str(data.get("projectNumber") or ""),  # proven ownership
            location=str(data.get("location") or ""),
            location_type=location_type,
            uniform_bucket_level_access=bool(uniform_access.get("enabled") is True),
            has_public_read=await self._bucket_has_public_read(),
            versioning_enabled=bool(versioning.get("enabled") is True),
        )

    async def _bucket_has_public_read(self) -> bool:
        """Whether any IAM binding grants public read access (Scope §2.4)."""
        response = await self._get(
            f"{_STORAGE_API}/b/{quote(self._bucket, safe='')}/iam",
            params={"fields": "bindings"},
            headers=self._auth_headers(),
        )
        if response.is_error:
            if response.status_code >= 500:
                raise ProviderUnavailableError(
                    "the GCS staging bucket policy could not be verified"
                )
            raise TransferStagingError("the GCS staging bucket IAM policy could not be read")
        try:
            data = cast(dict[str, Any], response.json())
        except ValueError as exc:
            raise ProviderResponseError(
                "the GCS staging bucket returned an unreadable IAM policy"
            ) from exc
        bindings_raw = data.get("bindings")
        bindings = cast(list[Any], bindings_raw) if isinstance(bindings_raw, list) else []
        for binding_value in bindings:
            binding = cast(dict[str, Any], binding_value) if isinstance(binding_value, dict) else {}
            members_raw = binding.get("members")
            members = cast(list[Any], members_raw) if isinstance(members_raw, list) else []
            if _PUBLIC_IAM_MEMBERS & {str(member) for member in members}:
                return True
        return False

    async def _object_metadata(self, object_key: str) -> StagedObjectMetadata | None:
        """Head one staged object's metadata, or ``None`` when missing."""
        response = await self._get(
            f"{_STORAGE_API}/b/{quote(self._bucket, safe='')}/o/{quote(object_key, safe='')}",
            params={"fields": _OBJECT_FIELDS},
            headers=self._auth_headers(),
        )
        if response.status_code == 404:
            return None
        if response.is_error:
            if response.status_code >= 500:
                raise ProviderUnavailableError("the staged GCS object could not be verified")
            raise TransferStagingError("the staged GCS object could not be verified")
        try:
            data = cast(dict[str, Any], response.json())
        except ValueError as exc:
            raise ProviderResponseError(
                "the staged GCS object returned an unreadable response"
            ) from exc
        return StagedObjectMetadata(
            name=str(data.get("name") or ""),
            size_bytes=int(data.get("size") or 0),
            content_type=data.get("contentType"),
            md5_hash=data.get("md5Hash"),
        )

    async def _upload_object(
        self,
        *,
        object_key: str,
        source_path: Path,
        mime_type: str,
        size_bytes: int,
        expected_digest: str,
    ) -> str:
        """Stream one verified temp file into the bucket; returns the GCS md5.

        Uses the GCS simple media upload with a bounded chunked request body
        (Content-Length declared up front) and records the incremental SHA-256
        and MD5 of exactly the uploaded bytes. The SHA-256 must equal the
        verified source digest — the caller streamed the source through the
        verified temp file, so this proves the staged copy is byte-identical
        to the object the transfer verified. A transient transport failure
        (timeout, 5xx) raises the retryable provider error; a 4xx refusal is a
        permanent staging failure.
        """
        stream = _FileDigestStream(
            source_path, size_bytes=size_bytes, chunk_size=VERTEX_GCS_UPLOAD_CHUNK_BYTES
        )
        request = self._client.build_request(
            "POST",
            f"{_UPLOAD_API}/b/{quote(self._bucket, safe='')}/o",
            params={"uploadType": "media", "name": object_key},
            headers={
                **self._auth_headers(),
                "Content-Type": mime_type,
                "Content-Length": str(size_bytes),
            },
            content=stream,
        )
        try:
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            logger.warning(
                "ai.gcs.http.transport_failure",
                exception_type=type(exc).__name__,
            )
            raise ProviderUnavailableError(
                "the staged object could not be uploaded to GCS"
            ) from exc
        if response.is_error:
            if response.status_code >= 500:
                raise ProviderUnavailableError("the staged object could not be uploaded to GCS")
            raise TransferStagingError("the staged object upload was refused by GCS")
        if stream.sha256_hex != expected_digest:
            raise TransferStagingError(
                "the staged object digest does not match the verified source"
            )
        return stream.md5_base64

    def _source_md5_base64(self, path: Path, *, size_bytes: int) -> str:
        """Re-derive the MD5 of the verified temp file (base64, GCS md5Hash form).

        On the object-reuse path the staged copy was uploaded by a previous
        attempt or process, so its server-reported ``md5Hash`` must be proven
        against the verified source bytes before the ``gs://`` reference is
        reused — a same-size/same-MIME but corrupted or replaced object can
        otherwise receive a durable reference (Scope §2.4 checkbox 2). The file
        is read in the same bounded chunks as the upload seam, and the total
        bytes read must equal the verified size.
        """
        md5 = hashlib.md5()
        read = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(VERTEX_GCS_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                read += len(chunk)
                md5.update(chunk)
        if read != size_bytes:
            raise TransferStagingError("the verified source changed while being re-read for reuse")
        return base64.b64encode(md5.digest()).decode("ascii")

    async def stage(
        self,
        *,
        mode: TransferMode,
        organisation_id: UUID,
        logical_request_id: str,
        source_reference: str,
        source_digest: str,
        mime_type: str,
        size_bytes: int,
        source_lifecycle: SourceLifecycle,
        region: str,
        expires_at: datetime | None,
        source_path: Path | None = None,
    ) -> ExternalFileReference:
        """Stage one verified source into the private GCS bucket (Scope §2.4).

        Validates the actual bucket, the reviewed mode/MIME/size/region
        contract and the staged object's size/MIME/digest before the ``gs://``
        reference is created; a retry of one logical request reuses the live
        reference instead of uploading again (Scope §2.1). ``source_path`` is
        the verified secure temporary file from
        :class:`~app.ai.streamed_source.StreamedSource` — required for this
        adapter, since it must stream the bytes bounded.
        """
        if mode is not TransferMode.STORAGE_REFERENCE:
            raise TransferStagingError(
                "the Vertex GCS staging store stages storage_reference transfers only"
            )
        if mime_type not in VERTEX_STAGING_MIME_TYPES:
            raise TransferStagingError(
                "the Vertex staging path accepts exactly one application/pdf"
            )
        if size_bytes > VERTEX_STAGING_MAX_BYTES:
            raise TransferStagingError("the staged object exceeds the reviewed large-file ceiling")
        if region != self._location:
            raise TransferStagingError(
                "the staging region must match the configured Vertex location"
            )
        if source_path is None:
            raise TransferStagingError(
                "the Vertex GCS staging store requires the verified source temporary file"
            )
        key = derive_idempotency_key(
            provider=self.provider_id,
            mode=mode,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            source_digest=source_digest,
            region=region,
        )
        existing = self._records.get(key)
        if existing is not None and existing.is_live:
            existing.last_used_at = datetime.now(UTC)
            return existing
        metadata = await self._bucket_metadata()
        validate_vertex_staging_bucket(
            bucket_name=self._bucket,
            metadata=metadata,
            configured_project=await self._project_number(),
            configured_location=self._location,
        )
        object_key = vertex_staging_object_key(
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            source_digest=source_digest,
        )
        staged = await self._object_metadata(object_key)
        if staged is not None:
            # Object-level idempotency: the deterministic key produced an
            # existing object. The staged copy was uploaded by a previous
            # attempt or process, so re-derive the verified source's MD5 and
            # require the staged object's server-reported md5Hash to match it
            # before reuse — a same-size/same-MIME but corrupted or replaced
            # object must never receive a durable gs:// reference (Scope §2.4
            # checkbox 2).
            source_md5_b64 = self._source_md5_base64(source_path, size_bytes=size_bytes)
            validate_vertex_staged_object(
                metadata=staged,
                expected_size=size_bytes,
                expected_mime=mime_type,
                expected_md5_b64=source_md5_b64,
            )
        else:
            md5_b64 = await self._upload_object(
                object_key=object_key,
                source_path=source_path,
                mime_type=mime_type,
                size_bytes=size_bytes,
                expected_digest=source_digest,
            )
            staged = await self._object_metadata(object_key)
            if staged is None:
                raise TransferStagingError("the staged object could not be verified after upload")
            validate_vertex_staged_object(
                metadata=staged,
                expected_size=size_bytes,
                expected_mime=mime_type,
                expected_md5_b64=md5_b64,
            )
        reference = ExternalFileReference(
            mode=mode,
            provider=self.provider_id,
            external_id=f"gs://{self._bucket}/{object_key}",
            source_reference=source_reference,
            source_digest=source_digest,
            size_bytes=size_bytes,
            mime_type=mime_type,
            source_lifecycle=source_lifecycle,
            region=self._location,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            idempotency_key=key,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        self._records[key] = reference
        return reference

    async def find_reusable(
        self,
        *,
        mode: TransferMode,
        organisation_id: UUID,
        logical_request_id: str,
        source_digest: str,
        region: str,
    ) -> ExternalFileReference | None:
        """Return the live matching staged reference for a retry, or ``None``.

        Retry-only reuse (Scope §2.1/§2.3): the derived idempotency key is the
        exact predicate (provider, mode, organisation, logical request, digest,
        region), so a changed digest or region yields no hit and the caller
        stages a new transfer.
        """
        key = derive_idempotency_key(
            provider=self.provider_id,
            mode=mode,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            source_digest=source_digest,
            region=region,
        )
        record = self._records.get(key)
        if record is None or not record.is_live:
            return None
        record.last_used_at = datetime.now(UTC)
        return record

    async def delete(self, reference: ExternalFileReference) -> None:
        """Best-effort terminal deletion of the staged copy; never the source.

        Resolves the authoritative record by idempotency key, parses the
        ``gs://`` external id and deletes exactly that object — a reference
        pointing at a foreign bucket fails closed (the adapter must never
        delete from a bucket it does not stage into). A missing object is
        tolerated (already deleted by the deployer-owned lifecycle rule or a
        previous cleanup). A genuine failure propagates so the §6.7
        reconciliation job can cover the orphan (Scope §2.5).
        """
        record = self._records.get(reference.idempotency_key)
        if record is None or record.status is ExternalReferenceStatus.DELETED:
            return
        if record.mode is not TransferMode.STORAGE_REFERENCE:
            raise TransferStagingError("only storage_reference staging objects can be deleted here")
        bucket, object_key = parse_gs_uri(record.external_id)
        if bucket != self._bucket:
            raise TransferStagingError("the staged object belongs to a foreign GCS bucket")
        response = await self._delete(
            f"{_STORAGE_API}/b/{quote(bucket, safe='')}/o/{quote(object_key, safe='')}",
            headers=self._auth_headers(),
        )
        if response.is_error and response.status_code not in (404, 410):
            if response.status_code >= 500:
                raise ProviderUnavailableError("the staged GCS object could not be deleted")
            raise TransferStagingError("the staged GCS object could not be deleted")
        record.status = ExternalReferenceStatus.DELETED
        record.deleted_at = datetime.now(UTC)

    async def aclose(self) -> None:
        """Release the adapter's HTTP client (mirrors the provider adapters)."""
        await self._client.aclose()


__all__ = ["GcsTransferStore"]
