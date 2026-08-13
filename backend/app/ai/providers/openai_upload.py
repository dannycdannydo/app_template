"""OpenAI Files API upload transfer store (v0.8 Scope §2.3, §2.4, §6.5).

The OpenAI large-file path uploads a verified **transient** private source to
the OpenAI Files API with ``purpose=user_data`` and the shortest configured
``expires_after``, records the provider file id and its enforced expiry as the
durable reference, and passes that id through the Responses API ``input_file``
item at dispatch (the adapter maps it — ``app/ai/providers/openai.py``). This
module is the real :class:`~app.ai.staging.TransferStore` adapter for the
``openai`` provider, implemented over the OpenAI Files REST API with the same
thin pinned HTTP client the inference adapter uses (BP §23, ADR-0017).

OpenAI behavior stays behind this adapter: the AI layer never constructs
provider requests. The adapter:

- enforces the reviewed upload contract (exactly one PDF, at most
  50,000,000 bytes, a **transient** source, the configured region and an
  ``expires_after`` inside the reviewed 1 hour..30 day bounds — Scope §2.4,
  §5.3) *before any upload*;
- streams the verified secure temporary file bounded (never accumulated in
  Python memory) through a multipart upload whose incremental SHA-256 must
  equal the verified source digest — the provider copy is byte-identical to
  the object the transfer verified;
- derives the durable ``expires_at`` from the provider-reported file expiry
  (``expires_after`` is anchored at the file's ``created_at``);
- is **idempotent** on the derived idempotency key (retry-only reuse within
  one logical request, Scope §2.1) and deletes **best-effort** on terminal
  cleanup, removing only the AI-owned provider copy — never the feature-owned
  source object (Scope §2.5). Deletion failures propagate so the §6.7
  reconciliation job can cover them; the provider's enforced ``expires_after``
  is the automatic-expiry backstop. A failure *after* the upload succeeded
  (source mutation, malformed response) best-effort deletes the just-uploaded
  file by the id the successful response names; a transport timeout after
  provider acceptance leaves no id to act on, so that window is bounded by the
  provider-enforced expiry only.

Transport failures (timeouts, unreachable, 5xx, 429) surface as the existing
retryable provider errors; permanent validation/refusal failures surface as
:class:`~app.ai.errors.TransferStagingError`. Messages never echo the file id,
the source reference, a URL or the response body (BP §28).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
import structlog

from app.ai.errors import (
    AIInputValidationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
    TransferStagingError,
)
from app.ai.openai_staging import (
    OPENAI_EXPIRES_AFTER_MAX_SECONDS,
    OPENAI_EXPIRES_AFTER_MIN_SECONDS,
    OPENAI_FILES_PURPOSE,
    OPENAI_UPLOAD_CHUNK_BYTES,
    OPENAI_UPLOAD_FILENAME,
    validate_openai_upload,
)
from app.ai.providers.http_transport import (
    translate_http_exception,
)
from app.ai.providers.openai import REGIONAL_API_HOSTS
from app.ai.staging import ExternalFileReference, ExternalReferenceStatus, TransferStore
from app.ai.transfer import SourceLifecycle, TransferMode, derive_idempotency_key

#: The OpenAI Files API root. ``expires_after`` is carried as flattened
#: multipart form fields (``expires_after[anchor]``/``expires_after[seconds]``)
#: matching the documented curl form and the SDK's own multipart serialization
#: (a single JSON-string field is refused by the API); the file's ``expires_at``
#: (unix seconds) is echoed on the FileObject. Both verified against the
#: official reference on 2026-08-11 (``app/ai/contracts/providers.yaml``).
_DEFAULT_API_ROOT = "https://api.openai.com/v1"

#: The FileObject fields the upload/delete seams need.
_FILE_ID_KEY = "id"
_EXPIRES_AT_KEY = "expires_at"

#: Module logger. Failure log lines carry the exception category only — never
#: URLs, credentials, file ids, object keys or content (BP §28).
logger = structlog.get_logger()


class _DigestUploadFile:
    """A bounded file wrapper folding every uploaded byte into a SHA-256.

    httpx encodes multipart file fields by calling ``read(chunk)`` repeatedly
    (64 KiB at a time), so wrapping the verified secure temporary file lets
    the adapter compute the SHA-256 of exactly the uploaded bytes without ever
    accumulating the 50 MB ceiling in Python memory (Scope §2.3). The wrapper
    exposes ``fileno`` so httpx reports a real ``Content-Length`` (never
    chunked transfer encoding) without reading ahead.
    """

    def __init__(self, path: Path, *, size_bytes: int, chunk_size: int) -> None:
        self._handle = path.open("rb")
        self._size_bytes = size_bytes
        self._chunk_size = chunk_size
        self._sha256 = hashlib.sha256()
        self._read = 0
        self._closed = False

    def fileno(self) -> int:
        """Delegate to the underlying file so httpx can stat the exact length."""
        return self._handle.fileno()

    def read(self, size: int = -1) -> bytes:
        """Read one bounded chunk and fold it into the running SHA-256."""
        read_size = self._chunk_size if size < 0 else max(1, min(size, self._chunk_size))
        chunk = self._handle.read(read_size)
        if chunk:
            self._read += len(chunk)
            self._sha256.update(chunk)
        return chunk

    @property
    def sha256_hex(self) -> str:
        """The SHA-256 of exactly the uploaded bytes (hex)."""
        return self._sha256.hexdigest()

    @property
    def read_bytes(self) -> int:
        """Total bytes forwarded to the upload so far."""
        return self._read

    @property
    def size_bytes(self) -> int:
        """The verified size the upload must not exceed."""
        return self._size_bytes

    def close(self) -> None:
        if not self._closed:
            self._handle.close()
            self._closed = True


def _file_expiry_timestamp(data: dict[str, Any], *, fallback_seconds: int) -> int:
    """The provider-enforced file expiry as a unix timestamp.

    ``expires_after`` is anchored at the file's ``created_at``, and the
    FileObject echoes the absolute ``expires_at``; when the provider omits it,
    the same anchor is reconstructed from ``created_at`` plus the configured
    duration so the durable reference never records a weaker expiry than the
    provider enforces.
    """
    expires_raw = data.get(_EXPIRES_AT_KEY)
    try:
        return int(expires_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        created_raw = data.get("created_at")
        try:
            created = int(created_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            created = int(datetime.now(UTC).timestamp())
        return created + fallback_seconds


class OpenAITransferStore(TransferStore):
    """OpenAI ``provider_upload`` staging over the Files REST API."""

    provider_id = "openai"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        region: str = "",
        upload_expiry_seconds: int = 3_600,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise AIInputValidationError("an OpenAI API key is required for the upload store")
        if not (
            OPENAI_EXPIRES_AFTER_MIN_SECONDS
            <= upload_expiry_seconds
            <= OPENAI_EXPIRES_AFTER_MAX_SECONDS
        ):
            raise AIInputValidationError(
                "upload_expiry_seconds must be within the reviewed OpenAI "
                f"expires_after bounds ({OPENAI_EXPIRES_AFTER_MIN_SECONDS}.."
                f"{OPENAI_EXPIRES_AFTER_MAX_SECONDS} seconds)"
            )
        # Defense in depth with the inference adapter (v0.7 Scope §6.3): a
        # region derives the regional endpoint and an explicit base URL must
        # name that region's domain, so a directly constructed store can never
        # upload through the global endpoint while the reference is labelled
        # regional (Scope §5.7 never-mislabel rule). The settings validator
        # enforces the same relationship; this constructor re-checks it.
        if region and not base_url:
            base_url = f"https://{REGIONAL_API_HOSTS[region]}/v1"
        elif region and base_url:
            host = urlsplit(base_url).hostname or ""
            if host != REGIONAL_API_HOSTS[region]:
                raise AIInputValidationError(
                    f"AI_OPENAI_BASE_URL host {host!r} conflicts with region {region!r}; "
                    f"regional requests must use https://{REGIONAL_API_HOSTS[region]}/v1"
                )
        self._base_url = (base_url or _DEFAULT_API_ROOT).rstrip("/")
        self._api_key = api_key
        # The configured deployment region (``ai_openai_region``: '' default or
        # 'us'/'eu'); a reference can never silently move to another region
        # (Scope §5.7). The regional domain itself comes from the inference
        # adapter's base URL validation (v0.7 Scope §6.3).
        self.region = region
        self._upload_expiry_seconds = upload_expiry_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        #: In-process live reference cache keyed by the derived idempotency
        #: key (retry-only reuse within one logical request, Scope §2.1). The
        #: durable row remains the authoritative dedup across processes.
        self._records: dict[str, ExternalFileReference] = {}

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _files_url(self) -> str:
        return f"{self._base_url}/files"

    def _file_url(self, file_id: str) -> str:
        return f"{self._files_url()}/{quote(file_id, safe='')}"

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Map a non-2xx Files-API status into the safe taxonomy.

        429 and 5xx are retryable provider errors; other 4xx responses are
        permanent staging failures (the identical retry cannot help). The
        response body is never embedded (BP §28).
        """
        if response.is_success:
            return
        status = response.status_code
        if status == 429:
            raise ProviderRateLimitError("the OpenAI Files API rate limited the request")
        if status >= 500:
            raise ProviderUnavailableError("the OpenAI Files API returned a server error")
        # The status code is safe (low-cardinality) and is the fastest way to
        # diagnose an upload refusal (e.g. a rejected field format) without
        # logging the response body (BP §28).
        logger.warning("ai.openai.upload.refused", status=status)
        raise TransferStagingError("the OpenAI Files API refused the request")

    async def _best_effort_delete_uploaded(self, response: httpx.Response) -> None:
        """Best-effort delete of a just-uploaded provider file after a staging failure.

        Runs only when the upload succeeded but verification or response
        parsing failed, so no reference exists yet. The file id is taken from
        the successful response when it parses — source-mutation failures are
        covered because the body still names the file — while an unparseable
        or id-less body leaves nothing addressable to delete. A failed delete
        is suppressed and logged by category only: the provider-enforced
        ``expires_after`` still bounds the copy. A transport failure during the
        upload itself never reaches here: the provider may have accepted the
        file, but no id exists to act on, so that window stays bounded solely
        by the same provider-enforced expiry. File ids and URLs are never
        logged (BP §28).
        """
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            logger.warning("ai.openai.upload.cleanup", outcome="no_file_id")
            return
        body_data = cast(dict[str, Any], body)
        file_id = str(body_data.get(_FILE_ID_KEY) or "")
        if not file_id:
            logger.warning("ai.openai.upload.cleanup", outcome="no_file_id")
            return
        try:
            response = await self._client.delete(
                self._file_url(file_id), headers=self._auth_headers()
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "ai.openai.upload.cleanup",
                outcome="delete_failed",
                category=type(exc).__name__,
            )
        else:
            if response.is_error:
                logger.warning(
                    "ai.openai.upload.cleanup",
                    outcome="delete_refused",
                    status=response.status_code,
                )
            else:
                logger.warning("ai.openai.upload.cleanup", outcome="deleted")

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
        """Upload one verified transient source to the Files API (Scope §2.4).

        Enforces the reviewed contract (mode, MIME, size, transient lifecycle,
        region, expiry bounds) before any upload; a retry of one logical
        request reuses the live reference instead of uploading again (Scope
        §2.1). ``source_path`` is the verified secure temporary file from
        :class:`~app.ai.streamed_source.StreamedSource` — required, since the
        adapter must stream the bytes bounded.
        """
        validate_openai_upload(
            mode=mode,
            mime_type=mime_type,
            size_bytes=size_bytes,
            source_lifecycle=source_lifecycle,
            region=region,
            configured_region=self.region,
            upload_expiry_seconds=self._upload_expiry_seconds,
        )
        if source_path is None:
            raise TransferStagingError(
                "the OpenAI upload store requires the verified source temporary file"
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

        file_wrapper = _DigestUploadFile(
            source_path, size_bytes=size_bytes, chunk_size=OPENAI_UPLOAD_CHUNK_BYTES
        )
        try:
            # The expiration policy travels as flattened multipart form fields
            # (``expires_after[anchor]`` + ``expires_after[seconds]``) — the
            # documented curl form and the SDK's multipart serialization; the
            # API refuses a single JSON-string field. ``seconds`` is within the
            # reviewed 1 hour..30 day bounds (Scope §2.4).
            data: dict[str, str] = {
                "purpose": OPENAI_FILES_PURPOSE,
                "expires_after[anchor]": "created_at",
                "expires_after[seconds]": str(self._upload_expiry_seconds),
            }
            files: dict[str, Any] = {
                "file": (OPENAI_UPLOAD_FILENAME, file_wrapper, mime_type),
            }
            try:
                response = await self._client.post(
                    self._files_url(),
                    headers=self._auth_headers(),
                    data=data,
                    files=files,
                )
            except httpx.HTTPError as exc:
                raise translate_http_exception(exc) from exc
            self._raise_for_status(response)
        finally:
            file_wrapper.close()

        # The upload succeeded, so the provider now hosts a file. Every failure
        # from here must remove that AI-owned copy before surfacing: no durable
        # reference exists yet, so the orchestrator's compensation (which only
        # runs once stage() returns) cannot cover this window and an untracked
        # file would be unreachable by the reconciliation job.
        try:
            if file_wrapper.read_bytes != file_wrapper.size_bytes:
                raise TransferStagingError("the verified source changed while being uploaded")
            if file_wrapper.sha256_hex != source_digest:
                raise TransferStagingError(
                    "the uploaded file digest does not match the verified source"
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise ProviderResponseError(
                    "the OpenAI Files API returned an unparseable response"
                ) from exc
            if not isinstance(body, dict):
                raise ProviderResponseError(
                    "the OpenAI Files API returned a malformed response body"
                )
            body_data = cast(dict[str, Any], body)
            file_id = str(body_data.get(_FILE_ID_KEY) or "")
            if not file_id:
                raise ProviderResponseError("the OpenAI Files API returned no file id")
            expiry_timestamp = _file_expiry_timestamp(
                body_data, fallback_seconds=self._upload_expiry_seconds
            )
        except Exception:
            await self._best_effort_delete_uploaded(response)
            raise
        reference = ExternalFileReference(
            mode=mode,
            provider=self.provider_id,
            external_id=file_id,
            source_reference=source_reference,
            source_digest=source_digest,
            size_bytes=size_bytes,
            mime_type=mime_type,
            source_lifecycle=source_lifecycle,
            region=region,
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            idempotency_key=key,
            created_at=datetime.now(UTC),
            expires_at=datetime.fromtimestamp(expiry_timestamp, tz=UTC),
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
        """Return the live matching upload reference for a retry, or ``None``.

        Retry-only reuse (Scope §2.1/§2.3): the derived idempotency key is the
        exact predicate (provider, mode, organisation, logical request, digest,
        region), so a changed digest or region yields no hit and the caller
        uploads a new idempotent transfer.
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
        """Best-effort terminal deletion of the provider copy; never the source.

        Resolves the authoritative record by idempotency key and deletes the
        provider file named by the durable reference. A missing file (404/410,
        e.g. already expired or deleted by a previous cleanup) is tolerated;
        a genuine failure propagates so the §6.7 reconciliation job can cover
        the orphan (Scope §2.5). The feature-owned source object is never
        touched.
        """
        record = self._records.get(reference.idempotency_key)
        if record is None or record.status is ExternalReferenceStatus.DELETED:
            return
        if record.mode is not TransferMode.PROVIDER_UPLOAD:
            raise TransferStagingError("only provider_upload files can be deleted here")
        try:
            response = await self._client.delete(
                self._file_url(record.external_id), headers=self._auth_headers()
            )
        except httpx.HTTPError as exc:
            raise translate_http_exception(exc) from exc
        if response.is_error and response.status_code not in (404, 410):
            self._raise_for_status(response)
        record.status = ExternalReferenceStatus.DELETED
        record.deleted_at = datetime.now(UTC)

    async def aclose(self) -> None:
        """Release the adapter's HTTP client (mirrors the provider adapters)."""
        await self._client.aclose()


__all__ = ["OpenAITransferStore"]
