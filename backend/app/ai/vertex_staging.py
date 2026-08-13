"""Provider-neutral Vertex GCS staging contracts and fake (v0.8 Scope §2.3, §2.4, §6.4).

The Vertex large-file path stages a verified private source into a
user-provisioned, non-public, same-region GCS staging bucket and references it
as ``gs://...`` ``fileData`` (Scope §2.4). This module owns the provider-neutral
half of that contract with **no Google import**: the approved org-scoped object
prefix, the reviewed MIME/size bounds, the GCS bucket-name rules, the
fail-closed bucket/object validation rules and the deterministic fake store —
so the default test suite can exercise every staging, validation, reuse and
deletion path hermetically (Scope §6.4 checkbox 4).

The real adapter (``app/ai/providers/vertex_gcs.py``) implements
:class:`~app.ai.staging.TransferStore` over the GCS JSON API and shares these
rules, so the fake and the adapter can never drift about what is staged where,
which buckets are acceptable or what a ``gs://`` reference must look like.

Fail-closed rules enforced here (Scope §2.4, §5.7):

- the staging bucket must be single-region, located in the configured Vertex
  location, owned by the configured Google Cloud project and private (no
  public read access) — multi-region, cross-region, foreign-project and public
  buckets are rejected **before any upload**;
- staged objects live under the organisation-scoped approved prefix
  (``organisations/{organisation_id}/ai/vertex-staging/…``), so a staged copy
  can never be mistaken for a feature-owned source and cross-organisation
  references fail closed;
- a staged object must match the verified source's size, MIME type and digest
  before its ``gs://`` reference becomes durable;
- the reference region must equal the configured Vertex location (no provider
  path silently changes region).

Deletion is best-effort terminal cleanup of the **AI-owned staged copy only**:
it never touches the feature-owned source object, and the deployer-owned GCS
Object Lifecycle Management rule (``age = 1``, asynchronous) is the cleanup
backstop the application never schedules (Scope §2.5).
"""

from __future__ import annotations

import ipaddress
import re
import string
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field

from app.ai.errors import TransferStagingError
from app.ai.staging import ExternalFileReference, ExternalReferenceStatus, TransferStore
from app.ai.transfer import (
    MAX_LARGE_ATTACHMENT_BYTES,
    NON_INLINE_MIME_TYPES,
    SourceLifecycle,
    TransferMode,
    derive_idempotency_key,
)

#: The approved org-scoped prefix for AI-owned Vertex staging objects (Scope
#: §2.4/§6.4). Staged copies live under the organisation namespace but under a
#: dedicated ``vertex-staging`` segment so they are structurally distinct from
#: feature-owned documents and the AI scratch namespace.
VERTEX_STAGING_PREFIX_TEMPLATE = "organisations/{organisation_id}/ai/vertex-staging/"

#: The v0.8 non-inline MIME contract applies: exactly one ``application/pdf``.
VERTEX_STAGING_MIME_TYPES = NON_INLINE_MIME_TYPES

#: The staged copy may never exceed the reviewed template large-file ceiling
#: (Scope §2.1: one PDF at most 50,000,000 bytes; provider ceilings always win).
VERTEX_STAGING_MAX_BYTES = MAX_LARGE_ATTACHMENT_BYTES

#: The bounded chunk size the upload seam reads from the verified temporary
#: file (and the fake reports), so a 50 MB source is never accumulated in
#: Python memory (Scope §2.3).
VERTEX_GCS_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024

#: GCS bucket-name rules (3-63 characters, lowercase letters/digits/``-``/``_``/
#: ``.``, starting and ending with a letter or digit; ``google``/``goog``
#: prefixes and IP-address names are reserved by the service).
_BUCKET_NAME_MIN = 3
_BUCKET_NAME_MAX = 63
_BUCKET_NAME_ALLOWED = frozenset(string.ascii_lowercase + string.digits + "-_.")
_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$")

#: The suffix attached to staged objects. v0.8 stages exactly one PDF, so the
#: suffix is fixed; the object's stored content type is validated regardless.
_STAGED_SUFFIX = ".pdf"


