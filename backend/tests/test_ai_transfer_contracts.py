"""Transfer/reference contract tests (v0.8 Scope §6.1 checkbox 4).

Covers the provider-neutral transfer contracts, the checked-in provider
contract fixture, deterministic mode selection and the fake staging
implementation. The central property is default-deny consistency: registry and
contract declarations fail fast on any inconsistent mode, source lifecycle,
MIME, threshold/ceiling, provider, expiry/TTL or regional declaration, so
invalid reviewed configuration can never reach a dispatch (Scope §2.2, §6.1).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from app.ai.registry import (
    Capability,
    CapabilityCostModelRegistry,
    FilePromptRegistry,
    FileTaskRegistry,
    ModelDefinition,
    NonInlineModeLimit,
    PricingBasis,
    PromptDefinition,
    QualityTier,
    RegistryBundle,
    RegistryValidationError,
    TaskDefinition,
    validate_registry_bundle,
)
from app.ai.staging import (
    ExternalFileReference,
    ExternalReferenceStatus,
    FakeTransferStore,
)
from app.ai.transfer import (
    INLINE_AGGREGATE_THRESHOLD_BYTES,
    MANAGED_URL_DEFAULT_TTL_SECONDS,
    MANAGED_URL_MAX_TTL_SECONDS,
    MAX_LARGE_ATTACHMENT_BYTES,
    NON_INLINE_MIME_TYPES,
    REQUIRED_STORAGE_CONTRACTS,
    ManagedUrlTtlContract,
    ModelInlineCeiling,
    ModelModeCeiling,
    ProviderTransferContract,
    ProviderUploadLifecycle,
    SourceLifecycle,
    StorageTransferContract,
    TransferContracts,
    TransferDeploymentPolicy,
    TransferMode,
    TransferModeContract,
    UploadExpiryContract,
    load_transfer_contracts,
    select_transfer_mode,
    select_transfer_mode_for_policy,
    source_lifecycle_for_reference,
    validate_transfer_contracts,
)

_ORG_ID = UUID("01989f1c-e5cb-7000-8000-000000000001")
_INLINE_MIME = sorted(
    {
        "application/json",
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)


def _upload_expiry() -> UploadExpiryContract:
    return UploadExpiryContract(min_seconds=3_600, default_seconds=3_600, max_seconds=2_592_000)


def _url_ttl() -> ManagedUrlTtlContract:
    return ManagedUrlTtlContract(
        default_seconds=MANAGED_URL_DEFAULT_TTL_SECONDS, max_seconds=MANAGED_URL_MAX_TTL_SECONDS
    )


def _provider_contract(
    provider: str = "fake",
    *,
    modes: dict[TransferMode, TransferModeContract] | None = None,
    verified_at: date = date(2026, 8, 11),
    api_version: str = "reviewed contract",
    sources: dict[str, str] | None = None,
    retention_notes: str = "retained until deleted; deletion supported",
    regional_notes: str = "regional caveats verified against official sources",
) -> ProviderTransferContract:
    return ProviderTransferContract(
        provider=provider,
        verified_at=verified_at,
        api_version=api_version,
        sources=sources or {"docs": "https://example.com/docs"},
        retention_notes=retention_notes,
        regional_notes=regional_notes,
        transfer_modes=modes
        or {
            TransferMode.INLINE: TransferModeContract(
                mime_types=_INLINE_MIME,
                max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
            ),
            TransferMode.PROVIDER_UPLOAD: TransferModeContract(
                mime_types=sorted(NON_INLINE_MIME_TYPES),
                max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT],
                upload_lifecycle=ProviderUploadLifecycle.EXPIRES_AFTER,
                upload_expiry=_upload_expiry(),
            ),
            TransferMode.MANAGED_SIGNED_URL: TransferModeContract(
                mime_types=sorted(NON_INLINE_MIME_TYPES),
                max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                source_lifecycles=[SourceLifecycle.RETAINED],
                managed_url_ttl=_url_ttl(),
            ),
            TransferMode.STORAGE_REFERENCE: TransferModeContract(
                mime_types=sorted(NON_INLINE_MIME_TYPES),
                max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                same_region_required=True,
            ),
        },
    )


def _storage_contract(key: str) -> StorageTransferContract:
    return StorageTransferContract(
        key=key,
        verified_at=date(2026, 8, 11),
        sources={"docs": "https://example.com/docs"},
        notes="verified storage-side facts",
    )


def _contracts() -> TransferContracts:
    return TransferContracts(
        providers={"fake": _provider_contract()},
        storage={key: _storage_contract(key) for key in REQUIRED_STORAGE_CONTRACTS},
    )


# --- checked-in fixture -----------------------------------------------------


def test_checked_in_fixture_covers_every_provider_and_required_storage_fact() -> None:
    contracts = load_transfer_contracts()
    assert set(contracts.providers) == {
        "fake",
        "openai",
        "anthropic",
        "deepseek",
        "azure_openai",
        "vertex",
        "local",
    }
    assert set(contracts.storage) >= REQUIRED_STORAGE_CONTRACTS
    for contract in contracts.providers.values():
        assert contract.verified_at == date(2026, 8, 11)
        assert contract.api_version
        assert contract.sources
    # The reviewed provider ceilings are lower than the template ceiling and
    # always win (Scope §2.1): Anthropic's 32 MB request-payload ceiling is
    # below the template's 50 MB ceiling.
    anthropic = contracts.providers["anthropic"].transfer_modes[TransferMode.PROVIDER_UPLOAD]
    assert anthropic.max_bytes == 32_000_000 < MAX_LARGE_ATTACHMENT_BYTES
    # Anthropic's lifecycle is delete-only: files persist until explicit
    # DELETE, so the fixture records the retention kind and no expiry bounds
    # (Scope §6.1 checkbox 1), while OpenAI keeps automatic-expiry bounds.
    assert anthropic.upload_lifecycle is ProviderUploadLifecycle.UNTIL_DELETED
    assert anthropic.upload_expiry is None
    openai = contracts.providers["openai"].transfer_modes[TransferMode.PROVIDER_UPLOAD]
    assert openai.upload_lifecycle is ProviderUploadLifecycle.EXPIRES_AFTER
    assert openai.upload_expiry is not None
    # Every provider records a regional caveat backed by a cited source.
    for provider_id, contract in contracts.providers.items():
        assert contract.regional_notes, provider_id
        assert contract.sources, provider_id


def test_checked_in_fixture_declares_no_non_inline_mode_for_fail_closed_providers() -> None:
    contracts = load_transfer_contracts()
    for provider_id in ("azure_openai", "deepseek", "local"):
        assert not any(
            mode is not TransferMode.INLINE
            for mode in contracts.providers[provider_id].transfer_modes
        ), provider_id
    # Vertex large files use private GCS staging only; the contract pins it to
    # the same region (Scope §2.4, §5.7).
    vertex = contracts.providers["vertex"].transfer_modes[TransferMode.STORAGE_REFERENCE]
    assert vertex.same_region_required is True


# --- deterministic mode selection (Scope §5.2) ------------------------------


def test_inline_is_selected_at_or_below_the_aggregate_threshold() -> None:
    contract = _provider_contract()
    for size in (1, INLINE_AGGREGATE_THRESHOLD_BYTES):
        assert (
            select_transfer_mode(
                aggregate_bytes=size,
                source_lifecycle=SourceLifecycle.TRANSIENT,
                allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
                contract=contract,
            )
            is TransferMode.INLINE
        )


def test_inline_is_not_selected_when_the_allowed_set_excludes_it() -> None:
    """The allowed-mode intersection gates inline too (Scope §2.2/§5.2).

    A small input whose allowed set excludes inline must never silently
    downgrade to inline; the preferred eligible non-inline mode wins instead,
    and ``None`` when none is eligible.
    """
    contract = _provider_contract()
    assert (
        select_transfer_mode(
            aggregate_bytes=1,
            source_lifecycle=SourceLifecycle.TRANSIENT,
            allowed_modes=[TransferMode.PROVIDER_UPLOAD],
            contract=contract,
        )
        is TransferMode.PROVIDER_UPLOAD
    )
    # A retained source with only managed_signed_url allowed also skips inline.
    assert (
        select_transfer_mode(
            aggregate_bytes=1,
            source_lifecycle=SourceLifecycle.RETAINED,
            allowed_modes=[TransferMode.MANAGED_SIGNED_URL],
            contract=contract,
        )
        is TransferMode.MANAGED_SIGNED_URL
    )
    # No allowed mode at all (not even inline): fail closed.
    assert (
        select_transfer_mode(
            aggregate_bytes=1,
            source_lifecycle=SourceLifecycle.TRANSIENT,
            allowed_modes=[],
            contract=contract,
        )
        is None
    )


def test_transient_source_prefers_provider_upload_above_the_threshold() -> None:
    assert (
        select_transfer_mode(
            aggregate_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES + 1,
            source_lifecycle=SourceLifecycle.TRANSIENT,
            allowed_modes=[
                TransferMode.INLINE,
                TransferMode.PROVIDER_UPLOAD,
                TransferMode.STORAGE_REFERENCE,
            ],
            contract=_provider_contract(),
        )
        is TransferMode.PROVIDER_UPLOAD
    )


def test_retained_source_prefers_managed_signed_url_above_the_threshold() -> None:
    assert (
        select_transfer_mode(
            aggregate_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES + 1,
            source_lifecycle=SourceLifecycle.RETAINED,
            allowed_modes=[
                TransferMode.INLINE,
                TransferMode.MANAGED_SIGNED_URL,
                TransferMode.STORAGE_REFERENCE,
            ],
            contract=_provider_contract(),
        )
        is TransferMode.MANAGED_SIGNED_URL
    )


def test_vertex_selects_storage_reference_for_either_lifecycle() -> None:
    contract = _provider_contract(
        modes={
            TransferMode.INLINE: TransferModeContract(
                mime_types=_INLINE_MIME,
                max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
            ),
            TransferMode.STORAGE_REFERENCE: TransferModeContract(
                mime_types=sorted(NON_INLINE_MIME_TYPES),
                max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                same_region_required=True,
            ),
        }
    )
    for lifecycle in (SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED):
        assert (
            select_transfer_mode(
                aggregate_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES + 1,
                source_lifecycle=lifecycle,
                allowed_modes=[TransferMode.INLINE, TransferMode.STORAGE_REFERENCE],
                contract=contract,
            )
            is TransferMode.STORAGE_REFERENCE
        )


def test_selection_fails_closed_when_no_mode_is_eligible() -> None:
    contract = _provider_contract()
    # Inline only, above the threshold → nothing eligible (no silent downgrade).
    assert (
        select_transfer_mode(
            aggregate_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES + 1,
            source_lifecycle=SourceLifecycle.RETAINED,
            allowed_modes=[TransferMode.INLINE],
            contract=contract,
        )
        is None
    )
    # The provider's ceiling always wins: above 50 MB nothing is eligible.
    assert (
        select_transfer_mode(
            aggregate_bytes=MAX_LARGE_ATTACHMENT_BYTES + 1,
            source_lifecycle=SourceLifecycle.TRANSIENT,
            allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            contract=contract,
        )
        is None
    )
    # A lifecycle a mode cannot carry never makes it eligible.
    assert (
        select_transfer_mode(
            aggregate_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES + 1,
            source_lifecycle=SourceLifecycle.TRANSIENT,
            allowed_modes=[TransferMode.INLINE, TransferMode.MANAGED_SIGNED_URL],
            contract=contract,
        )
        is None
    )


# --- policy-aware selection (v0.8 Scope §6.2) -------------------------------


def test_policy_selection_intersects_organisation_task_and_model() -> None:
    """The org policy is an additional default-deny gate, never a bypass."""
    contract = _provider_contract()
    above = INLINE_AGGREGATE_THRESHOLD_BYTES + 1

    # Org and task both allow provider_upload for a transient source → selected.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[
                TransferMode.INLINE,
                TransferMode.PROVIDER_UPLOAD,
            ],
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            model_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            contract=contract,
        )
        is TransferMode.PROVIDER_UPLOAD
    )
    # The org restricts to inline only → nothing is eligible above the threshold.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[TransferMode.INLINE],
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            model_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            contract=contract,
        )
        is None
    )
    # The task restricts to inline only → the org's broader allowlist cannot
    # silently widen the task's reviewed declaration.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[
                TransferMode.INLINE,
                TransferMode.PROVIDER_UPLOAD,
            ],
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=[TransferMode.INLINE],
            model_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            contract=contract,
        )
        is None
    )


def test_policy_selection_applies_organisation_max_large_ceiling() -> None:
    contract = _provider_contract()
    above = INLINE_AGGREGATE_THRESHOLD_BYTES + 1

    # The org tightens the ceiling below the request: no non-inline mode fits.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[
                TransferMode.INLINE,
                TransferMode.PROVIDER_UPLOAD,
            ],
            organisation_max_large_attachment_bytes=above - 1,
            task_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            model_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            contract=contract,
        )
        is None
    )
    # A ceiling at or above the request keeps provider_upload eligible.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[
                TransferMode.INLINE,
                TransferMode.PROVIDER_UPLOAD,
            ],
            organisation_max_large_attachment_bytes=above,
            task_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            model_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            contract=contract,
        )
        is TransferMode.PROVIDER_UPLOAD
    )


def test_policy_selection_retained_source_prefers_managed_url() -> None:
    contract = _provider_contract()
    above = INLINE_AGGREGATE_THRESHOLD_BYTES + 1
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.RETAINED,
            organisation_allowed_modes=[
                TransferMode.INLINE,
                TransferMode.MANAGED_SIGNED_URL,
            ],
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=[
                TransferMode.INLINE,
                TransferMode.MANAGED_SIGNED_URL,
            ],
            model_allowed_modes=[TransferMode.INLINE, TransferMode.MANAGED_SIGNED_URL],
            contract=contract,
        )
        is TransferMode.MANAGED_SIGNED_URL
    )


def test_policy_selection_vertex_storage_reference_for_either_lifecycle() -> None:
    contract = _provider_contract(
        modes={
            TransferMode.INLINE: TransferModeContract(
                mime_types=_INLINE_MIME,
                max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
            ),
            TransferMode.STORAGE_REFERENCE: TransferModeContract(
                mime_types=sorted(NON_INLINE_MIME_TYPES),
                max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                same_region_required=True,
            ),
        }
    )
    above = INLINE_AGGREGATE_THRESHOLD_BYTES + 1
    for lifecycle in (SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED):
        assert (
            select_transfer_mode_for_policy(
                aggregate_bytes=above,
                attachment_mime_types=["application/pdf"],
                source_lifecycle=lifecycle,
                organisation_allowed_modes=[TransferMode.INLINE, TransferMode.STORAGE_REFERENCE],
                organisation_max_large_attachment_bytes=None,
                task_allowed_modes=[TransferMode.INLINE, TransferMode.STORAGE_REFERENCE],
                model_allowed_modes=[TransferMode.INLINE, TransferMode.STORAGE_REFERENCE],
                contract=contract,
            )
            is TransferMode.STORAGE_REFERENCE
        )


def test_policy_selection_requires_inline_through_the_threshold() -> None:
    """An intersection without inline can never skip to non-inline for a small
    file — inline remains the reviewed default (Scope §2.2)."""
    contract = _provider_contract()
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=1,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[TransferMode.PROVIDER_UPLOAD],
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=[TransferMode.PROVIDER_UPLOAD],
            model_allowed_modes=[TransferMode.PROVIDER_UPLOAD],
            contract=contract,
        )
        is None
    )


def test_policy_selection_rejects_ceiling_above_template() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        select_transfer_mode_for_policy(
            aggregate_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES + 1,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            organisation_max_large_attachment_bytes=MAX_LARGE_ATTACHMENT_BYTES + 1,
            task_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            model_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            contract=_provider_contract(),
        )


def test_policy_selection_intersects_the_deployment_configuration() -> None:
    """Deployment configuration is an eligibility gate (v0.8 Scope §2.2/§6.2):
    a mode the deployment does not enable can never be selected, and the
    deployment ceiling caps every non-inline mode."""
    contract = _provider_contract()
    above = INLINE_AGGREGATE_THRESHOLD_BYTES + 1
    permissive_policy = [
        TransferMode.INLINE,
        TransferMode.PROVIDER_UPLOAD,
        TransferMode.STORAGE_REFERENCE,
    ]

    # Default-deny deployment (no non-inline mode enabled): nothing eligible.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=permissive_policy,
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=permissive_policy,
            model_allowed_modes=permissive_policy,
            deployment=TransferDeploymentPolicy(),
            contract=contract,
        )
        is None
    )
    # Enabling the mode in the deployment makes it eligible again.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=permissive_policy,
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=permissive_policy,
            model_allowed_modes=permissive_policy,
            deployment=TransferDeploymentPolicy(
                enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD})
            ),
            contract=contract,
        )
        is TransferMode.PROVIDER_UPLOAD
    )
    # A deployment that enables a mode but tightens the ceiling below the
    # request excludes that mode: the lowest ceiling wins.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=permissive_policy,
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=permissive_policy,
            model_allowed_modes=permissive_policy,
            deployment=TransferDeploymentPolicy(
                enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD}),
                max_large_attachment_bytes=above - 1,
            ),
            contract=contract,
        )
        is None
    )


def test_policy_selection_uses_the_deployment_inline_threshold() -> None:
    """A lowered deployment inline threshold moves the inline-only boundary:
    the fail-closed intersection check and the service gate use the configured
    threshold, never the template constant (v0.8 Scope §2.2)."""
    contract = _provider_contract()
    lowered = TransferDeploymentPolicy(
        inline_aggregate_threshold_bytes=1_000_000,
        enabled_transfer_modes=frozenset({TransferMode.PROVIDER_UPLOAD}),
    )
    # 2 MB is above the deployment threshold, so the policy intersection
    # applies; the mode is eligible and selected.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=2_000_000,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            model_allowed_modes=[TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD],
            deployment=lowered,
            contract=contract,
        )
        is TransferMode.PROVIDER_UPLOAD
    )
    # A tiny request with an inline-less intersection still fails closed at the
    # lowered boundary (below it inline is the only eligible mode).
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=500_000,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[TransferMode.PROVIDER_UPLOAD],
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=[TransferMode.PROVIDER_UPLOAD],
            model_allowed_modes=[TransferMode.PROVIDER_UPLOAD],
            deployment=lowered,
            contract=contract,
        )
        is None
    )


def test_policy_selection_inline_is_not_eligible_above_a_lowered_threshold() -> None:
    """The configured deployment threshold is the authoritative inline
    boundary (v0.8 Scope §2.2/§5.2): above a lowered threshold an inline-only
    intersection fails closed rather than falling back to inline whenever the
    provider's fixed inline contract ceiling would still fit."""
    contract = _provider_contract()
    lowered = TransferDeploymentPolicy(inline_aggregate_threshold_bytes=1_000_000)
    # 2,000,000 bytes: above the lowered 1,000,000-byte deployment threshold,
    # still within the provider's 5,000,000-byte inline contract ceiling —
    # inline must not be selected.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=2_000_000,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[TransferMode.INLINE],
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=[TransferMode.INLINE],
            model_allowed_modes=[TransferMode.INLINE],
            deployment=lowered,
            contract=contract,
        )
        is None
    )
    # At or below the lowered threshold inline is still the reviewed default.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=1_000_000,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=[TransferMode.INLINE],
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=[TransferMode.INLINE],
            model_allowed_modes=[TransferMode.INLINE],
            deployment=lowered,
            contract=contract,
        )
        is TransferMode.INLINE
    )


