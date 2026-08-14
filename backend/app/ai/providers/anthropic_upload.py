"""Anthropic Files API upload transfer store (v0.8 Scope §2.3, §2.4, §6.6).

The Anthropic large-file path uploads a verified **transient** private source
to the beta Files API (``POST /v1/files``), records the provider file id as
the durable reference and passes that id through the Messages API as a
``document`` source with ``source.type = "file"`` at dispatch (the adapter
maps it — ``app/ai/providers/anthropic.py``). This module is the real
:class:`~app.ai.staging.TransferStore` adapter for the ``anthropic`` provider,
implemented over the Anthropic Files REST API with the same thin pinned HTTP
client the inference adapter uses (BP §23, ADR-0017); the reviewed beta header
and version are pinned in exactly one place
(``app/ai/anthropic_staging.py``, Scope §6.6 checkbox 1).

Anthropic behavior stays behind this adapter: the AI layer never constructs
provider requests. The adapter:

- enforces the reviewed upload contract (exactly one PDF, at most 32,000,000
  bytes — the provider's request-payload ceiling — a **transient** source and
  a reference region equal to the configured inference geography, Scope §2.4,
  §5.3) *before any upload*;
- streams the verified secure temporary file bounded (never accumulated in
  Python memory) through a multipart upload whose incremental SHA-256 must
  equal the verified source digest — the provider copy is byte-identical to
  the object the transfer verified;
- records the **delete-only** retention kind (providers.yaml
  ``upload_lifecycle: until_deleted``): Anthropic has no automatic expiry, so
  the durable reference carries no ``expires_at`` and terminal
  deletion/reconciliation is the only removal (Scope §6.1 checkbox 1, §6.7);
- is **idempotent** on the derived idempotency key (retry-only reuse within
  one logical request, Scope §2.1) and deletes **best-effort** on terminal
  cleanup, removing only the AI-owned provider copy — never the feature-owned
  source object (Scope §2.5). Deletion failures propagate so the §6.7
  reconciliation job can cover them. A failure *after* the upload succeeded
  (source mutation, malformed response) best-effort deletes the just-uploaded
  file by the id the successful response names; a transport timeout after
  provider acceptance leaves no id to act on, so that window stays bounded by
  the delete-only contract and the mandatory reconciliation job.

Local-transient path (Scope §6.6 lesson from the OpenAI build): with a local
storage seam (offline MinIO), a managed signed URL minted from that storage
can never resolve from the provider's network, so a **transient** source is
served by staging the verified object into the scratch GCS staging directory
and providing a signed URL to that GCS object as the URL document source
instead of uploading to the beta Files API. When constructed with
``stage_transient_locally=True`` (the runtime wires it only when the storage
seam is local), a transient source yields a **no-copy reference** — no
provider file is uploaded, the external id is the immutable source reference
itself — and the dispatch-time managed-URL minting stages it into GCS and
mints the URL. ``delete`` on such a reference marks it deleted without a
provider call (no provider copy exists; the staged GCS copy at dispatch is
cleaned by the deployer's ``age = 1`` lifecycle backstop, Scope §2.5).

Transport failures (timeouts, unreachable, 5xx, 429) surface as the existing
retryable provider errors; permanent validation/refusal failures surface as
:class:`~app.ai.errors.TransferStagingError`. Messages never echo the file id,
the source reference, a URL or the response body (BP §28).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID

import httpx
import structlog

from app.ai.anthropic_staging import (
    ANTHROPIC_FILES_BETA_VERSION,
    ANTHROPIC_UPLOAD_CHUNK_BYTES,
    ANTHROPIC_UPLOAD_FILENAME,
    validate_anthropic_upload,
)
from app.ai.errors import (
    AIInputValidationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
    TransferStagingError,
)
from app.ai.providers.http_transport import translate_http_exception
from app.ai.staging import ExternalFileReference, ExternalReferenceStatus, TransferStore
from app.ai.transfer import SourceLifecycle, TransferMode, derive_idempotency_key
from app.ai.upload_stream import DigestUploadFile

#: The Anthropic API root; ``/v1/files`` is the beta Files API surface. The
#: default mirrors the inference adapter's default base URL.
_DEFAULT_API_ROOT = "https://api.anthropic.com"

#: The Messages API version pinned by the inference adapter.
_ANTHROPIC_VERSION = "2023-06-01"

#: The FileObject field the upload/delete seams need.
_FILE_ID_KEY = "id"

#: Module logger. Failure log lines carry the exception category only — never
#: URLs, credentials, file ids, object keys or content (BP §28).
logger = structlog.get_logger()


class AnthropicTransferStore(TransferStore):
    """Anthropic ``provider_upload`` staging over the beta Files REST API."""

    provider_id = "anthropic"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        region: str = "",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        stage_transient_locally: bool = False,
    ) -> None:
        if not api_key:
            raise AIInputValidationError("an Anthropic API key is required for the upload store")
        self._base_url = (base_url or _DEFAULT_API_ROOT).rstrip("/")
        self._api_key = api_key
        # The configured inference geography (``ai_anthropic_inference_geography``:
        # '' default or 'us'); a reference can never silently move to another
        # geography (Scope §5.7).
        self.region = region
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        #: Local-transient scratch-GCS path (Scope §6.6 lesson): when the
        #: storage seam cannot serve a provider-reachable HTTPS URL, a
        #: transient source is served by a GCS-staged signed URL document
        #: source instead of a beta Files API upload. ``True`` means stage()
        #: yields a no-copy reference for transient sources.
        self.stage_transient_locally = stage_transient_locally
        #: In-process live reference cache keyed by the derived idempotency
        #: key (retry-only reuse within one logical request, Scope §2.1). The
        #: durable row remains the authoritative dedup across processes.
        self._records: dict[str, ExternalFileReference] = {}

    def _auth_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            # The reviewed beta header/version, pinned in one place (Scope
            # §6.6 checkbox 1): upload, delete and the Messages file-id
            # dispatch all use it.
            "anthropic-beta": ANTHROPIC_FILES_BETA_VERSION,
        }

    def _files_url(self) -> str:
        return f"{self._base_url}/v1/files"

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
            raise ProviderRateLimitError("the Anthropic Files API rate limited the request")
        if status >= 500:
            raise ProviderUnavailableError("the Anthropic Files API returned a server error")
        # The status code is safe (low-cardinality) and is the fastest way to
        # diagnose an upload refusal without logging the response body
        # (BP §28).
        logger.warning("ai.anthropic.upload.refused", status=status)
        raise TransferStagingError("the Anthropic Files API refused the request")

    async def _best_effort_delete_uploaded(self, response: httpx.Response) -> None:
        """Best-effort delete of a just-uploaded provider file after a staging failure.

        Runs only when the upload succeeded but verification or response
        parsing failed, so no reference exists yet. The file id is taken from
        the successful response when it parses — source-mutation failures are
        covered because the body still names the file — while an unparseable
        or id-less body leaves nothing addressable to delete. A failed delete
        is suppressed and logged by category only: the delete-only contract
        leaves the copy for the §6.7 reconciliation job. A transport failure
        during the upload itself never reaches here: the provider may have
        accepted the file, but no id exists to act on. File ids and URLs are
        never logged (BP §28).
        """
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            logger.warning("ai.anthropic.upload.cleanup", outcome="no_file_id")
            return
        body_data = cast(dict[str, Any], body)
        file_id = str(body_data.get(_FILE_ID_KEY) or "")
        if not file_id:
            logger.warning("ai.anthropic.upload.cleanup", outcome="no_file_id")
            return
        try:
            response = await self._client.delete(
                self._file_url(file_id), headers=self._auth_headers()
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "ai.anthropic.upload.cleanup",
                outcome="delete_failed",
                category=type(exc).__name__,
            )
        else:
            if response.is_error:
                logger.warning(
                    "ai.anthropic.upload.cleanup",
                    outcome="delete_refused",
                    status=response.status_code,
                )
            else:
                logger.warning("ai.anthropic.upload.cleanup", outcome="deleted")

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
        region) before any upload; a retry of one logical request reuses the
        live reference instead of uploading again (Scope §2.1). ``source_path``
        is the verified secure temporary file from
        :class:`~app.ai.streamed_source.StreamedSource` — required for the
        beta upload, since the adapter must stream the bytes bounded.

        With ``stage_transient_locally`` (local storage seam, Scope §6.6) a
        transient source yields a **no-copy reference** instead: the external
        id is the immutable source reference itself, no provider file is
        uploaded, and the dispatch-time managed-URL minting stages the object
        into the scratch GCS staging directory and provides a signed URL to
        that GCS object as the URL document source. PDF structure and page
        ceilings have already been checked at the common service boundary.
        """
        validate_anthropic_upload(
            mode=mode,
            mime_type=mime_type,
            size_bytes=size_bytes,
            source_lifecycle=source_lifecycle,
            region=region,
            configured_region=self.region,
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

        if self.stage_transient_locally and source_lifecycle is SourceLifecycle.TRANSIENT:
            # Local-transient scratch-GCS path (Scope §6.6 lesson): no provider
            # copy is uploaded here; the durable reference mirrors the
            # managed-signed-url shape (external id = immutable source
            # identity) so the dispatch-time managed-URL minting can stage the
            # verified source into the scratch GCS staging directory and hand
            # Anthropic a signed URL to that GCS object as the document source.
            reference = ExternalFileReference(
                mode=mode,
                provider=self.provider_id,
                external_id=source_reference,
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
                expires_at=None,
            )
            self._records[key] = reference
            return reference

        if source_path is None:
            raise TransferStagingError(
                "the Anthropic upload store requires the verified source temporary file"
            )
        file_wrapper = DigestUploadFile(
            source_path, size_bytes=size_bytes, chunk_size=ANTHROPIC_UPLOAD_CHUNK_BYTES
        )
        try:
            files: dict[str, Any] = {
                "file": (ANTHROPIC_UPLOAD_FILENAME, file_wrapper, mime_type),
            }
            try:
                response = await self._client.post(
                    self._files_url(),
                    headers=self._auth_headers(),
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
                    "the Anthropic Files API returned an unparseable response"
                ) from exc
            if not isinstance(body, dict):
                raise ProviderResponseError(
                    "the Anthropic Files API returned a malformed response body"
                )
            body_data = cast(dict[str, Any], body)
            file_id = str(body_data.get(_FILE_ID_KEY) or "")
            if not file_id:
                raise ProviderResponseError("the Anthropic Files API returned no file id")
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
            # Delete-only retention kind (``until_deleted``): the provider
            # imposes no automatic expiry, so the durable reference carries
            # none — terminal deletion/reconciliation is the only removal.
            expires_at=None,
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

        Deletes the provider file named by the **passed** reference — the
        authoritative durable row the orchestrator resolved — not a record
        from this adapter's in-memory cache. The §6.7 reconciliation sweep
        builds a fresh adapter whose cache is empty, so a delete must work
        purely from the durable reference (v0.8 Scope §2.5/§6.7). A missing
        file (404/410) is tolerated — the provider already removed it, e.g.
        by a previous cleanup — while any other failure propagates so the
        sweep can cover the orphan after the bounded backoff. The feature-owned
        source object is never touched. A no-copy reference (local-transient
        scratch-GCS path, external id = source identity) has no provider file
        to delete and is marked deleted directly.
        """
        record = self._records.get(reference.idempotency_key)
        if record is not None and record.status is ExternalReferenceStatus.DELETED:
            # This adapter already deleted this copy in-process: no-op.
            return
        if reference.mode is not TransferMode.PROVIDER_UPLOAD:
            raise TransferStagingError("only provider_upload files can be deleted here")
        if reference.external_id == reference.source_reference:
            # No-copy reference (local-transient scratch-GCS path): no provider
            # file exists; the dispatch-time GCS staged copy is cleaned by the
            # deployer's age = 1 lifecycle backstop (Scope §2.5).
            if record is not None:
                record.status = ExternalReferenceStatus.DELETED
                record.deleted_at = datetime.now(UTC)
            return
        try:
            response = await self._client.delete(
                self._file_url(reference.external_id), headers=self._auth_headers()
            )
        except httpx.HTTPError as exc:
            raise translate_http_exception(exc) from exc
        if response.is_error and response.status_code not in (404, 410):
            self._raise_for_status(response)
        if record is not None:
            record.status = ExternalReferenceStatus.DELETED
            record.deleted_at = datetime.now(UTC)

    async def aclose(self) -> None:
        """Release the adapter's HTTP client (mirrors the provider adapters)."""
        await self._client.aclose()


__all__ = ["AnthropicTransferStore"]
