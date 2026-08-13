"""Dev-mode managed-URL staging: local sources → private GCS bucket → https URL.

The managed-signed-url mode mints a short-lived signed URL from the source
storage and hands it to the provider as a file input. That only works when
the source storage is publicly reachable over HTTPS. In development the
template's storage seam is typically a local MinIO instance over plain HTTP —
a signed URL from it would be rejected by the minter and unreachable by any
cloud provider anyway.

This module is the development seam for that case (v0.8 Scope §2.3/§6.4,
§6.5): :class:`GcsManagedUrlStager` re-verifies the retained source, stages a
byte-identical copy into the user-provisioned private GCS temp bucket (the
same one the Vertex ``storage_reference`` path uses —
``AI_VERTEX_TEMP_GCS_BUCKET``, with the deployer's ``age = 1`` Object Lifecycle
rule as the cleanup backstop) and mints a GCS v4 RSA-signed HTTPS GET URL that
a cloud provider can actually fetch. The source object itself is never
modified, the staged copy is AI-owned and only ever lives under the approved
org-scoped staging prefix, and the URL is a temporary bearer capability for
one dispatch — never persisted or logged (BP §28).

The stager is wired by the application runtime only when the storage seam
cannot produce a provider-reachable HTTPS URL (``app/ai/runtime.py``); with a
public HTTPS storage the orchestrator mints the URL directly and no copy is
ever made (the reviewed managed-signed-url contract, Scope §2.3).
"""

from __future__ import annotations

import binascii
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import structlog

from app.ai.errors import AIInputValidationError, TransferSourceError
from app.ai.providers._google_credentials import load_google_credentials
from app.ai.providers.vertex_gcs import GcsTransferStore
from app.ai.staging import ExternalFileReference
from app.ai.streamed_source import StreamedSource
from app.ai.transfer import (
    MANAGED_URL_DEFAULT_TTL_SECONDS,
    MANAGED_URL_MAX_TTL_SECONDS,
    NON_INLINE_MIME_TYPES,
    SourceLifecycle,
    TransferMode,
)
from app.ai.vertex_staging import parse_gs_uri
from app.storage.base import ObjectStorage
from app.storage.types import SignedUrl

#: The GCS JSON-API host signed URLs target (regional-independent; the bucket
#: itself is pinned to the configured Vertex location by the staging
#: validation, and the provider fetches through the global host).
_GCS_HOST = "storage.googleapis.com"

#: Module logger. Step outcomes only — sizes, digests-ok booleans and counts,
#: never object keys, gs:// URIs, signed URLs or content (BP §28).
logger = structlog.get_logger()


def _canonical_uri(bucket: str, object_key: str) -> str:
    """The canonical resource path: bucket + each object segment quoted.

    GCS v4 signing canonicalises the path with the slashes preserved as path
    separators and every segment percent-encoded (``organisations/x/y.pdf``
    stays three segments, never ``%2F``-joined).
    """
    segments = [quote(bucket, safe="")] + [
        quote(segment, safe="") for segment in object_key.split("/")
    ]
    return "/" + "/".join(segments)