def test_policy_selection_requires_exactly_one_pdf_for_non_inline() -> None:
    """v0.8 Scope §2.1 decision 3 / §5.3: the non-inline path carries exactly
    one application/pdf; any other count or MIME type above the threshold has
    no eligible mode."""
    contract = _provider_contract()
    above = INLINE_AGGREGATE_THRESHOLD_BYTES + 1
    permissive = [TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD]

    for bad_mimes in (
        ["application/pdf", "application/pdf"],
        ["text/plain"],
        ["application/json"],
    ):
        assert (
            select_transfer_mode_for_policy(
                aggregate_bytes=above,
                attachment_mime_types=bad_mimes,
                source_lifecycle=SourceLifecycle.TRANSIENT,
                organisation_allowed_modes=permissive,
                organisation_max_large_attachment_bytes=None,
                task_allowed_modes=permissive,
                model_allowed_modes=permissive,
                contract=contract,
            )
            is None
        )
    # Exactly one PDF keeps the mode eligible.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=permissive,
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=permissive,
            model_allowed_modes=permissive,
            contract=contract,
        )
        is TransferMode.PROVIDER_UPLOAD
    )


def test_policy_selection_applies_the_model_per_mode_ceiling() -> None:
    """The routed model's per-mode MIME set and byte ceiling gate selection:
    a request above a model-specific mode ceiling (or outside its MIME set)
    has no eligible mode (v0.8 Scope §2.2)."""
    contract = _provider_contract()
    above = INLINE_AGGREGATE_THRESHOLD_BYTES + 1
    permissive = [TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD]
    tight_limits = {
        TransferMode.PROVIDER_UPLOAD: ModelModeCeiling(
            mime_types=frozenset(NON_INLINE_MIME_TYPES), max_bytes=above - 1
        )
    }

    # The model's ceiling is below the request: the mode is excluded.
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=permissive,
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=permissive,
            model_allowed_modes=permissive,
            model_mode_limits=tight_limits,
            contract=contract,
        )
        is None
    )
    # The model's MIME set excludes PDF: the mode is excluded.
    no_pdf_limits = {
        TransferMode.PROVIDER_UPLOAD: ModelModeCeiling(
            mime_types=frozenset({"text/plain"}), max_bytes=MAX_LARGE_ATTACHMENT_BYTES
        )
    }
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=permissive,
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=permissive,
            model_allowed_modes=permissive,
            model_mode_limits=no_pdf_limits,
            contract=contract,
        )
        is None
    )
    # A ceiling at or above the request keeps the mode eligible.
    roomy_limits = {
        TransferMode.PROVIDER_UPLOAD: ModelModeCeiling(
            mime_types=frozenset(NON_INLINE_MIME_TYPES), max_bytes=MAX_LARGE_ATTACHMENT_BYTES
        )
    }
    assert (
        select_transfer_mode_for_policy(
            aggregate_bytes=above,
            attachment_mime_types=["application/pdf"],
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=permissive,
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=permissive,
            model_allowed_modes=permissive,
            model_mode_limits=roomy_limits,
            contract=contract,
        )
        is TransferMode.PROVIDER_UPLOAD
    )


