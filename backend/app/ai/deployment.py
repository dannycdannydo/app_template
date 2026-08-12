"""Deployment-level transfer-mode configuration validation (v0.8 Scope §2.2, §6.2).

The typed deployment settings (``app/core/config.py``) enable non-inline
transfer modes, the aggregate inline threshold, the large-file template
ceiling, the provider upload expiry, the managed signed-URL TTL and the Vertex
staging bucket. Configuration must fail fast — in production and everywhere
else, at startup, never at request time (BP §27) — on incomplete or
incompatible declarations, *without* creating or configuring any cloud
infrastructure (the staging bucket is user-provisioned, Scope §2.2).

This module owns those cross-field checks. It lives inside ``app/ai/`` because
it names the reviewed transfer modes and consults the checked-in provider
contract fixture; the settings model imports it lazily inside its validator so
the ``app.core.config`` ↔ ``app.ai.transfer`` import boundary stays intact
(v0.8 Scope §6.1 import-boundary rule) and no module-load cycle is introduced.
"""

from __future__ import annotations

from app.ai.transfer import (
    INLINE_AGGREGATE_THRESHOLD_BYTES,
    MANAGED_URL_MAX_TTL_SECONDS,
    MAX_LARGE_ATTACHMENT_BYTES,
    MAX_PROVIDER_UPLOAD_EXPIRY_SECONDS,
    ProviderUploadLifecycle,
    TransferMode,
    load_transfer_contracts,
)

#: The non-inline transfer modes a deployment may enable (Scope §2.2).
NON_INLINE_TRANSFER_MODES = frozenset(
    {
        TransferMode.PROVIDER_UPLOAD,
        TransferMode.MANAGED_SIGNED_URL,
        TransferMode.STORAGE_REFERENCE,
    }
)