class GcsBucketLocationType(StrEnum):
    """The GCS bucket location type reported by the service (Scope §2.4).

    Only ``SINGLE_REGION`` is acceptable for AI staging: the regional Vertex
    endpoint and the staging bucket must stay in the same configured location
    (same-region transfer, Scope §5.7). Dual- and multi-region buckets are
    rejected before any upload.
    """

    SINGLE_REGION = "SINGLE_REGION"
    DUAL_REGION = "DUAL_REGION"
    MULTI_REGION = "MULTI_REGION"


class StagingBucketMetadata(BaseModel):
    """The verified facts about the configured staging bucket (Scope §2.4).

    The real adapter reads these from the GCS JSON API; the fake is configured
    with them directly. ``project`` is the Google Cloud project the bucket
    belongs to (the real adapter proves it through the project-scoped bucket
    read), ``location`` the bucket's single-region location, ``location_type``
    the GCS location type, ``uniform_bucket_level_access`` whether the bucket
    uses uniform ACLs (a strong private-access guarantee: object-level public
    ACLs are impossible), and ``has_public_read`` whether any IAM binding
    grants allUsers/allAuthenticatedUsers read access. ``versioning_enabled``
    is informational only — deployers may disable soft delete/versioning or
    explicitly accept their longer retention semantics (Scope §2.5) — so it is
    recorded but never fails closed.
    """

    name: str = Field(min_length=1, max_length=_BUCKET_NAME_MAX)
    project: str = Field(min_length=1)
    location: str = Field(min_length=1)
    location_type: GcsBucketLocationType = GcsBucketLocationType.SINGLE_REGION
    uniform_bucket_level_access: bool = True
    has_public_read: bool = False
    versioning_enabled: bool = False


class StagedObjectMetadata(BaseModel):
    """The head/metadata facts of one staged object (Scope §2.4 checkbox 2).

    ``md5_hash`` is the base64-encoded MD5 the GCS JSON API reports (computed
    server-side); the upload seam compares it to the MD5 it computed while
    streaming the verified temporary file, proving the bucket object is
    byte-identical to the copy the transfer verified.
    """

    name: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    content_type: str | None = None
    md5_hash: str | None = None


def validate_gcs_bucket_name(bucket_name: str) -> None:
    """Validate one GCS bucket name against the service's naming rules.

    Raises :class:`TransferStagingError` with a safe message when the name is
    empty, out of the 3-63 character range, contains characters outside
    ``[a-z0-9._-]``, does not start/end with a letter or digit, or uses a
    reserved prefix (``goog``/``google``). A bucket the application would
    refuse to stage into can never be configured.
    """
    if not _BUCKET_NAME_MIN <= len(bucket_name) <= _BUCKET_NAME_MAX:
        raise TransferStagingError(
            f"GCS bucket names must be {_BUCKET_NAME_MIN}-{_BUCKET_NAME_MAX} characters long"
        )
    if any(character not in _BUCKET_NAME_ALLOWED for character in bucket_name):
        raise TransferStagingError(
            "GCS bucket names may contain only lowercase letters, digits, dashes, "
            "underscores and dots"
        )
    if _BUCKET_NAME_RE.fullmatch(bucket_name) is None:
        raise TransferStagingError("GCS bucket names must start and end with a letter or digit")
    if bucket_name.startswith("goog") or "google" in bucket_name:
        raise TransferStagingError(
            "GCS bucket names must not use the reserved 'goog'/'google' prefix"
        )
    if bucket_name.startswith("xn--"):
        raise TransferStagingError(
            "GCS bucket names must not start with the reserved 'xn--' prefix"
        )
    try:
        ipaddress.ip_address(bucket_name)
    except ValueError:
        pass
    else:
        raise TransferStagingError("GCS bucket names must not be an IP address")


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """Parse a ``gs://bucket/object`` URI into ``(bucket, object_name)``.

    The only external-reference form the Vertex path accepts (Scope §2.2: the
    caller can never supply one — these are constructed by the staging
    adapter). Raises :class:`TransferStagingError` for a non-``gs://`` scheme,
    an invalid bucket name or a missing object segment.
    """
    if not uri.startswith("gs://"):
        raise TransferStagingError("Vertex file references must be private gs:// URIs")
    rest = uri[len("gs://") :]
    bucket, separator, object_name = rest.partition("/")
    validate_gcs_bucket_name(bucket)
    if not separator or not object_name:
        raise TransferStagingError("a gs:// reference must name a bucket and an object")
    return bucket, object_name