def test_policy_selection_gates_inline_on_the_model_inline_declaration() -> None:
    """v0.8 Scope §6.2: the routed model's inline declarations gate the inline
    path. A model whose inline MIME set excludes the attachment, whose
    per-file or combined byte ceilings do not fit, or that lacks the vision
    capability for an image can never receive an inline dispatch — even when
    one of its non-inline modes happens to accept the set — so a
    below-threshold request fails closed instead of bypassing the declaration.
    """
    contract = _provider_contract()
    permissive = [TransferMode.INLINE, TransferMode.PROVIDER_UPLOAD]

    def _select(
        *,
        sizes: list[int],
        mimes: list[str],
        inline: ModelInlineCeiling,
    ) -> TransferMode | None:
        return select_transfer_mode_for_policy(
            aggregate_bytes=sum(sizes),
            attachment_sizes=sizes,
            attachment_mime_types=mimes,
            source_lifecycle=SourceLifecycle.TRANSIENT,
            organisation_allowed_modes=permissive,
            organisation_max_large_attachment_bytes=None,
            task_allowed_modes=permissive,
            model_allowed_modes=permissive,
            model_mode_limits={
                TransferMode.PROVIDER_UPLOAD: ModelModeCeiling(
                    mime_types=frozenset(NON_INLINE_MIME_TYPES),
                    max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                )
            },
            model_inline=inline,
            contract=contract,
        )

    pdf_mime = ["application/pdf"]
    # The model's inline MIME set excludes the PDF: inline is excluded and a
    # below-threshold request has no eligible mode — the non-inline declaration
    # that accepts PDF cannot turn into an inline dispatch.
    text_only = ModelInlineCeiling(
        mime_types=frozenset({"text/plain"}),
        max_attachment_bytes=5 * 1024 * 1024,
        max_total_attachment_bytes=10 * 1024 * 1024,
        has_documents_capability=True,
    )
    assert _select(sizes=[100_000], mimes=pdf_mime, inline=text_only) is None

    # A fitting inline declaration keeps inline selectable below the threshold.
    pdf_inline = ModelInlineCeiling(
        mime_types=frozenset({"application/pdf"}),
        max_attachment_bytes=5 * 1024 * 1024,
        max_total_attachment_bytes=10 * 1024 * 1024,
        has_documents_capability=True,
    )
    assert _select(sizes=[100_000], mimes=pdf_mime, inline=pdf_inline) is TransferMode.INLINE

    # A per-file ceiling below the file size excludes inline for the same set.
    tight_per_file = ModelInlineCeiling(
        mime_types=frozenset({"application/pdf"}),
        max_attachment_bytes=50_000,
        max_total_attachment_bytes=10 * 1024 * 1024,
        has_documents_capability=True,
    )
    assert _select(sizes=[100_000], mimes=pdf_mime, inline=tight_per_file) is None

    # A combined ceiling below the set excludes inline as well.
    tight_total = ModelInlineCeiling(
        mime_types=frozenset({"application/pdf"}),
        max_attachment_bytes=10 * 1024 * 1024,
        max_total_attachment_bytes=60_000,
        has_documents_capability=True,
    )
    assert _select(sizes=[100_000], mimes=pdf_mime, inline=tight_total) is None

    # A model without the documents capability can never carry the set inline.
    no_documents = ModelInlineCeiling(
        mime_types=frozenset({"application/pdf"}),
        max_attachment_bytes=5 * 1024 * 1024,
        max_total_attachment_bytes=10 * 1024 * 1024,
        has_documents_capability=False,
    )
    assert _select(sizes=[100_000], mimes=pdf_mime, inline=no_documents) is None

    # An image additionally needs the vision capability on the model's inline
    # declaration; with it, inline stays selectable below the threshold.
    image = ["image/png"]
    image_no_vision = ModelInlineCeiling(
        mime_types=frozenset({"image/png"}),
        max_attachment_bytes=5 * 1024 * 1024,
        max_total_attachment_bytes=10 * 1024 * 1024,
        has_documents_capability=True,
        has_vision_capability=False,
    )
    assert _select(sizes=[100_000], mimes=image, inline=image_no_vision) is None
    image_with_vision = ModelInlineCeiling(
        mime_types=frozenset({"image/png"}),
        max_attachment_bytes=5 * 1024 * 1024,
        max_total_attachment_bytes=10 * 1024 * 1024,
        has_documents_capability=True,
        has_vision_capability=True,
    )
    assert _select(sizes=[100_000], mimes=image, inline=image_with_vision) is TransferMode.INLINE


