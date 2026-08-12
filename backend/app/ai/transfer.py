"""Provider-neutral transfer contracts for large AI attachments (v0.8 Scope §2, §6.1).

The v0.8 release adds four **transfer modes** — ``inline``, ``provider_upload``,
``managed_signed_url`` and ``storage_reference`` — behind the unchanged
application-facing rule: a feature supplies only a task name and a private
``storage_reference`` (Scope §2.2). This module owns the provider-neutral
contract: the reviewed template constants, the mode/lifecycle enums, the
typed provider contract fixture loaded from ``app/ai/contracts/providers.yaml``
and the deterministic mode-selection function. The fixture records the
re-verified official provider and cloud-storage facts (verification date,
supported API/version, retention/deletion behavior, MIME/size limits and
regional caveats — Scope §6.1 checkbox 1) and the loader fails fast on any
inconsistent declaration (Scope §6.1 checkbox 4).

Import-boundary rule (enforced by ``tests/test_ai_import_boundary.py``): no
module outside ``app/ai/`` may import this module, so feature modules can
never name a transfer mode or a provider reference. Transfer mode selection
and every provider/cloud identifier remain internal to the AI layer; a managed
signed URL or ``gs://`` URI is never a caller-supplied input (Scope §2.2).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import UUID

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.ai.attachments import (
    ALLOWED_ATTACHMENT_MIME_TYPES,
    IMAGE_ATTACHMENT_MIME_TYPES,
)
from app.ai.errors import RegistryValidationError
from app.core.config import AI_KNOWN_PROVIDER_IDS

# --- Reviewed template constants (v0.8 Scope §2.1, §2.2) ---------------------
#
#: The aggregate raw attachment byte threshold below which ``inline`` is the
#: only eligible mode. It is a template constant and cannot be configured
#: above this value (Scope §2.2).
INLINE_AGGREGATE_THRESHOLD_BYTES = 5_000_000

#: The reviewed large-file ceiling: exactly one ``application/pdf`` above the
#: inline threshold and never larger than this many bytes (Scope §2.1). The
#: non-inline path accepts no other MIME type or count in v0.8.
MAX_LARGE_ATTACHMENT_BYTES = 50_000_000

#: The only MIME type the v0.8 non-inline path accepts: exactly one PDF
#: (Scope §2.1, §5.3).
NON_INLINE_MIME_TYPES = frozenset({"application/pdf"})

#: Managed signed-URL TTL defaults and ceiling (Scope §2.2): default 900
#: seconds, maximum 1,800. The URL is minted just before dispatch and never
#: returned, persisted, audited or logged.
MANAGED_URL_DEFAULT_TTL_SECONDS = 900
MANAGED_URL_MAX_TTL_SECONDS = 1_800

#: The maximum provider-upload expiry the template permits. Providers may
#: document shorter ceilings; the fixture records each provider's own bounds
#: and the loader rejects anything above this template ceiling.
MAX_PROVIDER_UPLOAD_EXPIRY_SECONDS = 2_592_000  # 30 days (OpenAI expires_after max)

#: The organisation-scoped AI scratch namespace (v0.7 Scope §6.5 item 4): the
#: v0.7 retention job owns objects under this prefix and the source lifecycle
#: classifier treats them as ``transient`` (short-lived, v0.8 Scope §2.2). The
#: persistence service keeps a same-named alias so retention and the transfer
#: selector can never drift about which namespace is transient.
SCRATCH_KEY_TEMPLATE = "organisations/{organisation_id}/ai/scratch/"


class SourceLifecycle(StrEnum):
    """Where the source object lives in its feature-owned lifecycle (Scope §2.2).

    ``transient`` sources are short-lived objects (for example the
    organisation-scoped AI scratch namespace governed by the v0.7 retention
    job); ``retained`` sources are durable feature-owned objects in private
    S3-compatible storage. The source lifecycle is a property of the object,
    never of the caller's request, and is one of the gates the deterministic
    selector intersects.
    """

    TRANSIENT = "transient"
    RETAINED = "retained"


class TransferMode(StrEnum):
    """The four provider-neutral transfer modes (v0.8 Scope §2.2).

    ``inline`` remains the default but is eligible only through the aggregate
    raw threshold; the other three are never requested by a caller — the
    service selects one only when source lifecycle, task definition,
    organisation policy, model/provider capability and deployment configuration
    all allow it.
    """

    INLINE = "inline"
    PROVIDER_UPLOAD = "provider_upload"
    MANAGED_SIGNED_URL = "managed_signed_url"
    STORAGE_REFERENCE = "storage_reference"


@dataclass(frozen=True)
class TransferDeploymentPolicy:
    """Deployment-level transfer configuration closed over by the executor.

    Built from the typed settings at wiring time (``app/ai/runtime.py``) so the
    service's deterministic selector can never drift from the deployment the
    process actually boots with (v0.8 Scope §2.2, §6.2): the aggregate inline
    threshold, the large-file template ceiling and the enabled non-inline
    modes. Non-inline modes are default-deny — an empty ``enabled_transfer_modes``
    deploys inline only — and ``inline`` always remains in the eligible set, so
    a deployment can never accidentally disable the reviewed default.
    """

    inline_aggregate_threshold_bytes: int = INLINE_AGGREGATE_THRESHOLD_BYTES
    max_large_attachment_bytes: int = MAX_LARGE_ATTACHMENT_BYTES
    enabled_transfer_modes: frozenset[TransferMode] = frozenset({TransferMode.INLINE})

    @property
    def allowed_transfer_modes(self) -> frozenset[TransferMode]:
        """The deployable mode set: inline plus the enabled non-inline modes."""
        return self.enabled_transfer_modes | frozenset({TransferMode.INLINE})


@dataclass(frozen=True)
class ModelModeCeiling:
    """One model's per-mode non-inline limits in provider-neutral form.

    A structural twin of the registry's ``NonInlineModeLimit`` (kept here so
    the selector never depends on the registry module, which imports this one):
    the MIME set and byte ceiling one model can carry for one non-inline mode.
    """

    mime_types: frozenset[str]
    max_bytes: int


@dataclass(frozen=True)
class ModelInlineCeiling:
    """One model's inline-mode declarations in provider-neutral form.

    A structural twin of the registry's inline attachment fields (kept here so
    the selector never depends on the registry module, which imports this one):
    the MIME set, the per-file and aggregate byte ceilings the model can carry
    inline, and the capability facts (``documents`` required for any inline
    attachment, ``vision`` required for image attachments). The selector
    requires a model to fit these before ``inline`` can be selected, so a model
    whose inline MIME set excludes an attachment can never ride a non-inline
    declaration to an inline dispatch, and inline dispatch never violates a
    model's inline MIME or byte limits (v0.8 Scope §6.2).
    """

    mime_types: frozenset[str] = frozenset()
    max_attachment_bytes: int | None = None
    max_total_attachment_bytes: int | None = None
    has_documents_capability: bool = False
    has_vision_capability: bool = False

    def can_carry(self, *, sizes: Sequence[int], mime_types: Sequence[str]) -> bool:
        """Whether one attachment set fits the model's inline declarations.

        Mirrors the registry's inline check: the documents capability, the
        per-file and combined byte ceilings, the MIME coverage and the vision
        requirement for image attachments must all hold, or ``inline`` is not
        an eligible mode for this model and set.
        """
        if not self.has_documents_capability:
            return False
        if self.max_attachment_bytes is None or self.max_total_attachment_bytes is None:
            return False
        if any(size > self.max_attachment_bytes for size in sizes):
            return False
        if sum(sizes) > self.max_total_attachment_bytes:
            return False
        if any(mime_type not in self.mime_types for mime_type in mime_types):
            return False
        has_images = any(mime_type in IMAGE_ATTACHMENT_MIME_TYPES for mime_type in mime_types)
        return not has_images or self.has_vision_capability


class ProviderUploadLifecycle(StrEnum):
    """How a provider retains a transient uploaded file (v0.8 Scope §2.2, §6.1).

    ``expires_after`` is an automatic hard expiry the provider enforces from
    upload time; the ``UploadExpiryContract`` records the reviewed bounds
    (OpenAI's 1 hour to 30 day ``expires_after`` policy). ``until_deleted`` is
    a delete-only lifecycle: the provider keeps the file until an explicit
    terminal delete (or the reconciliation job) removes it, and there is no
    automatic expiry to record (Anthropic's beta Files API, whose uploaded
    files persist until ``DELETE /v1/files/{file_id}``). The two retention
    models are distinct contract kinds: a delete-only provider must never
    declare expiry bounds it does not have.
    """

    EXPIRES_AFTER = "expires_after"
    UNTIL_DELETED = "until_deleted"


class UploadExpiryContract(BaseModel):
    """Provider-documented automatic hard-expiry bounds for a transient upload.

    Recorded from the official provider contract (Scope §6.1 checkbox 1) for
    ``ProviderUploadLifecycle.EXPIRES_AFTER`` providers: the shortest
    supported ``expires_after`` is the template's default so provider-hosted
    copies live as briefly as the provider allows.
    """

    min_seconds: int = Field(ge=1)
    default_seconds: int = Field(ge=1)
    max_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> UploadExpiryContract:
        if not self.min_seconds <= self.default_seconds <= self.max_seconds:
            raise ValueError(
                "upload_expiry bounds must satisfy min_seconds <= default_seconds <= max_seconds"
            )
        if self.max_seconds > MAX_PROVIDER_UPLOAD_EXPIRY_SECONDS:
            raise ValueError(
                f"upload_expiry max_seconds must not exceed {MAX_PROVIDER_UPLOAD_EXPIRY_SECONDS}"
            )
        return self


class ManagedUrlTtlContract(BaseModel):
    """Managed signed-URL TTL bounds (Scope §2.2): default 900, maximum 1,800."""

    default_seconds: int = Field(ge=1)
    max_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def _ttl_is_reviewed(self) -> ManagedUrlTtlContract:
        if self.default_seconds > self.max_seconds:
            raise ValueError("managed_url_ttl default_seconds must not exceed max_seconds")
        if self.max_seconds > MANAGED_URL_MAX_TTL_SECONDS:
            raise ValueError(
                f"managed_url_ttl max_seconds must not exceed {MANAGED_URL_MAX_TTL_SECONDS}"
            )
        if self.default_seconds != MANAGED_URL_DEFAULT_TTL_SECONDS:
            raise ValueError(
                f"managed_url_ttl default_seconds must be {MANAGED_URL_DEFAULT_TTL_SECONDS}"
            )
        return self


class TransferModeContract(BaseModel):
    """One provider's reviewed capability for one transfer mode.

    ``mime_types`` and ``max_bytes`` are the provider-neutral per-mode limits
    (Scope §2.2: "per-mode MIME types and byte ceilings"); provider ceilings
    are lower than the template ceiling and always win (Scope §2.1).
    ``source_lifecycles`` declares which source lifecycles the mode can carry
    — the reviewed contract pins ``inline`` and ``storage_reference`` to both
    lifecycles, ``provider_upload`` to transient sources and
    ``managed_signed_url`` to retained sources. ``upload_lifecycle`` records
    the provider's retention kind for ``provider_upload`` (automatic expiry
    with recorded bounds, or delete-only with no automatic expiry — Scope
    §6.1 checkbox 1), and ``upload_expiry`` carries the reviewed bounds only
    for ``expires_after`` providers. ``same_region_required`` is mandatory
    for ``storage_reference``: staging must stay in the configured Vertex
    location (Scope §2.4, §5.7).
    """

    mime_types: list[str] = Field(min_length=1)
    max_bytes: int = Field(ge=1)
    source_lifecycles: list[SourceLifecycle] = Field(min_length=1)
    upload_lifecycle: ProviderUploadLifecycle | None = None
    upload_expiry: UploadExpiryContract | None = None
    managed_url_ttl: ManagedUrlTtlContract | None = None
    same_region_required: bool = False

    @field_validator("mime_types")
    @classmethod
    def _mime_types_are_reviewed(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("mime_types must not contain duplicates")
        return values

    @field_validator("source_lifecycles")
    @classmethod
    def _lifecycles_are_unique(cls, values: list[SourceLifecycle]) -> list[SourceLifecycle]:
        if len(set(values)) != len(values):
            raise ValueError("source_lifecycles must not contain duplicates")
        return values


class ProviderTransferContract(BaseModel):
    """The re-verified official contract for one provider (Scope §6.1).

    ``verified_at`` records when the official sources were last re-checked;
    ``sources`` maps the documentation names to their URLs; ``retention_notes``
    and ``regional_notes`` record the provider's retention/deletion behavior
    and regional caveats in plain language. ``regional_notes`` is a required
    declaration: every provider must state where requests are processed (or
    that no template-controlled pinning exists) so a silent omission cannot
    validate (Scope §6.1 checkbox 4).
    """

    provider: str = Field(min_length=1, max_length=128)
    verified_at: date
    api_version: str = Field(min_length=1)
    sources: dict[str, str] = Field(min_length=1)
    retention_notes: str = Field(min_length=1)
    regional_notes: str = Field(min_length=1)
    transfer_modes: dict[TransferMode, TransferModeContract] = Field(
        default_factory=lambda: dict[TransferMode, TransferModeContract]()
    )

    @field_validator("sources")
    @classmethod
    def _sources_are_https(cls, sources: dict[str, str]) -> dict[str, str]:
        for name, url in sources.items():
            if not url.startswith("https://"):
                raise ValueError(f"provider contract source {name!r} must be an https URL")
        return sources

    @model_validator(mode="after")
    def _modes_follow_reviewed_contract(self) -> ProviderTransferContract:
        for mode, contract in self.transfer_modes.items():
            if mode is TransferMode.INLINE:
                inline = contract
                if inline.max_bytes != INLINE_AGGREGATE_THRESHOLD_BYTES:
                    raise ValueError(
                        "inline max_bytes must equal the "
                        f"{INLINE_AGGREGATE_THRESHOLD_BYTES} byte aggregate threshold"
                    )
                unknown = set(inline.mime_types) - set(ALLOWED_ATTACHMENT_MIME_TYPES)
                if unknown:
                    raise ValueError(
                        f"inline MIME types outside the template allowlist: {sorted(unknown)}"
                    )
                # Inline is the universal default mode and must carry every
                # source lifecycle (Scope §2.2 reviewed lifecycle matrix); a
                # contract omitting one side silently narrows the default.
                if inline.source_lifecycles != [
                    SourceLifecycle.TRANSIENT,
                    SourceLifecycle.RETAINED,
                ]:
                    raise ValueError(
                        "inline carries transient and retained sources only as the "
                        "reviewed lifecycle matrix declares"
                    )
            else:
                if not set(contract.mime_types) <= set(NON_INLINE_MIME_TYPES):
                    raise ValueError(
                        "v0.8 non-inline modes carry exactly one PDF; "
                        f"found: {sorted(set(contract.mime_types) - set(NON_INLINE_MIME_TYPES))}"
                    )
                if not (
                    INLINE_AGGREGATE_THRESHOLD_BYTES
                    < contract.max_bytes
                    <= MAX_LARGE_ATTACHMENT_BYTES
                ):
                    raise ValueError(
                        "non-inline max_bytes must be above the "
                        f"{INLINE_AGGREGATE_THRESHOLD_BYTES} byte inline threshold and at most "
                        f"{MAX_LARGE_ATTACHMENT_BYTES}"
                    )
            if mode is TransferMode.PROVIDER_UPLOAD:
                if contract.upload_lifecycle is None:
                    raise ValueError(
                        "provider_upload must declare its upload_lifecycle retention kind"
                    )
                if contract.upload_lifecycle is ProviderUploadLifecycle.EXPIRES_AFTER:
                    if contract.upload_expiry is None:
                        raise ValueError(
                            "provider_upload with expires_after retention must declare "
                            "upload_expiry bounds"
                        )
                elif contract.upload_expiry is not None:
                    raise ValueError(
                        "provider_upload with until_deleted retention must not declare "
                        "upload_expiry bounds it does not have"
                    )
                if contract.source_lifecycles != [SourceLifecycle.TRANSIENT]:
                    raise ValueError("provider_upload carries transient sources only")
            else:
                if contract.upload_lifecycle is not None or contract.upload_expiry is not None:
                    raise ValueError(
                        "upload_lifecycle and upload_expiry belong to provider_upload only"
                    )
            if mode is TransferMode.MANAGED_SIGNED_URL:
                if contract.managed_url_ttl is None:
                    raise ValueError("managed_signed_url must declare managed_url_ttl bounds")
                if contract.source_lifecycles != [SourceLifecycle.RETAINED]:
                    raise ValueError("managed_signed_url carries retained sources only")
            if mode is TransferMode.STORAGE_REFERENCE:
                # Pinned to the reviewed Vertex/fake contract (Scope §2.2):
                # private GCS staging serves both lifecycles, and it must stay
                # in the configured Vertex region (Scope §2.4, §5.7).
                if contract.source_lifecycles != [
                    SourceLifecycle.TRANSIENT,
                    SourceLifecycle.RETAINED,
                ]:
                    raise ValueError(
                        "storage_reference carries transient and retained sources only as "
                        "the reviewed lifecycle matrix declares"
                    )
                if not contract.same_region_required:
                    raise ValueError(
                        "storage_reference requires a same-region private staging contract "
                        "(same_region_required=true)"
                    )
        return self


class StorageTransferContract(BaseModel):
    """A re-verified cloud-storage-side fact recorded in the fixture.

    Covers the S3 presigned-URL and GCS lifecycle documentation the release
    depends on (Scope §6.1 checkbox 1); the application never talks to these
    services through a new SDK in this checkpoint, so the record is
    documentation-only but still validated (non-empty, https sources).
    """

    key: str = Field(min_length=1)
    verified_at: date
    sources: dict[str, str] = Field(min_length=1)
    notes: str = Field(min_length=1)

    @field_validator("sources")
    @classmethod
    def _sources_are_https(cls, sources: dict[str, str]) -> dict[str, str]:
        for name, url in sources.items():
            if not url.startswith("https://"):
                raise ValueError(f"storage contract source {name!r} must be an https URL")
        return sources


class TransferContracts(BaseModel):
    """The full checked-in contract fixture (``app/ai/contracts/providers.yaml``)."""

    providers: dict[str, ProviderTransferContract] = Field(default_factory=dict)
    storage: dict[str, StorageTransferContract] = Field(default_factory=dict)


#: The storage-side facts every release must keep re-verified.
REQUIRED_STORAGE_CONTRACTS = frozenset({"s3_presigned_url", "gcs_lifecycle"})

#: The provider contract fixture path relative to this module's package root.
CONTRACTS_DIRECTORY_NAME = "contracts"
CONTRACTS_FILENAME = "providers.yaml"


def load_transfer_contracts(root: Path | None = None) -> TransferContracts:
    """Load and validate the checked-in provider transfer contract fixture.

    Parses ``app/ai/contracts/providers.yaml`` with PyYAML's safe loader and
    rejects any inconsistent declaration (unknown provider or mode, MIME types
    outside the reviewed sets, thresholds/ceilings out of bounds, missing or
    unordered expiry/TTL bounds, lifecycle mismatch or missing same-region
    storage-reference contract) with an actionable error, so invalid reviewed
    configuration fails at startup and in CI (Scope §6.1 checkbox 4).
    """
    ai_root = root or Path(__file__).resolve().parent
    path = ai_root / CONTRACTS_DIRECTORY_NAME / CONTRACTS_FILENAME
    if not path.is_file():
        raise RegistryValidationError(f"provider transfer contract fixture is missing: {path}")
    if path.stat().st_size > 256 * 1024:
        raise RegistryValidationError(f"provider transfer contract fixture is too large: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RegistryValidationError(
            f"cannot read provider transfer contract fixture: {path}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise RegistryValidationError(
            f"provider transfer contract fixture must be a mapping: {path}"
        )
    raw_mapping = cast(Mapping[str, object], raw)
    # The fixture lists providers naturally (like the model registry); index
    # them by provider id so the typed model can enforce key/field agreement.
    providers_value = raw_mapping.get("providers")
    if isinstance(providers_value, list):
        indexed: dict[str, object] = {}
        entries = cast(list[object], providers_value)
        for entry_value in entries:
            if not isinstance(entry_value, Mapping):
                raise RegistryValidationError(
                    f"provider transfer contract fixture entries must name a provider: {path}"
                )
            entry = cast(Mapping[str, object], entry_value)
            provider_id = entry.get("provider")
            if not isinstance(provider_id, str):
                raise RegistryValidationError(
                    f"provider transfer contract fixture provider ids must be strings: {path}"
                )
            if provider_id in indexed:
                raise RegistryValidationError(f"duplicate provider contract: {provider_id}")
            indexed[provider_id] = dict(entry)
        normalized: dict[str, object] = {**raw_mapping, "providers": indexed}
    else:
        normalized = dict(raw_mapping)
    try:
        contracts = TransferContracts.model_validate(normalized)
    except ValidationError as exc:
        raise RegistryValidationError(
            f"invalid provider transfer contract fixture {path}: {_error_message(exc)}"
        ) from exc
    validate_transfer_contracts(contracts)
    return contracts


def validate_transfer_contracts(contracts: TransferContracts) -> None:
    """Cross-check the whole fixture for inconsistent declarations.

    Runs after per-model validation: every provider id must be a known adapter
    id, every storage contract the release relies on must be present, and
    providers that declare document transfer capability must declare at least
    one reviewed mode. Raises :class:`RegistryValidationError` on the first
    inconsistency.
    """
    if not contracts.providers:
        raise RegistryValidationError("provider transfer contract fixture declares no providers")
    unknown_providers = set(contracts.providers) - set(AI_KNOWN_PROVIDER_IDS)
    if unknown_providers:
        raise RegistryValidationError(
            f"provider transfer contracts for unknown providers: {sorted(unknown_providers)}"
        )
    for provider_id, contract in contracts.providers.items():
        if contract.provider != provider_id:
            raise RegistryValidationError(
                f"provider contract key {provider_id!r} does not match its provider field"
            )
        if contract.transfer_modes and TransferMode.INLINE not in contract.transfer_modes:
            raise RegistryValidationError(
                f"provider {provider_id!r} declares transfer modes without inline; "
                "inline must remain the eligible default through the aggregate threshold"
            )
    missing_storage = REQUIRED_STORAGE_CONTRACTS - set(contracts.storage)
    if missing_storage:
        raise RegistryValidationError(
            f"required storage contracts missing from fixture: {sorted(missing_storage)}"
        )


def select_transfer_mode(
    *,
    aggregate_bytes: int,
    source_lifecycle: SourceLifecycle,
    allowed_modes: Iterable[TransferMode],
    contract: ProviderTransferContract,
    inline_threshold_bytes: int = INLINE_AGGREGATE_THRESHOLD_BYTES,
) -> TransferMode | None:
    """Deterministically select the eligible transfer mode, or ``None``.

    Default-deny selection (Scope §2.2, §5.2): ``inline`` wins whenever the
    aggregate raw attachment bytes are at or below the inline threshold (the
    deployment's ``ai_inline_aggregate_threshold_bytes``, which defaults to
    the 5,000,000-byte template constant) and inline is both allowed and
    supported. Above the threshold a transient source prefers
    ``provider_upload`` and a retained source prefers ``managed_signed_url``;
    ``storage_reference`` (Vertex GCS staging) serves either lifecycle when the
    provider declares it. A mode is eligible only when the caller's allowed
    set, the provider's reviewed contract, the source lifecycle and the byte
    ceiling all allow it; returning ``None`` means the caller must fail before
    any external transfer — never silently downgrade to a less private mode.

    ``inline_threshold_bytes`` is the *authoritative* inline boundary: inline
    is eligible only at or below it, so a lowered deployment threshold makes
    inline ineligible above it even when the provider's fixed inline contract
    ceiling would still fit — the configured threshold decides, never the
    provider contract (v0.8 Scope §5.2, §6.2).
    """
    allowed = list(allowed_modes)
    contracts_by_mode = contract.transfer_modes

    def _eligible(mode: TransferMode) -> bool:
        mode_contract = contracts_by_mode.get(mode)
        return (
            mode_contract is not None
            and source_lifecycle in mode_contract.source_lifecycles
            and aggregate_bytes <= mode_contract.max_bytes
        )

    if (
        aggregate_bytes <= inline_threshold_bytes
        and TransferMode.INLINE in allowed
        and _eligible(TransferMode.INLINE)
    ):
        return TransferMode.INLINE
    if source_lifecycle is SourceLifecycle.TRANSIENT:
        priority = (TransferMode.PROVIDER_UPLOAD, TransferMode.STORAGE_REFERENCE)
    else:
        priority = (TransferMode.MANAGED_SIGNED_URL, TransferMode.STORAGE_REFERENCE)
    for mode in priority:
        if mode in allowed and _eligible(mode):
            return mode
    # Above the deployment threshold inline is ineligible by definition: the
    # configured boundary, not the provider's inline contract ceiling, decides
    # where the default mode stops being selectable (Scope §2.2).
    return None


def select_transfer_mode_for_policy(
    *,
    aggregate_bytes: int,
    attachment_mime_types: Sequence[str],
    attachment_sizes: Sequence[int] | None = None,
    source_lifecycle: SourceLifecycle,
    organisation_allowed_modes: Iterable[TransferMode],
    organisation_max_large_attachment_bytes: int | None,
    task_allowed_modes: Iterable[TransferMode],
    model_allowed_modes: Iterable[TransferMode],
    model_mode_limits: Mapping[TransferMode, ModelModeCeiling] | None = None,
    model_inline: ModelInlineCeiling | None = None,
    deployment: TransferDeploymentPolicy | None = None,
    contract: ProviderTransferContract,
) -> TransferMode | None:
    """Deterministic mode selection intersected with every policy gate.

    v0.8 Scope §2.2/§6.2: a non-inline mode is eligible only when the source
    lifecycle, the task declaration, the organisation policy, the routed
    model/provider capability and the deployment configuration all allow it.
    This function intersects the four mode allowlists (organisation, task,
    model, deployment) and applies the lowest of the organisation ceiling, the
    deployment ceiling, the routed model's per-mode ceiling and the provider
    contract's own per-mode ceiling, then delegates the deterministic priority
    to :func:`select_transfer_mode`. An organisation with the default
    ``inline``-only policy can never reach a non-inline mode, a deployment
    that enables no non-inline mode can never either, and a request above the
    inline threshold whose intersection leaves no eligible mode returns
    ``None`` — the caller must fail before any external transfer (never
    silently downgrade to a less private mode).

    ``organisation_max_large_attachment_bytes`` ``None`` means the template
    ceiling applies unchanged (no organisation-level tightening); a configured
    value caps every non-inline mode, so a request above the organisation's
    ceiling can never silently ride a provider mode the organisation did not
    authorise. ``deployment`` carries the typed deployment configuration; when
    ``None`` the template defaults apply (inline only at the 5,000,000-byte
    threshold).

    ``attachment_mime_types`` and ``model_mode_limits`` gate the v0.8 large
    path (Scope §2.1 decision 3, §5.3): above the inline threshold, once a
    non-inline mode is a candidate, the request must be exactly one
    ``application/pdf`` and must fit the routed model's per-mode MIME set and
    byte ceiling, or no mode is eligible. Every request — including one at or
    below the threshold — passes through the full intersection: inline is
    selectable only when every allowlist and the provider contract allow it,
    and the configured threshold decides where inline stops being eligible,
    never the provider's inline contract ceiling (§5.2, §6.2).

    ``model_inline`` (with ``attachment_sizes``) additionally gates the inline
    path on the routed model's own inline declarations — MIME set, per-file
    and combined byte ceilings, documents/vision capabilities — so a model
    that cannot carry the set inline can never receive an inline dispatch,
    even when one of its non-inline modes happens to fit the set (Scope §6.2).
    Both must be supplied together; when either is absent the model-inline
    gate is skipped (backward-compatible callers).
    """
    allowed = set(organisation_allowed_modes) & set(task_allowed_modes) & set(model_allowed_modes)
    inline_threshold = (
        deployment.inline_aggregate_threshold_bytes
        if deployment is not None
        else INLINE_AGGREGATE_THRESHOLD_BYTES
    )
    if deployment is not None:
        allowed &= deployment.allowed_transfer_modes
    if (
        model_inline is not None
        and attachment_sizes is not None
        and not model_inline.can_carry(sizes=attachment_sizes, mime_types=attachment_mime_types)
    ):
        # The model cannot carry this set inline: exclude inline from the
        # eligible set so the early inline-required check below fails closed
        # for a small set and ``select_transfer_mode`` can never return inline
        # for a model whose inline MIME/byte limits the set violates.
        allowed = {mode for mode in allowed if mode is not TransferMode.INLINE}
    if TransferMode.INLINE not in allowed and aggregate_bytes <= inline_threshold:
        # Inline is the reviewed default and must remain eligible through the
        # aggregate threshold; an intersection without it is misconfigured and
        # must fail closed rather than skip to a non-inline mode for a small
        # file (Scope §2.2 "inline remains the default").
        return None
    if (
        organisation_max_large_attachment_bytes is not None
        and organisation_max_large_attachment_bytes > MAX_LARGE_ATTACHMENT_BYTES
    ):
        raise ValueError(
            f"organisation_max_large_attachment_bytes must not exceed {MAX_LARGE_ATTACHMENT_BYTES}"
        )
    if organisation_max_large_attachment_bytes is not None:
        allowed = {
            mode
            for mode in allowed
            if mode is TransferMode.INLINE
            or aggregate_bytes <= organisation_max_large_attachment_bytes
        }
    if deployment is not None:
        allowed = {
            mode
            for mode in allowed
            if mode is TransferMode.INLINE
            or aggregate_bytes <= deployment.max_large_attachment_bytes
        }
    if (
        any(mode is not TransferMode.INLINE for mode in allowed)
        and aggregate_bytes > inline_threshold
    ):
        # v0.8 large path (Scope §2.1 decision 3, §5.3): exactly one PDF. Any
        # other count or MIME type above the inline threshold has no eligible
        # non-inline mode, so the caller fails before any external transfer.
        # Below the threshold the shape gate is irrelevant: inline is the only
        # mode ``select_transfer_mode`` can return there (when allowed), so a
        # small multi-file or non-PDF set must not be rejected.
        if len(attachment_mime_types) != 1 or attachment_mime_types[0] not in NON_INLINE_MIME_TYPES:
            return None
        if model_mode_limits is not None:
            allowed = {
                mode
                for mode in allowed
                if mode is TransferMode.INLINE
                or (
                    (ceiling := model_mode_limits.get(mode)) is not None
                    and set(attachment_mime_types) <= ceiling.mime_types
                    and aggregate_bytes <= ceiling.max_bytes
                )
            }
    return select_transfer_mode(
        aggregate_bytes=aggregate_bytes,
        source_lifecycle=source_lifecycle,
        allowed_modes=allowed,
        contract=contract,
        inline_threshold_bytes=inline_threshold,
    )


def source_lifecycle_for_reference(reference: str, organisation_id: UUID) -> SourceLifecycle:
    """Classify one private storage reference by its source lifecycle.

    v0.8 Scope §2.2: transient sources live in the organisation-scoped AI
    scratch namespace (v0.7 Scope §6.5 item 4); every other feature-owned
    reference in private storage is retained. The classification is a property
    of the object's namespace, never of the caller's request.
    """
    prefix = SCRATCH_KEY_TEMPLATE.format(organisation_id=organisation_id)
    if reference.startswith(prefix):
        return SourceLifecycle.TRANSIENT
    return SourceLifecycle.RETAINED


def derive_idempotency_key(
    *,
    provider: str,
    mode: TransferMode,
    organisation_id: UUID,
    logical_request_id: str,
    source_digest: str,
    region: str,
) -> str:
    """The structural idempotency key for one logical transfer.

    v0.8 Scope §2.1/§2.3, §6.3: derived, never caller-supplied. Retries of one
    logical request reconstruct the same key, while a changed provider, mode,
    organisation, digest or region creates a different key and therefore a new
    transfer. The key is the SHA-256 of the exact reuse predicate
    (provider, mode, organisation, logical request, digest, region), so the
    durable ``ai_attachment_references`` row, the :class:`TransferStore`
    implementations and the managed-URL reuse path can never drift about which
    transfer a retry reuses. The provider is part of the key because a store
    instance is provider-specific; including it keeps the derived key
    collision-free even where stores share a namespace.
    """
    raw = (
        f"{provider}|{mode.value}|{organisation_id}|{logical_request_id}|{source_digest}|{region}"
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _error_message(exc: ValidationError) -> str:
    """Format a pydantic error summary without echoing provider content."""
    return "; ".join(
        f"{'.'.join(str(location) for location in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )


__all__ = [
    "CONTRACTS_DIRECTORY_NAME",
    "CONTRACTS_FILENAME",
    "INLINE_AGGREGATE_THRESHOLD_BYTES",
    "MANAGED_URL_DEFAULT_TTL_SECONDS",
    "MANAGED_URL_MAX_TTL_SECONDS",
    "MAX_LARGE_ATTACHMENT_BYTES",
    "MAX_PROVIDER_UPLOAD_EXPIRY_SECONDS",
    "NON_INLINE_MIME_TYPES",
    "REQUIRED_STORAGE_CONTRACTS",
    "SCRATCH_KEY_TEMPLATE",
    "ManagedUrlTtlContract",
    "ModelInlineCeiling",
    "ModelModeCeiling",
    "ProviderTransferContract",
    "ProviderUploadLifecycle",
    "SourceLifecycle",
    "StorageTransferContract",
    "TransferContracts",
    "TransferDeploymentPolicy",
    "TransferMode",
    "TransferModeContract",
    "UploadExpiryContract",
    "derive_idempotency_key",
    "load_transfer_contracts",
    "select_transfer_mode",
    "select_transfer_mode_for_policy",
    "source_lifecycle_for_reference",
    "validate_transfer_contracts",
]