def vertex_staging_object_key(
    *, organisation_id: UUID, logical_request_id: str, source_digest: str
) -> str:
    """The deterministic, org-scoped object key for one staged copy.

    Derived from the organisation, the logical request and the source digest,
    so a retry of one logical transfer reconstructs the same key (idempotent
    stage) while a changed digest or request produces a distinct object and
    therefore a distinct transfer (Scope §2.1 retry-only reuse, §5.4). The key
    always sits under the approved org-scoped prefix, never in a feature-owned
    namespace.
    """
    prefix = VERTEX_STAGING_PREFIX_TEMPLATE.format(organisation_id=organisation_id)
    return f"{prefix}{logical_request_id}/{source_digest[:32]}{_STAGED_SUFFIX}"


def validate_vertex_staging_bucket(
    *,
    bucket_name: str,
    metadata: StagingBucketMetadata,
    configured_project: str,
    configured_location: str,
) -> None:
    """Fail closed unless the bucket is a private, same-region, same-project staging bucket.

    v0.8 Scope §2.4/§5.7: the staging bucket must be single-region, located in
    the configured Vertex location, owned by the configured Google Cloud
    project and private. Every violation is raised *before* any upload or
    staging call, so an unsafe bucket never receives a copy. The message never
    echoes the bucket contents or a URI (BP §28).
    """
    validate_gcs_bucket_name(bucket_name)
    if metadata.name != bucket_name:
        raise TransferStagingError(
            "the staging bucket metadata does not match the configured bucket"
        )
    if metadata.project != configured_project:
        raise TransferStagingError(
            "the staging bucket belongs to a different Google Cloud project than the configured Vertex project"
        )
    if metadata.location != configured_location:
        raise TransferStagingError("the staging bucket is not in the configured Vertex location")
    if metadata.location_type is not GcsBucketLocationType.SINGLE_REGION:
        raise TransferStagingError(
            "the staging bucket must be single-region; dual-region and multi-region buckets are not accepted"
        )
    if not metadata.uniform_bucket_level_access:
        raise TransferStagingError(
            "the staging bucket must use uniform bucket-level access so object-level public ACLs are impossible"
        )
    if metadata.has_public_read:
        raise TransferStagingError("the staging bucket must be private (no public read access)")


def validate_vertex_staged_object(
    *,
    metadata: StagedObjectMetadata,
    expected_size: int,
    expected_mime: str,
    expected_md5_b64: str | None = None,
) -> None:
    """Validate one staged object against the verified source (Scope §2.4).

    The staged copy must match the source's exact size and MIME type, and —
    when the upload seam computed one — the server-reported MD5 must equal the
    MD5 of the streamed copy. A mismatch fails closed: no ``gs://`` reference
    is created for content the transfer never verified.
    """
    if metadata.size_bytes != expected_size:
        raise TransferStagingError("the staged object size does not match the verified source")
    stored_mime = (metadata.content_type or "").strip().lower().split(";")[0]
    if stored_mime != expected_mime:
        raise TransferStagingError(
            "the staged object content type does not match the verified source"
        )
    if expected_md5_b64 is not None and metadata.md5_hash != expected_md5_b64:
        raise TransferStagingError("the staged object digest does not match the verified source")