def test_source_lifecycle_for_reference_classifies_scratch_as_transient() -> None:
    assert (
        source_lifecycle_for_reference(
            f"organisations/{_ORG_ID}/ai/scratch/analyse-input.pdf", _ORG_ID
        )
        is SourceLifecycle.TRANSIENT
    )
    assert (
        source_lifecycle_for_reference(f"organisations/{_ORG_ID}/documents/kept.pdf", _ORG_ID)
        is SourceLifecycle.RETAINED
    )
    # Another organisation's scratch namespace is retained from this org's view.
    other = UUID("01989f1c-e5cb-7000-8000-000000000002")
    assert (
        source_lifecycle_for_reference(f"organisations/{_ORG_ID}/ai/scratch/x.pdf", other)
        is SourceLifecycle.RETAINED
    )


# --- fixture consistency: every inconsistency fails fast ---------------------


def test_inline_contract_must_match_the_aggregate_threshold() -> None:
    with pytest.raises(ValidationError, match="aggregate threshold"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES + 1,
                    source_lifecycles=[SourceLifecycle.TRANSIENT],
                )
            }
        )


def test_non_inline_contract_carries_exactly_one_pdf() -> None:
    bad_mime = TransferModeContract(
        mime_types=["application/pdf", "text/plain"],
        max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
        source_lifecycles=[SourceLifecycle.TRANSIENT],
        upload_expiry=_upload_expiry(),
    )
    with pytest.raises(ValidationError, match="exactly one PDF"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.PROVIDER_UPLOAD: bad_mime,
            }
        )


