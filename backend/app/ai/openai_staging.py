"""Provider-neutral OpenAI upload contracts and fake (v0.8 Scope §2.3, §2.4, §6.5).

The OpenAI large-file path uploads a verified transient source to the OpenAI
Files API with ``purpose=user_data`` and the shortest configured
``expires_after``, then passes the provider file id (or, for a retained
private S3 source, a just-in-time managed download URL) through the Responses
API ``input_file`` item (Scope §2.4). This module owns the provider-neutral
half of that contract with **no OpenAI import**: the reviewed MIME/size
bounds, the user-data purpose, the ``expires_after`` anchor and bounds, the
fail-closed upload contract validation and the deterministic fake store — so
the default test suite can exercise every upload, reuse, expiry and deletion
path hermetically (Scope §6.5 checkbox 3).

The real adapter (``app/ai/providers/openai_upload.py``) implements
:class:`~app.ai.staging.TransferStore` over the OpenAI Files API and shares
these rules, so the fake and the adapter can never drift about what is
uploaded where, which sources are eligible or what a durable external file
reference must look like.

Fail-closed rules enforced here (Scope §2.4, §5.3, §5.6):

- ``provider_upload`` carries exactly one ``application/pdf`` at most
  50,000,000 bytes (the reviewed template ceiling; the provider's own 50 MB
  per-file PDF-input limit matches it — Scope §2.1);
- only a **transient** source may be uploaded (retained sources prefer the
  managed-signed-url mode, Scope §2.4);
- the reference region must equal the provider's configured deployment region
  (no provider path silently changes region);
- the configured ``expires_after`` duration must sit within the reviewed
  OpenAI bounds (1 hour to 30 days, anchored at ``created_at`` — verified
  2026-08-11, ``app/ai/contracts/providers.yaml``).

Deletion is best-effort terminal cleanup of the **AI-owned provider copy
only**: it never touches the feature-owned source object (Scope §2.5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from app.ai.errors import TransferStagingError
from app.ai.staging import ExternalFileReference, ExternalReferenceStatus, TransferStore
from app.ai.transfer import (
    MAX_LARGE_ATTACHMENT_BYTES,
    NON_INLINE_MIME_TYPES,
    SourceLifecycle,
    TransferMode,
    derive_idempotency_key,
)

#: The v0.8 non-inline MIME contract applies: exactly one ``application/pdf``
#: (Scope §2.1, §5.3). The provider's PDF-input contract permits PDF only.
OPENAI_UPLOAD_MIME_TYPES = NON_INLINE_MIME_TYPES

#: The uploaded copy may never exceed the reviewed template large-file
#: ceiling (Scope §2.1). The provider's own per-file PDF-input limit is 50 MB
#: (``app/ai/contracts/providers.yaml``), so the template and provider bounds
#: coincide; the provider Files API's higher 512 MB storage limit does not
#: apply to PDF model inputs and is deliberately never used.
OPENAI_UPLOAD_MAX_BYTES = MAX_LARGE_ATTACHMENT_BYTES

#: The multipart ``purpose`` value for files passed as model inputs (verified
#: 2026-08-11: "use user_data for files you plan to pass as model inputs").
OPENAI_FILES_PURPOSE = "user_data"

#: The reviewed ``expires_after`` bounds from the provider contract: the
#: duration is anchored at the file's ``created_at`` and must sit between
#: 1 hour and 30 days (3600..2592000 seconds). The deployment-wide setting
#: (``ai_upload_expiry_seconds``) is validated against these bounds by
#: ``app/ai/deployment.py``; the store re-checks them defensively.
OPENAI_EXPIRES_AFTER_MIN_SECONDS = 3_600
OPENAI_EXPIRES_AFTER_MAX_SECONDS = 2_592_000

#: The bounded chunk size the upload seam reads from the verified temporary
#: file, so a 50 MB source is never accumulated in Python memory (Scope §2.3).
OPENAI_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024

#: The filename sent to the Files API. The PDF guide requires a ``.pdf``
#: extension; the name is derived, never caller-controlled, and carries no
#: source path or organisation identifier.
OPENAI_UPLOAD_FILENAME = "attachment.pdf"


def validate_openai_upload(
    *,
    mode: TransferMode,
    mime_type: str,
    size_bytes: int,
    source_lifecycle: SourceLifecycle,
    region: str,
    configured_region: str,
    upload_expiry_seconds: int,
) -> None:
    """Fail closed unless the source satisfies the reviewed OpenAI upload contract.

    v0.8 Scope §2.1/§2.4/§5.3: ``provider_upload`` carries exactly one PDF of
    at most 50,000,000 bytes from a **transient** source, the reference region
    must equal the provider's configured deployment region, and the configured
    ``expires_after`` duration must sit within the reviewed OpenAI bounds.
    Every violation is raised *before* any upload call, so an ineligible
    source never reaches the Files API. The message never echoes the source
    reference or a URI (BP §28).
    """
    if mode is not TransferMode.PROVIDER_UPLOAD:
        raise TransferStagingError("the OpenAI upload store stages provider_upload transfers only")
    if mime_type not in OPENAI_UPLOAD_MIME_TYPES:
        raise TransferStagingError("the OpenAI upload path accepts exactly one application/pdf")
    if size_bytes > OPENAI_UPLOAD_MAX_BYTES:
        raise TransferStagingError("the uploaded object exceeds the reviewed large-file ceiling")
    if source_lifecycle is not SourceLifecycle.TRANSIENT:
        raise TransferStagingError(
            "provider_upload accepts transient sources only; retained sources use "
            "the managed-signed-url mode"
        )
    if region != configured_region:
        raise TransferStagingError(
            "the upload region must match the configured OpenAI deployment region"
        )
    if not (
        OPENAI_EXPIRES_AFTER_MIN_SECONDS
        <= upload_expiry_seconds
        <= OPENAI_EXPIRES_AFTER_MAX_SECONDS
    ):
        raise TransferStagingError(
            "the configured upload expiry must be within the reviewed OpenAI "
            f"expires_after bounds ({OPENAI_EXPIRES_AFTER_MIN_SECONDS}.."
            f"{OPENAI_EXPIRES_AFTER_MAX_SECONDS} seconds)"
        )


class FakeOpenAIUploadStore(TransferStore):
    """Deterministic in-memory :class:`TransferStore` for the OpenAI upload path.

    Simulates the reviewed upload contract (Scope §2.4, §6.5 checkbox 3)
    without a network: external file ids are deterministic per derived
    idempotency key (``file-fake-...``), the user-data purpose and the
    configured ``expires_after`` are recorded on every reference, retry-only
    reuse is scoped to one logical request, and best-effort deletion removes
    only the AI-owned provider copy. ``uploads`` and ``deleted`` record every
    upload/deletion so tests can assert that an ineligible source is never
    uploaded and that AI cleanup never touches the feature source. No bytes
    are ever stored.
    """

    provider_id = "openai"

    def __init__(self, *, region: str = "", upload_expiry_seconds: int = 3_600) -> None:
        self._region = region
        self._upload_expiry_seconds = upload_expiry_seconds
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
        validate_openai_upload(
            mode=mode,
            mime_type=mime_type,
            size_bytes=size_bytes,
            source_lifecycle=source_lifecycle,
            region=region,
            configured_region=self._region,
            upload_expiry_seconds=self._upload_expiry_seconds,
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
        created = datetime.now(UTC)
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
            created_at=created,
            # The provider enforces the configured expires_after anchored at
            # created_at (Scope §2.4); the durable reference records the same
            # wall-clock expiry the provider will enforce.
            expires_at=created + timedelta(seconds=self._upload_expiry_seconds),
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

    def expire_due(self, *, now: datetime | None = None) -> int:
        """Mark every record whose expiry has passed as expired; returns count.

        Mirrors the generic fake's expiry hook: an expired reference is no
        longer reusable and a retry uploads a new idempotent transfer (Scope
        §5.4).
        """
        current = now or datetime.now(UTC)
        expired = 0
        for record in self._records.values():
            if (
                record.status is ExternalReferenceStatus.LIVE
                and record.expires_at is not None
                and record.expires_at <= current
            ):
                record.status = ExternalReferenceStatus.EXPIRED
                expired += 1
        return expired

    @staticmethod
    def _touch(reference: ExternalFileReference) -> None:
        reference.last_used_at = datetime.now(UTC)


__all__ = [
    "OPENAI_EXPIRES_AFTER_MAX_SECONDS",
    "OPENAI_EXPIRES_AFTER_MIN_SECONDS",
    "OPENAI_FILES_PURPOSE",
    "OPENAI_UPLOAD_CHUNK_BYTES",
    "OPENAI_UPLOAD_FILENAME",
    "OPENAI_UPLOAD_MAX_BYTES",
    "OPENAI_UPLOAD_MIME_TYPES",
    "FakeOpenAIUploadStore",
    "validate_openai_upload",
]