def mint_gcs_v4_signed_url(
    *,
    credentials: Any,
    bucket: str,
    object_key: str,
    ttl_seconds: int,
) -> str:
    """Mint a GCS v4 RSA-signed read-only HTTPS GET URL for one private object.

    Implements the official GCS v4 signing protocol directly over the
    service-account key's private signer (google-auth ships the
    :class:`google.auth.crypt.RSASigner`; the template deliberately has no
    google-cloud-storage SDK dependency, ADR-0018). The canonical query is
    percent-encoded exactly as the spec requires (the credential scope's
    slashes become ``%2F``), the canonical URI keeps the object's path
    separators, and the signature is computed over the canonical request
    before being appended to the final URL. The URL is a one-dispatch bearer
    capability.
    """
    signer = getattr(credentials, "signer", None)
    signer_email = getattr(credentials, "signer_email", None)
    if signer is None or not signer_email:
        raise TransferSourceError(
            "managed-URL GCS staging requires a service-account key that can sign URLs"
        )
    if not MANAGED_URL_DEFAULT_TTL_SECONDS <= ttl_seconds <= MANAGED_URL_MAX_TTL_SECONDS:
        raise TransferSourceError(
            "managed URL TTL must be between the reviewed bounds for GCS staging"
        )
    now = datetime.now(UTC)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{now.strftime('%Y%m%d')}/auto/storage/goog4_request"
    canonical_uri = _canonical_uri(bucket, object_key)
    params: list[tuple[str, str]] = [
        ("X-Goog-Algorithm", "GOOG4-RSA-SHA256"),
        ("X-Goog-Credential", f"{signer_email}/{scope}"),
        ("X-Goog-Date", timestamp),
        ("X-Goog-Expires", str(int(ttl_seconds))),
        ("X-Goog-SignedHeaders", "host"),
    ]
    # The canonical query is the percent-encoded form — the GCS spec signs
    # exactly what the server re-parses, so the credential scope's slashes are
    # ``%2F`` here (verified against the live GCS signature check).
    encoded = [(quote(name, safe=""), quote(value, safe="")) for name, value in params]
    canonical_query = "&".join(f"{name}={value}" for name, value in encoded)
    canonical_request = (
        f"GET\n{canonical_uri}\n{canonical_query}\nhost:{_GCS_HOST}\n\nhost\nUNSIGNED-PAYLOAD"
    )
    string_to_sign = (
        f"GOOG4-RSA-SHA256\n{timestamp}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    # GCS v4 expects the RSA signature hex-encoded (``binascii.hexlify``), not
    # base64 — the exact wire form the official google-cloud-storage library
    # emits; hex is also URL-safe, so no query escaping is needed.
    signature = binascii.hexlify(signer.sign(string_to_sign.encode("utf-8"))).decode("ascii")
    return f"https://{_GCS_HOST}{canonical_uri}?{canonical_query}&X-Goog-Signature={signature}"


class GcsManagedUrlStager:
    """Re-verify a retained source, stage it into the GCS temp bucket and mint a URL.

    Used only when the source storage cannot produce a provider-reachable
    HTTPS signed URL (local MinIO in development). The staged GCS copy is an
    AI-owned derivative under the approved org-scoped staging prefix; its
    cleanup is the deployer-owned Object Lifecycle rule (``age = 1``), the same
    backstop as the Vertex ``storage_reference`` path (Scope §2.4).
    """

    def __init__(
        self,
        *,
        project: str,
        location: str,
        bucket: str,
        credentials_path: str = "",
        timeout_seconds: float = 60.0,
        client: Any = None,
    ) -> None:
        self._store = GcsTransferStore(
            project=project,
            location=location,
            bucket=bucket,
            credentials_path=credentials_path,
            timeout_seconds=timeout_seconds,
            client=client,
        )
        credentials = load_google_credentials(credentials_path)
        if getattr(credentials, "signer", None) is None or not getattr(
            credentials, "signer_email", None
        ):
            raise AIInputValidationError(
                "managed-URL GCS staging requires a service-account key with a signer"
            )
        self._credentials = credentials

    @property
    def region(self) -> str:
        """The staging region (the configured Vertex location)."""
        return self._store.region

    async def mint(
        self,
        *,
        reference: ExternalFileReference,
        ttl_seconds: int,
        source_storage: ObjectStorage,
    ) -> SignedUrl:
        """Stage one verified retained source into GCS and mint its signed URL.

        The source is re-verified bounded from the source storage (ownership,
        size, MIME and the exact SHA-256 digest recorded in the durable
        reference) before any byte is copied — an object replaced with
        different content of the same size never receives a URL (Scope §2.3
        exact immutable identity). The GCS staging store then re-validates the
        bucket and verifies the staged object's size/MIME/MD5 before the URL
        is minted.
        """
        if reference.mode is not TransferMode.MANAGED_SIGNED_URL:
            raise TransferSourceError(
                "a managed download URL can only be minted for the managed-signed-url mode"
            )
        if reference.source_lifecycle is not SourceLifecycle.RETAINED:
            raise TransferSourceError(
                "a managed download URL can only be minted for a retained private source"
            )
        logger.info(
            "ai.managed_url.gcs_staging.started",
            size_bytes=reference.size_bytes,
            mime_type=reference.mime_type,
            region=self._store.region,
            ttl_seconds=ttl_seconds,
        )
        async with StreamedSource(
            storage=source_storage,
            reference=reference.source_reference,
            organisation_id=reference.organisation_id,
            max_bytes=reference.size_bytes,
            allowed_mime_types=NON_INLINE_MIME_TYPES,
        ) as source:
            if (
                source.size_bytes != reference.size_bytes
                or source.mime_type != reference.mime_type
                or source.sha256_digest != reference.source_digest
            ):
                raise TransferSourceError(
                    "the referenced storage object changed since it was verified"
                )
            logger.info(
                "ai.managed_url.gcs_staging.verified",
                size_bytes=source.size_bytes,
                mime_type=source.mime_type,
                digest_verified=True,
            )
            staged = await self._store.stage(
                mode=TransferMode.STORAGE_REFERENCE,
                organisation_id=reference.organisation_id,
                logical_request_id=reference.logical_request_id,
                source_reference=reference.source_reference,
                source_digest=source.sha256_digest,
                mime_type=source.mime_type,
                size_bytes=source.size_bytes,
                source_lifecycle=SourceLifecycle.RETAINED,
                region=self._store.region,
                expires_at=None,
                source_path=source.path,
            )
        logger.info(
            "ai.managed_url.gcs_staging.staged",
            size_bytes=staged.size_bytes,
            mime_type=staged.mime_type,
        )
        bucket, object_key = parse_gs_uri(staged.external_id)
        url = mint_gcs_v4_signed_url(
            credentials=self._credentials,
            bucket=bucket,
            object_key=object_key,
            ttl_seconds=ttl_seconds,
        )
        logger.info(
            "ai.managed_url.gcs_staging.minted",
            ttl_seconds=ttl_seconds,
            scheme=urlsplit(url).scheme,
        )
        return SignedUrl(
            url=url,
            method="GET",
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )


__all__ = ["GcsManagedUrlStager", "mint_gcs_v4_signed_url"]