def test_non_inline_ceiling_must_be_above_threshold_and_at_most_50mb() -> None:
    too_small = TransferModeContract(
        mime_types=sorted(NON_INLINE_MIME_TYPES),
        max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
        source_lifecycles=[SourceLifecycle.TRANSIENT],
        upload_expiry=_upload_expiry(),
    )
    with pytest.raises(ValidationError, match="above the"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.PROVIDER_UPLOAD: too_small,
            }
        )
    too_large = TransferModeContract(
        mime_types=sorted(NON_INLINE_MIME_TYPES),
        max_bytes=MAX_LARGE_ATTACHMENT_BYTES + 1,
        source_lifecycles=[SourceLifecycle.TRANSIENT],
        upload_expiry=_upload_expiry(),
    )
    with pytest.raises(ValidationError):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.PROVIDER_UPLOAD: too_large,
            }
        )


def test_provider_upload_declares_a_retention_kind_and_bounds_match_it() -> None:
    no_lifecycle = TransferModeContract(
        mime_types=sorted(NON_INLINE_MIME_TYPES),
        max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
        source_lifecycles=[SourceLifecycle.TRANSIENT],
        upload_expiry=_upload_expiry(),
    )
    with pytest.raises(ValidationError, match="upload_lifecycle"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.PROVIDER_UPLOAD: no_lifecycle,
            }
        )
    # expires_after retention without recorded bounds is inconsistent.
    expires_without_bounds = TransferModeContract(
        mime_types=sorted(NON_INLINE_MIME_TYPES),
        max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
        source_lifecycles=[SourceLifecycle.TRANSIENT],
        upload_lifecycle=ProviderUploadLifecycle.EXPIRES_AFTER,
    )
    with pytest.raises(ValidationError, match="upload_expiry bounds"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.PROVIDER_UPLOAD: expires_without_bounds,
            }
        )
    # until_deleted retention must not invent expiry bounds (Anthropic's
    # delete-only lifecycle, Scope §6.1 checkbox 1).
    until_deleted_with_bounds = TransferModeContract(
        mime_types=sorted(NON_INLINE_MIME_TYPES),
        max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
        source_lifecycles=[SourceLifecycle.TRANSIENT],
        upload_lifecycle=ProviderUploadLifecycle.UNTIL_DELETED,
        upload_expiry=_upload_expiry(),
    )
    with pytest.raises(ValidationError, match="must not declare upload_expiry"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.PROVIDER_UPLOAD: until_deleted_with_bounds,
            }
        )
    # A delete-only provider contract without expiry bounds validates and
    # carries no expiry to orchestrate (the no-provider-expiry case).
    until_deleted = TransferModeContract(
        mime_types=sorted(NON_INLINE_MIME_TYPES),
        max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
        source_lifecycles=[SourceLifecycle.TRANSIENT],
        upload_lifecycle=ProviderUploadLifecycle.UNTIL_DELETED,
    )
    contract = _provider_contract(
        modes={
            TransferMode.INLINE: TransferModeContract(
                mime_types=_INLINE_MIME,
                max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
            ),
            TransferMode.PROVIDER_UPLOAD: until_deleted,
        }
    )
    mode_contract = contract.transfer_modes[TransferMode.PROVIDER_UPLOAD]
    assert mode_contract.upload_lifecycle is ProviderUploadLifecycle.UNTIL_DELETED
    assert mode_contract.upload_expiry is None
    wrong_lifecycle = TransferModeContract(
        mime_types=sorted(NON_INLINE_MIME_TYPES),
        max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
        source_lifecycles=[SourceLifecycle.RETAINED],
        upload_lifecycle=ProviderUploadLifecycle.EXPIRES_AFTER,
        upload_expiry=_upload_expiry(),
    )
    with pytest.raises(ValidationError, match="transient sources only"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.PROVIDER_UPLOAD: wrong_lifecycle,
            }
        )


def test_expiry_bounds_must_be_ordered_and_within_the_template_ceiling() -> None:
    with pytest.raises(ValidationError, match="min_seconds <= default_seconds"):
        UploadExpiryContract(min_seconds=3_600, default_seconds=100, max_seconds=2_592_000)
    with pytest.raises(ValidationError, match="max_seconds"):
        UploadExpiryContract(min_seconds=3_600, default_seconds=3_600, max_seconds=9_000_000_000)


def test_managed_signed_url_requires_ttl_bounds_and_retained_lifecycle() -> None:
    no_ttl = TransferModeContract(
        mime_types=sorted(NON_INLINE_MIME_TYPES),
        max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
        source_lifecycles=[SourceLifecycle.RETAINED],
    )
    with pytest.raises(ValidationError, match="managed_url_ttl"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.MANAGED_SIGNED_URL: no_ttl,
            }
        )
    with pytest.raises(ValidationError, match="retained sources only"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.MANAGED_SIGNED_URL: TransferModeContract(
                    mime_types=sorted(NON_INLINE_MIME_TYPES),
                    max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT],
                    managed_url_ttl=_url_ttl(),
                ),
            }
        )


def test_managed_url_ttl_is_reviewed_and_bounded() -> None:
    with pytest.raises(ValidationError, match="default_seconds"):
        ManagedUrlTtlContract(default_seconds=1_800, max_seconds=900)
    with pytest.raises(ValidationError, match="must not exceed"):
        ManagedUrlTtlContract(
            default_seconds=MANAGED_URL_DEFAULT_TTL_SECONDS,
            max_seconds=MANAGED_URL_MAX_TTL_SECONDS + 1,
        )
    with pytest.raises(ValidationError, match="must be 900"):
        ManagedUrlTtlContract(default_seconds=1, max_seconds=1_800)


def test_inline_must_carry_both_source_lifecycles() -> None:
    # The reviewed lifecycle matrix pins inline to both lifecycles: it is the
    # universal default mode, so a contract omitting transient or retained
    # silently narrows the default (Scope §6.1 checkbox 4).
    for only in ([SourceLifecycle.TRANSIENT], [SourceLifecycle.RETAINED]):
        with pytest.raises(ValidationError, match="inline carries"):
            _provider_contract(
                modes={
                    TransferMode.INLINE: TransferModeContract(
                        mime_types=_INLINE_MIME,
                        max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                        source_lifecycles=only,
                    )
                }
            )