class FakeGcsStagingStore(TransferStore):
    """Deterministic in-memory :class:`TransferStore` for the Vertex GCS path.

    Simulates the reviewed staging contract (Scope §2.4, §6.4 checkbox 4)
    without a network: bucket metadata is configured directly and validated
    through the shared fail-closed rules, staged objects are recorded under
    the approved org-scoped prefix with their size/MIME, ``gs://`` external
    ids are deterministic per derived idempotency key, retry-only reuse is
    scoped to one logical request, and best-effort deletion removes only the
    staged copy. ``uploads`` and ``deleted`` record every upload/deletion so
    tests can assert that an invalid bucket never receives a copy and that AI
    cleanup never touches the feature source. No bytes are ever stored.
    """

    provider_id = "vertex"

    def __init__(
        self,
        *,
        bucket: str,
        project: str,
        location: str,
        location_type: GcsBucketLocationType = GcsBucketLocationType.SINGLE_REGION,
        uniform_bucket_level_access: bool = True,
        has_public_read: bool = False,
        versioning_enabled: bool = False,
    ) -> None:
        validate_gcs_bucket_name(bucket)
        if not project or not location:
            raise ValueError("FakeGcsStagingStore requires a project and a location")
        self._bucket = bucket
        self._project = project
        self._location = location
        self._metadata = StagingBucketMetadata(
            name=bucket,
            project=project,
            location=location,
            location_type=location_type,
            uniform_bucket_level_access=uniform_bucket_level_access,
            has_public_read=has_public_read,
            versioning_enabled=versioning_enabled,
        )
        self._objects: dict[str, StagedObjectMetadata] = {}
        self._records: dict[str, ExternalFileReference] = {}
        self.uploads: list[str] = []
        self.deleted: list[str] = []

    def set_bucket_metadata(self, **updates: object) -> None:
        """Test hook: replace bucket metadata to drive the fail-closed cases."""
        self._metadata = self._metadata.model_copy(update=updates)

    @property
    def staged_objects(self) -> list[str]:
        """Object keys currently staged in the simulated bucket (tests)."""
        return list(self._objects)

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
        # Fail closed BEFORE any upload: an unsafe bucket never receives a copy.
        validate_vertex_staging_bucket(
            bucket_name=self._bucket,
            metadata=self._metadata,
            configured_project=self._project,
            configured_location=self._location,
        )
        object_key = vertex_staging_object_key(
            organisation_id=organisation_id,
            logical_request_id=logical_request_id,
            source_digest=source_digest,
        )
        staged = self._objects.get(object_key)
        if staged is not None:
            # Object-level idempotency: the deterministic key produced an
            # existing object; it must still match the verified source.
            validate_vertex_staged_object(
                metadata=staged, expected_size=size_bytes, expected_mime=mime_type
            )
        else:
            self._objects[object_key] = StagedObjectMetadata(
                name=object_key, size_bytes=size_bytes, content_type=mime_type
            )
            self.uploads.append(object_key)
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
        """Best-effort terminal deletion of the staged copy; never the source.

        Resolves the authoritative record by idempotency key, removes the
        staged object named by the ``gs://`` external id from the simulated
        bucket, and marks the record ``deleted``. A reference pointing at a
        foreign bucket fails closed — the adapter must never delete from a
        bucket it does not stage into. A missing object is tolerated
        (best-effort, idempotent), exactly like the real adapter.
        """
        record = self._records.get(reference.idempotency_key)
        if record is None or record.status is ExternalReferenceStatus.DELETED:
            return
        if record.mode is not TransferMode.STORAGE_REFERENCE:
            raise TransferStagingError("only storage_reference staging objects can be deleted here")
        bucket, object_key = parse_gs_uri(record.external_id)
        if bucket != self._bucket:
            raise TransferStagingError("the staged object belongs to a foreign GCS bucket")
        self._objects.pop(object_key, None)
        if object_key not in self.deleted:
            self.deleted.append(object_key)
        record.status = ExternalReferenceStatus.DELETED
        record.deleted_at = datetime.now(UTC)

    def expire_due(self, *, now: datetime | None = None) -> int:
        """Mark every record whose expiry has passed as expired; returns count.

        Mirrors the generic fake's expiry hook: an expired reference is no
        longer reusable and a retry stages a new idempotent transfer (Scope
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
    "VERTEX_GCS_UPLOAD_CHUNK_BYTES",
    "VERTEX_STAGING_MAX_BYTES",
    "VERTEX_STAGING_MIME_TYPES",
    "VERTEX_STAGING_PREFIX_TEMPLATE",
    "FakeGcsStagingStore",
    "GcsBucketLocationType",
    "StagedObjectMetadata",
    "StagingBucketMetadata",
    "parse_gs_uri",
    "validate_gcs_bucket_name",
    "validate_vertex_staged_object",
    "validate_vertex_staging_bucket",
    "vertex_staging_object_key",
]