def validate_transfer_deployment(
    *,
    enabled_transfer_modes: list[str],
    enabled_providers: list[str],
    inline_aggregate_threshold_bytes: int,
    max_large_attachment_bytes: int,
    upload_expiry_seconds: int,
    managed_url_ttl_seconds: int,
    vertex_temp_gcs_bucket: str,
) -> None:
    """Fail fast on incomplete or incompatible transfer-mode configuration.

    Runs at settings construction time. ``enabled_transfer_modes`` must be a
    duplicate-free set of known non-inline mode ids; every enabled mode must be
    supportable by at least one enabled provider's reviewed contract (so an
    enabled mode with no capable provider is a configuration error, not a
    silent no-op); the ceiling bounds must satisfy the reviewed contract
    (inline threshold at or below 5,000,000, large-file ceiling above the
    threshold and at most 50,000,000, expiry within the 30-day ceiling, TTL at
    most 1,800 seconds); and ``storage_reference`` additionally requires the
    user-provisioned staging bucket. Mode-specific values must also satisfy
    every relevant enabled provider's reviewed bounds: a configured
    ``provider_upload`` expiry below an ``expires_after`` provider's documented
    minimum (or above its maximum) is a configuration error, and a managed
    signed-URL TTL outside a supporting provider's reviewed range is one too
    (Scope §2.2, §6.1 checkbox 1 — the provider contract always wins). Delete-
    only providers (``until_deleted``, e.g. Anthropic) impose no automatic
    expiry bound, so the expiry setting is irrelevant to them. Nothing here
    creates, configures or touches cloud infrastructure.
    """
    if len(set(enabled_transfer_modes)) != len(enabled_transfer_modes):
        raise ValueError("ai_enabled_transfer_modes must not contain duplicates")
    unknown = set(enabled_transfer_modes) - {mode.value for mode in NON_INLINE_TRANSFER_MODES}
    if unknown:
        raise ValueError(
            "ai_enabled_transfer_modes contains unknown non-inline transfer modes: "
            f"{sorted(unknown)}"
        )
    if not 1 <= inline_aggregate_threshold_bytes <= INLINE_AGGREGATE_THRESHOLD_BYTES:
        raise ValueError(
            "ai_inline_aggregate_threshold_bytes must be between 1 and "
            f"{INLINE_AGGREGATE_THRESHOLD_BYTES}"
        )
    if not 1 <= max_large_attachment_bytes <= MAX_LARGE_ATTACHMENT_BYTES:
        raise ValueError(
            f"ai_max_large_attachment_bytes must be between 1 and {MAX_LARGE_ATTACHMENT_BYTES}"
        )
    if enabled_transfer_modes and max_large_attachment_bytes <= inline_aggregate_threshold_bytes:
        raise ValueError(
            "ai_max_large_attachment_bytes must be above "
            "ai_inline_aggregate_threshold_bytes when a non-inline transfer "
            "mode is enabled"
        )
    if not 1 <= upload_expiry_seconds <= MAX_PROVIDER_UPLOAD_EXPIRY_SECONDS:
        raise ValueError(
            f"ai_upload_expiry_seconds must be between 1 and {MAX_PROVIDER_UPLOAD_EXPIRY_SECONDS}"
        )
    if not 1 <= managed_url_ttl_seconds <= MANAGED_URL_MAX_TTL_SECONDS:
        raise ValueError(
            f"ai_managed_url_ttl_seconds must be between 1 and {MANAGED_URL_MAX_TTL_SECONDS}"
        )

    contracts = load_transfer_contracts()
    supporting: dict[TransferMode, list[str]] = {}
    for mode_id in enabled_transfer_modes:
        mode = TransferMode(mode_id)
        supporting[mode] = [
            provider_id
            for provider_id in enabled_providers
            if mode in contracts.providers[provider_id].transfer_modes
        ]
        if not supporting[mode]:
            raise ValueError(
                f"transfer mode {mode_id!r} is enabled but no enabled provider "
                "declares support for it"
            )
    # Mode-specific values must satisfy every relevant enabled provider's
    # reviewed contract (Scope §2.2/§6.1): the *lowest* documented minimum and
    # the *highest* documented maximum across the supporting providers bound
    # the single deployment-wide setting, so one invalid combination can never
    # slip through because another provider happens to allow it.
    upload_providers = supporting.get(TransferMode.PROVIDER_UPLOAD, [])
    for provider_id in upload_providers:
        upload_mode = contracts.providers[provider_id].transfer_modes[TransferMode.PROVIDER_UPLOAD]
        if upload_mode.upload_lifecycle is not ProviderUploadLifecycle.EXPIRES_AFTER:
            # Delete-only providers (e.g. Anthropic) impose no automatic expiry
            # bound; the configured expiry is irrelevant to them (Scope §6.1).
            continue
        expiry_bounds = upload_mode.upload_expiry
        if expiry_bounds is not None and not (
            expiry_bounds.min_seconds <= upload_expiry_seconds <= expiry_bounds.max_seconds
        ):
            raise ValueError(
                "ai_upload_expiry_seconds must be within the reviewed "
                "provider_upload expiry bounds of enabled provider "
                f"{provider_id!r} ({expiry_bounds.min_seconds}.."
                f"{expiry_bounds.max_seconds} seconds); got {upload_expiry_seconds}"
            )
    ttl_providers = supporting.get(TransferMode.MANAGED_SIGNED_URL, [])
    for provider_id in ttl_providers:
        ttl_contract = (
            contracts.providers[provider_id]
            .transfer_modes[TransferMode.MANAGED_SIGNED_URL]
            .managed_url_ttl
        )
        if ttl_contract is not None and not (
            ttl_contract.default_seconds <= managed_url_ttl_seconds <= ttl_contract.max_seconds
        ):
            raise ValueError(
                "ai_managed_url_ttl_seconds must be within the reviewed managed "
                f"signed-URL TTL bounds of enabled provider {provider_id!r} "
                f"({ttl_contract.default_seconds}..{ttl_contract.max_seconds} "
                f"seconds); got {managed_url_ttl_seconds}"
            )
    if (
        TransferMode.STORAGE_REFERENCE.value in enabled_transfer_modes
        and not vertex_temp_gcs_bucket
    ):
        raise ValueError(
            "storage_reference requires the user-provisioned "
            "ai_vertex_temp_gcs_bucket staging bucket"
        )


__all__ = ["NON_INLINE_TRANSFER_MODES", "validate_transfer_deployment"]