def test_storage_reference_must_carry_both_source_lifecycles() -> None:
    # Pinned to the reviewed Vertex/fake contract (Scope §2.2): private GCS
    # staging serves both lifecycles, so a contract omitting either side
    # fails (Scope §6.1 checkbox 4).
    for only in ([SourceLifecycle.TRANSIENT], [SourceLifecycle.RETAINED]):
        with pytest.raises(ValidationError, match="storage_reference carries"):
            _provider_contract(
                modes={
                    TransferMode.INLINE: TransferModeContract(
                        mime_types=_INLINE_MIME,
                        max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                        source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                    ),
                    TransferMode.STORAGE_REFERENCE: TransferModeContract(
                        mime_types=sorted(NON_INLINE_MIME_TYPES),
                        max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                        source_lifecycles=only,
                        same_region_required=True,
                    ),
                }
            )


def test_regional_notes_are_a_required_declaration() -> None:
    # A provider can no longer omit the required regional caveat and still
    # validate (Scope §6.1 checkbox 4).
    with pytest.raises(ValidationError):
        _provider_contract(regional_notes="")


def test_storage_reference_requires_a_same_region_contract() -> None:
    with pytest.raises(ValidationError, match="same_region"):
        _provider_contract(
            modes={
                TransferMode.INLINE: TransferModeContract(
                    mime_types=_INLINE_MIME,
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
                TransferMode.STORAGE_REFERENCE: TransferModeContract(
                    mime_types=sorted(NON_INLINE_MIME_TYPES),
                    max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                    source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                ),
            }
        )


def test_fixture_cross_checks_reject_unknown_providers_and_drift() -> None:
    unknown = _contracts()
    unknown.providers["mystery"] = _provider_contract("mystery")
    with pytest.raises(RegistryValidationError, match="unknown providers"):
        validate_transfer_contracts(unknown)

    missing_storage = _contracts()
    missing_storage.storage.pop("gcs_lifecycle")
    with pytest.raises(RegistryValidationError, match="storage contracts missing"):
        validate_transfer_contracts(missing_storage)

    no_inline = _contracts()
    no_inline.providers["fake"] = _provider_contract(
        modes={
            TransferMode.PROVIDER_UPLOAD: TransferModeContract(
                mime_types=sorted(NON_INLINE_MIME_TYPES),
                max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT],
                upload_lifecycle=ProviderUploadLifecycle.EXPIRES_AFTER,
                upload_expiry=_upload_expiry(),
            )
        }
    )
    with pytest.raises(RegistryValidationError, match="without inline"):
        validate_transfer_contracts(no_inline)

    mismatched = _contracts()
    mismatched.providers["fake"] = _provider_contract("openai")
    with pytest.raises(RegistryValidationError, match="does not match"):
        validate_transfer_contracts(mismatched)


def test_storage_sources_must_be_https() -> None:
    with pytest.raises(ValidationError, match="https"):
        StorageTransferContract(
            key="s3_presigned_url",
            verified_at=date(2026, 8, 11),
            sources={"docs": "http://insecure.example.com"},
            notes="notes",
        )


# --- registry declarations fail on inconsistency -----------------------------


def _pricing() -> PricingBasis:
    return PricingBasis(
        currency="USD",
        input_price_per_million_tokens=Decimal(1),
        output_price_per_million_tokens=Decimal(2),
        effective_date=date(2026, 8, 10),
        owner="tests",
    )


def _model(
    model_id: str = "fake.document-classifier",
    *,
    provider: str = "fake",
    capabilities: list[Capability] | None = None,
    allowed_transfer_modes: list[TransferMode] | None = None,
    transfer_mode_limits: dict[TransferMode, NonInlineModeLimit] | None = None,
) -> ModelDefinition:
    values: dict[str, object] = {
        "id": model_id,
        "provider": provider,
        "model": f"provider-{model_id}",
        "capabilities": capabilities or [Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
        "context_window": 16_384,
        "supported_parameters": ["max_tokens", "temperature"],
        "quality_tier": QualityTier.ECONOMY,
        "latency_tier": "interactive",
        "priority": 100,
        "max_attachment_bytes": 5_242_880,
        "max_total_attachment_bytes": 10_485_760,
        "attachment_mime_types": sorted(NON_INLINE_MIME_TYPES),
        "pricing": _pricing(),
    }
    if allowed_transfer_modes is not None:
        values["allowed_transfer_modes"] = allowed_transfer_modes
    if transfer_mode_limits is not None:
        values["transfer_mode_limits"] = transfer_mode_limits
    return ModelDefinition.model_validate(values)


def _limits(max_bytes: int = MAX_LARGE_ATTACHMENT_BYTES) -> dict[TransferMode, NonInlineModeLimit]:
    return {
        TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
            mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=max_bytes
        ),
        TransferMode.MANAGED_SIGNED_URL: NonInlineModeLimit(
            mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=max_bytes
        ),
        TransferMode.STORAGE_REFERENCE: NonInlineModeLimit(
            mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=max_bytes
        ),
    }


def _prompt() -> PromptDefinition:
    return PromptDefinition(
        name="document.classify",
        version=1,
        system_instructions="Static.",
        input_variables=["text"],
        user_template="{text}",
        output_contract="app.ai.tasks.schemas.DocumentClassificationResult",
    )


def _task(**updates: object) -> TaskDefinition:
    values: dict[str, object] = {
        "name": "document.classify",
        "prompt_name": "document.classify",
        "prompt_version": 1,
        "input_variables": ["text"],
        "required_capabilities": [Capability.STRUCTURED_OUTPUT, Capability.DOCUMENTS],
        "parameter_defaults": {"max_tokens": 100, "temperature": 0},
        "output_schema": "app.ai.tasks.schemas.DocumentClassificationResult",
        "quality_tier": QualityTier.ECONOMY,
        "latency_tier": "interactive",
        "max_input_tokens": 1000,
    }
    values.update(updates)
    return TaskDefinition.model_validate(values)


def test_task_declares_a_non_empty_unique_transfer_mode_set() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        _task(allowed_transfer_modes=[])
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        _task(allowed_transfer_modes=[TransferMode.INLINE, TransferMode.INLINE])


