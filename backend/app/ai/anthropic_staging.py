"""Provider-neutral Anthropic upload contracts and fake (v0.8 Scope §2.3, §2.4, §6.6).

The Anthropic large-file path uploads a verified **transient** private source
to the beta Files API and references the returned provider file id as a
``document`` source with ``source.type = "file"`` at dispatch; a retained
private S3 source is served by a just-in-time managed download URL as a
``document`` source with ``source.type = "url"`` (Scope §2.4). This module
owns the provider-neutral half of that contract with **no Anthropic import**:
the reviewed MIME/size bounds, the pinned beta header/version, the fail-closed
upload contract validation and the deterministic fake store — so the default
test suite can exercise every upload, reuse, expiration and deletion path
hermetically (Scope §6.6 checkbox 3).

The real adapter (``app/ai/providers/anthropic_upload.py``) implements
:class:`~app.ai.staging.TransferStore` over the Anthropic Files REST API and
shares these rules, so the fake and the adapter can never drift about what is
uploaded where, which sources are eligible or what a durable external file
reference must look like.

Fail-closed rules enforced here (Scope §2.1/§2.4, §5.3, §5.6, providers.yaml
``provider: anthropic``, re-verified 2026-08-11):

- ``provider_upload`` carries exactly one ``application/pdf`` at most
  32,000,000 bytes — the provider's own 32 MB request-payload ceiling always
  wins over the template's 50 MB ceiling (Scope §2.1, decision 3);
- only a **transient** source may be uploaded (retained sources prefer the
  managed-signed-url mode, Scope §2.4);
- the reference region must equal the provider's configured inference
  geography (no provider path silently changes geography, Scope §5.7);
- Anthropic has **no automatic expiry**: uploaded files persist until
  ``DELETE /v1/files/{file_id}``, so the reviewed retention kind is
  delete-only (``until_deleted``) and terminal deletion/reconciliation is the
  only removal (providers.yaml, Scope §6.1 checkbox 1).

Deletion is best-effort terminal cleanup of the **AI-owned provider copy
only**: it never touches the feature-owned source object (Scope §2.5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from app.ai.errors import TransferStagingError
from app.ai.staging import ExternalFileReference, ExternalReferenceStatus, TransferStore
from app.ai.transfer import (
    MAX_LARGE_ATTACHMENT_BYTES,
    NON_INLINE_MIME_TYPES,
    PdfPagesContract,
    SourceLifecycle,
    TransferMode,
    derive_idempotency_key,
    load_transfer_contracts,
)

#: The v0.8 non-inline MIME contract applies: exactly one ``application/pdf``
#: (Scope §2.1, §5.3). The provider's ``document`` source permits PDF and
#: plain text, but the reviewed large-file contract is PDF-only.
ANTHROPIC_UPLOAD_MIME_TYPES = NON_INLINE_MIME_TYPES

#: The provider's own per-request PDF-input ceiling (32 MB, the whole request
#: not just the PDF) always wins over the template's 50 MB ceiling (Scope
#: §2.1 decision 3, providers.yaml ``provider: anthropic``).
ANTHROPIC_UPLOAD_MAX_BYTES = 32_000_000

#: The reviewed beta header/version for the Files API, pinned in exactly one
#: place (Scope §6.6 checkbox 1). The upload, delete and Messages-API file-id
#: dispatch seams all import this value; the wire header is
#: ``anthropic-beta: files-api-2025-04-14`` (official contract:
#: https://platform.claude.com/docs/en/build-with-claude/files, re-verified
#: 2026-08-11 in ``app/ai/contracts/providers.yaml``).
ANTHROPIC_FILES_BETA_VERSION = "files-api-2025-04-14"

#: The bounded chunk size the upload seam reads from the verified temporary
#: file, so a large source is never accumulated in Python memory (Scope §2.3).
ANTHROPIC_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024

#: The filename sent to the Files API. The name is derived, never
#: caller-controlled, and carries no source path or organisation identifier.
ANTHROPIC_UPLOAD_FILENAME = "attachment.pdf"

#: The max template large-file ceiling is re-exported under the Anthropic name
#: so the fake/adapter never needs to import the generic ceiling directly.
ANTHROPIC_UPLOAD_TEMPLATE_MAX_BYTES = MAX_LARGE_ATTACHMENT_BYTES

# --- PDF page/context ceiling (Scope §6.6 checkbox 2) ------------------------
#
# The reviewed Anthropic contract (providers.yaml `pdf_pages`) documents the
# PDF page ceiling per request: 600 pages, lowered to 100 when the model's
# context window is under 1M tokens. The template enforces the *effective*
# per-model ceiling before any upload so a 101+-page PDF for a small-context
# model is rejected pre-upload instead of only failing at inference. The page
# count itself is derived by the bounded, fail-closed inspector in
# ``app/ai/pdf_inspection.py`` (never accumulating the source and failing
# closed on anything a classic cross-reference table cannot prove); it is
# re-exported here so the Anthropic stores share one import.


def anthropic_pdf_page_ceiling(context_window: int | None) -> int | None:
    """The reviewed per-model PDF page ceiling for the Anthropic document source.

    Derived from the checked-in provider contract (``pdf_pages``) and the
    routed model's ``context_window``: below the reviewed 1M-token threshold
    the tighter ceiling applies (100 pages), at or above it the full ceiling
    (600 pages). A model without a declared context window is treated as below
    the threshold — the conservative choice. Returns ``None`` only when the
    reviewed contract is absent (impossible for a validated deployment).
    """
    contract = _anthropic_pdf_pages_contract()
    if contract is None:
        return None
    return contract.effective_ceiling(context_window)


@lru_cache(maxsize=1)
def _anthropic_pdf_pages_contract() -> PdfPagesContract | None:
    """The reviewed Anthropic PDF page ceiling, cached (immutable).

    Loaded from the checked-in provider contract fixture so the pre-upload
    seam can never drift from the reviewed numbers recorded in
    ``providers.yaml`` (Scope §6.1 checkbox 1).
    """
    contracts = load_transfer_contracts()
    provider = contracts.providers.get("anthropic")
    if provider is None:
        return None
    mode = provider.transfer_modes.get(TransferMode.PROVIDER_UPLOAD)
    if mode is None or mode.pdf_pages is None:
        return None
    return mode.pdf_pages


def validate_anthropic_upload(
    *,
    mode: TransferMode,
    mime_type: str,
    size_bytes: int,
    source_lifecycle: SourceLifecycle,
    region: str,
    configured_region: str,
) -> None:
    """Fail closed unless the source satisfies the reviewed Anthropic upload contract.

    v0.8 Scope §2.1/§2.4/§5.3: ``provider_upload`` carries exactly one PDF of
    at most 32,000,000 bytes (the provider's request-payload ceiling) from a
    **transient** source, and the reference region must equal the provider's
    configured inference geography. Every violation is raised *before* any
    upload call, so an ineligible source never reaches the Files API. The
    message never echoes the source reference or a URI (BP §28).

    PDF structure and any routed model page ceiling are validated once by the
    provider-neutral service before this adapter contract is evaluated.
    """
    if mode is not TransferMode.PROVIDER_UPLOAD:
        raise TransferStagingError(
            "the Anthropic upload store stages provider_upload transfers only"
        )
    if mime_type not in ANTHROPIC_UPLOAD_MIME_TYPES:
        raise TransferStagingError("the Anthropic upload path accepts exactly one application/pdf")
    if size_bytes > ANTHROPIC_UPLOAD_MAX_BYTES:
        raise TransferStagingError(
            "the uploaded object exceeds the provider's 32 MB request-payload ceiling"
        )
    if source_lifecycle is not SourceLifecycle.TRANSIENT:
        raise TransferStagingError(
            "provider_upload accepts transient sources only; retained sources use "
            "the managed-signed-url mode"
        )
    if region != configured_region:
        raise TransferStagingError(
            "the upload region must match the configured Anthropic inference geography"
        )


class FakeAnthropicUploadStore(TransferStore):
    """Deterministic in-memory :class:`TransferStore` for the Anthropic upload path.

    Simulates the reviewed upload contract (Scope §2.4, §6.6 checkbox 3)
    without a network: external file ids are deterministic per derived
    idempotency key (``file-fake-...``), the delete-only retention kind is
    recorded (no automatic expiry — ``expires_at`` stays ``None``, matching the
    provider's ``until_deleted`` contract), retry-only reuse is scoped to one
    logical request, and best-effort deletion removes only the AI-owned
    provider copy. ``uploads`` and ``deleted`` record every upload/deletion so
    tests can assert that an ineligible source is never uploaded and that AI
    cleanup never touches the feature source. No bytes are ever stored.
    """

    provider_id = "anthropic"

    def __init__(self, *, region: str = "") -> None:
        self._region = region
        self._records: dict[str, ExternalFileReference] = {}
        self._sequence = 0
        #: Every uploaded file id, in upload order (tests).
        self.uploads: list[str] = []
        #: Every deleted file id, in deletion order (tests).
        self.deleted: list[str] = []

    @property
    def records(self) -> list[ExternalFileReference]:
        """All references staged by this store, in stage order (tests)."""
        return list(self._records.values())

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
        validate_anthropic_upload(
            mode=mode,
            mime_type=mime_type,
            size_bytes=size_bytes,
            source_lifecycle=source_lifecycle,
            region=region,
            configured_region=self._region,
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
            self._touch(existing)
            return existing
        self._sequence += 1
        # Provider file ids are unique per upload (the Files API mints them),
        # so the fake mirrors that: a per-store sequence guarantees a fresh id
        # for every new transfer, while retry-only reuse still returns the
        # cached record and therefore the same id (Scope §2.1).
        external_id = f"file-fake-{self._sequence}"
        reference = ExternalFileReference(
            mode=mode,
            provider=self.provider_id,
            external_id=external_id,
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
        self.uploads.append(external_id)
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
        self._touch(record)
        return record

    async def delete(self, reference: ExternalFileReference) -> None:
        """Best-effort terminal deletion of the provider copy; never the source.

        Removes the simulated provider file named by the durable reference and
        marks the record ``deleted``. A record that was already deleted or
        never existed is tolerated (best-effort, idempotent), exactly like the
        real adapter. The feature-owned source object is never touched.
        """
        record = self._records.get(reference.idempotency_key)
        if record is None or record.status is ExternalReferenceStatus.DELETED:
            return
        if record.mode is not TransferMode.PROVIDER_UPLOAD:
            raise TransferStagingError("only provider_upload files can be deleted here")
        self.deleted.append(record.external_id)
        record.status = ExternalReferenceStatus.DELETED
        record.deleted_at = datetime.now(UTC)

    @staticmethod
    def _touch(reference: ExternalFileReference) -> None:
        reference.last_used_at = datetime.now(UTC)


__all__ = [
    "ANTHROPIC_FILES_BETA_VERSION",
    "ANTHROPIC_UPLOAD_CHUNK_BYTES",
    "ANTHROPIC_UPLOAD_FILENAME",
    "ANTHROPIC_UPLOAD_MAX_BYTES",
    "ANTHROPIC_UPLOAD_MIME_TYPES",
    "ANTHROPIC_UPLOAD_TEMPLATE_MAX_BYTES",
    "FakeAnthropicUploadStore",
    "anthropic_pdf_page_ceiling",
    "validate_anthropic_upload",
]