def test_model_non_inline_declarations_are_a_complete_contract() -> None:
    # A non-inline mode without per-mode limits is incomplete.
    with pytest.raises(ValidationError, match="must declare per-mode"):
        _model(allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD])
    # Stray limits without a non-inline mode are inconsistent.
    with pytest.raises(ValidationError, match="require a non-inline"):
        _model(
            transfer_mode_limits={
                TransferMode.PROVIDER_UPLOAD: _limits()[TransferMode.PROVIDER_UPLOAD]
            }
        )
    # Limits may only name non-inline modes: inline ceilings come from the
    # v0.7 attachment fields.
    with pytest.raises(ValidationError, match="non-inline modes only"):
        _model(
            allowed_transfer_modes=[TransferMode.INLINE],
            transfer_mode_limits={
                TransferMode.INLINE: NonInlineModeLimit(
                    mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=10_000_000
                )
            },
        )
    # A limit entry must name a mode the model actually allows.
    with pytest.raises(ValidationError, match="not in allowed_transfer_modes"):
        _model(
            allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
            transfer_mode_limits=_limits(),
        )
    # Every allowed non-inline mode needs its own per-mode limits entry.
    with pytest.raises(ValidationError, match="must declare per-mode"):
        _model(
            allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD, TransferMode.STORAGE_REFERENCE],
            transfer_mode_limits={
                TransferMode.PROVIDER_UPLOAD: _limits()[TransferMode.PROVIDER_UPLOAD]
            },
        )
    # The v0.8 large path is exactly one PDF.
    with pytest.raises(ValidationError, match="exactly one PDF"):
        _model(
            allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
            transfer_mode_limits={
                TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                    mime_types=["application/pdf", "text/plain"], max_bytes=10_000_000
                )
            },
        )
    # Each mode's ceiling must sit above the inline threshold.
    with pytest.raises(ValidationError, match="above the"):
        _model(
            allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
            transfer_mode_limits={
                TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                    mime_types=sorted(NON_INLINE_MIME_TYPES),
                    max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                )
            },
        )
    # Each mode's ceiling is capped by the template ceiling.
    with pytest.raises(ValidationError):
        _model(
            allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
            transfer_mode_limits={
                TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                    mime_types=sorted(NON_INLINE_MIME_TYPES),
                    max_bytes=MAX_LARGE_ATTACHMENT_BYTES + 1,
                )
            },
        )
    # Non-inline input is still document input.
    with pytest.raises(ValidationError, match="documents capability"):
        _model(
            capabilities=[Capability.STRUCTURED_OUTPUT],
            allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
            transfer_mode_limits={
                TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                    mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=10_000_000
                )
            },
        )


def _bundle(
    models: list[ModelDefinition], tasks: list[TaskDefinition] | None = None
) -> RegistryBundle:
    return RegistryBundle(
        tasks=FileTaskRegistry(tasks or [_task()]),
        prompts=FilePromptRegistry([_prompt()]),
        models=CapabilityCostModelRegistry(models),
    )


def test_bundle_rejects_non_inline_declarations_the_provider_contract_cannot_support() -> None:
    upload_limits = {
        TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
            mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=10_000_000
        )
    }
    # A model whose provider has no transfer contract at all: strip the fake
    # provider contract so the declaration has nothing to validate against.
    stripped = _contracts()
    stripped.providers.pop("fake")
    with MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.ai.registry.load_transfer_contracts", lambda: stripped)
        bundle = _bundle(
            [
                _model(
                    "fake-upload",
                    provider="fake",
                    allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
                    transfer_mode_limits=upload_limits,
                )
            ]
        )
        with pytest.raises(RegistryValidationError, match="no provider transfer contract"):
            validate_registry_bundle(bundle)

    # A model declaring a mode the provider's reviewed contract does not offer
    # (this variant of the fake contract carries no provider_upload).
    vertex_only = _contracts()
    vertex_only.providers["fake"] = _provider_contract(
        modes={
            TransferMode.INLINE: TransferModeContract(
                mime_types=_INLINE_MIME,
                max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
            ),
            TransferMode.STORAGE_REFERENCE: TransferModeContract(
                mime_types=sorted(NON_INLINE_MIME_TYPES),
                max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                same_region_required=True,
            ),
        }
    )
    with MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.ai.registry.load_transfer_contracts", lambda: vertex_only)
        bundle = _bundle(
            [
                _model(
                    "fake-upload",
                    provider="fake",
                    allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
                    transfer_mode_limits=upload_limits,
                )
            ]
        )
        with pytest.raises(RegistryValidationError, match="does not support"):
            validate_registry_bundle(bundle)


def test_bundle_rejects_mime_and_ceiling_drift_beyond_the_provider_contract() -> None:
    # Anthropic's provider_upload carries PDF and 32 MB; declaring 50 MB drifts
    # past the provider ceiling and must fail (Scope §2.1: provider wins).
    with MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.ai.registry.load_transfer_contracts", load_transfer_contracts)
        bundle = _bundle(
            [
                _model(
                    "anthropic-oversized",
                    provider="anthropic",
                    allowed_transfer_modes=[TransferMode.PROVIDER_UPLOAD],
                    transfer_mode_limits={
                        TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
                            mime_types=sorted(NON_INLINE_MIME_TYPES),
                            max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                        )
                    },
                )
            ]
        )
        with pytest.raises(RegistryValidationError, match="above the provider ceiling"):
            validate_registry_bundle(bundle)


def test_bundle_checks_each_mode_against_its_own_contract_ceiling() -> None:
    # Mode-specific drift: the provider contract's ceilings differ per mode, so
    # a model that fits one mode but drifts in another fails only the drifting
    # mode (Scope §6.1 checkbox 4: per-mode limits preserved).
    differing = _contracts()
    differing.providers["fake"] = _provider_contract(
        modes={
            TransferMode.INLINE: TransferModeContract(
                mime_types=_INLINE_MIME,
                max_bytes=INLINE_AGGREGATE_THRESHOLD_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
            ),
            TransferMode.PROVIDER_UPLOAD: TransferModeContract(
                mime_types=sorted(NON_INLINE_MIME_TYPES),
                max_bytes=40_000_000,
                source_lifecycles=[SourceLifecycle.TRANSIENT],
                upload_lifecycle=ProviderUploadLifecycle.EXPIRES_AFTER,
                upload_expiry=_upload_expiry(),
            ),
            TransferMode.STORAGE_REFERENCE: TransferModeContract(
                mime_types=sorted(NON_INLINE_MIME_TYPES),
                max_bytes=MAX_LARGE_ATTACHMENT_BYTES,
                source_lifecycles=[SourceLifecycle.TRANSIENT, SourceLifecycle.RETAINED],
                same_region_required=True,
            ),
        }
    )
    limits_fit = {
        TransferMode.PROVIDER_UPLOAD: NonInlineModeLimit(
            mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=40_000_000
        ),
        TransferMode.STORAGE_REFERENCE: NonInlineModeLimit(
            mime_types=sorted(NON_INLINE_MIME_TYPES), max_bytes=MAX_LARGE_ATTACHMENT_BYTES
        ),
    }
    with MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.ai.registry.load_transfer_contracts", lambda: differing)
        bundle = _bundle(
            [
                _model(
                    "fake-fit",
                    provider="fake",
                    allowed_transfer_modes=[
                        TransferMode.PROVIDER_UPLOAD,
                        TransferMode.STORAGE_REFERENCE,
                    ],
                    transfer_mode_limits=limits_fit,
                )
            ]
        )
        validate_registry_bundle(bundle)
        # Same model declarations, provider contract storage ceiling lowered:
        # only the storage_reference mode drifts, so validation fails that mode
        # specifically while the still-fitting provider_upload ceiling stays.
        differing.providers["fake"].transfer_modes[
            TransferMode.STORAGE_REFERENCE
        ].max_bytes = 30_000_000
        with pytest.raises(RegistryValidationError, match="storage_reference"):
            validate_registry_bundle(bundle)


def test_bundle_rejects_a_task_mode_no_registered_model_can_realise() -> None:
    with MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("app.ai.registry.load_transfer_contracts", load_transfer_contracts)
        bundle = _bundle(
            [_model()],
            tasks=[_task(allowed_transfer_modes=[TransferMode.STORAGE_REFERENCE])],
        )
        with pytest.raises(RegistryValidationError, match="no registered model supports"):
            validate_registry_bundle(bundle)


def test_checked_in_bundle_with_fake_non_inline_modes_is_consistent() -> None:
    """The checked-in registry + fixture stay green with the fake model's
    non-inline declarations (Scope §2.4 default test suite)."""
    from app.ai.registry import load_registry_bundle

    load_registry_bundle()


# --- fake staging store (Scope §2.4) ----------------------------------------


class _NoExpirySentinel:
    """Marker for 'the caller did not supply an expiry' (distinct from None)."""


_NO_EXPIRY = _NoExpirySentinel()


async def _stage(
    store: FakeTransferStore,
    *,
    mode: TransferMode = TransferMode.PROVIDER_UPLOAD,
    organisation_id: UUID = _ORG_ID,
    logical_request_id: str = "request-1",
    source_digest: str = "a" * 64,
    source_lifecycle: SourceLifecycle = SourceLifecycle.TRANSIENT,
    region: str = "us",
    size_bytes: int = 6_000_000,
    expires_at: datetime | _NoExpirySentinel | None = _NO_EXPIRY,
) -> ExternalFileReference:
    # ``_NO_EXPIRY`` is the sentinel: an explicit ``None`` passes through to
    # the store as the no-provider-expiry case, while the default stages a
    # one-hour expiry so most tests exercise the expiry path.
    if expires_at is _NO_EXPIRY:
        expires_at = datetime.now(UTC) + timedelta(hours=1)
    # ``_NoExpirySentinel`` is only a default marker; by this point the value
    # is always a datetime or an explicit None (the no-provider-expiry case).
    resolved_expiry = cast("datetime | None", expires_at)
    return await store.stage(
        mode=mode,
        organisation_id=organisation_id,
        logical_request_id=logical_request_id,
        source_reference=f"organisations/{organisation_id}/documents/f1/original.pdf",
        source_digest=source_digest,
        mime_type="application/pdf",
        size_bytes=size_bytes,
        source_lifecycle=source_lifecycle,
        region=region,
        expires_at=resolved_expiry,
    )


async def test_fake_stage_is_deterministic_and_idempotent_within_one_logical_request() -> None:
    store = FakeTransferStore()
    first = await _stage(store)
    second = await _stage(store)
    assert first.external_id == second.external_id
    assert first.is_live
    assert second.is_live
    assert first.idempotency_key == second.idempotency_key
    assert len(store.records) == 1


async def test_fake_reuse_is_scoped_to_one_logical_request() -> None:
    store = FakeTransferStore()
    staged = await _stage(store)
    reusable = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORG_ID,
        logical_request_id="request-1",
        source_digest="a" * 64,
        region="us",
    )
    assert reusable is not None
    assert reusable.external_id == staged.external_id
    # A distinct logical request never reuses the reference (Scope §2.1).
    assert (
        await store.find_reusable(
            mode=TransferMode.PROVIDER_UPLOAD,
            organisation_id=_ORG_ID,
            logical_request_id="request-2",
            source_digest="a" * 64,
            region="us",
        )
        is None
    )
    # A changed digest or region creates a new transfer, not a reuse.
    assert (
        await store.find_reusable(
            mode=TransferMode.PROVIDER_UPLOAD,
            organisation_id=_ORG_ID,
            logical_request_id="request-1",
            source_digest="b" * 64,
            region="us",
        )
        is None
    )
    # Cross-organisation reuse is denied.
    assert (
        await store.find_reusable(
            mode=TransferMode.PROVIDER_UPLOAD,
            organisation_id=UUID("01989f1c-e5cb-7000-8000-000000000099"),
            logical_request_id="request-1",
            source_digest="a" * 64,
            region="us",
        )
        is None
    )


async def test_fake_expiry_makes_a_reference_unusable() -> None:
    store = FakeTransferStore()
    staged = await _stage(store, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert staged.is_live is False
    assert store.expire_due() >= 1
    assert (
        await store.find_reusable(
            mode=TransferMode.PROVIDER_UPLOAD,
            organisation_id=_ORG_ID,
            logical_request_id="request-1",
            source_digest="a" * 64,
            region="us",
        )
        is None
    )
    # A fresh idempotent transfer replaces the expired one.
    replacement = await _stage(store, expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert replacement.external_id != staged.external_id


async def test_fake_no_provider_expiry_stays_live_until_terminal_delete() -> None:
    # The no-provider-expiry case (Anthropic delete-only lifecycle): a
    # reference staged without ``expires_at`` remains live and reusable until
    # terminal deletion (Scope §6.1 checkbox 1; reconciliation is the only
    # removal path for delete-only providers).
    store = FakeTransferStore()
    staged = await _stage(store, expires_at=None)
    assert staged.expires_at is None
    assert staged.is_live
    reusable = await store.find_reusable(
        mode=TransferMode.PROVIDER_UPLOAD,
        organisation_id=_ORG_ID,
        logical_request_id="request-1",
        source_digest="a" * 64,
        region="us",
    )
    assert reusable is not None
    assert reusable.external_id == staged.external_id
    assert store.expire_due() == 0
    await store.delete(staged)
    assert staged.status is ExternalReferenceStatus.DELETED


async def test_fake_delete_is_best_effort_and_never_touches_the_source() -> None:
    store = FakeTransferStore()
    staged = await _stage(store)
    await store.delete(staged)
    assert staged.status is ExternalReferenceStatus.DELETED
    assert staged.deleted_at is not None
    assert len(store.deleted) == 1
    assert (
        await store.find_reusable(
            mode=TransferMode.PROVIDER_UPLOAD,
            organisation_id=_ORG_ID,
            logical_request_id="request-1",
            source_digest="a" * 64,
            region="us",
        )
        is None
    )
    # The fake holds no source object; delete is a no-op on a missing record.
    await store.delete(
        ExternalFileReference(
            mode=TransferMode.PROVIDER_UPLOAD,
            provider="fake",
            external_id="missing",
            source_reference="organisations/x/f.pdf",
            source_digest="c" * 64,
            size_bytes=1,
            mime_type="application/pdf",
            source_lifecycle=SourceLifecycle.TRANSIENT,
            region="",
            organisation_id=_ORG_ID,
            logical_request_id="none",
            idempotency_key="missing",
            created_at=datetime.now(UTC),
        )
    )
