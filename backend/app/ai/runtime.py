"""Process-wide :class:`AIService` wiring (v0.7 Scope §6.6, ADR-0017).

``get_ai_service`` is the single place the checked-in application assembles the
provider-neutral :class:`~app.ai.service.AIService` from its checked-in parts:
the validated task/prompt/model registry bundle (Scope §6.2), the enabled
provider adapters from typed settings (Scope §6.3 factory), and the private
storage reference resolver (Scope §6.4). Both the demonstration feature service
and the ``ai.execute`` Dramatiq job consume this one instance so the routing,
attachment, redaction and policy configuration can never drift between the
synchronous and the durable paths.

The instance is process-wide and session-neutral: organisation policy, budget
reservation, request/output persistence and audit are owned by the
session-bound :class:`~app.ai.persistence.service.AIPersistencePortImpl` each
caller constructs per request/job attempt and passes to ``execute`` as the
``recorder`` port (BP §11 — the service owns transaction boundaries, and no
transaction ever spans provider I/O). Provider credentials and raw provider
configuration never leave this module (ADR-0017).

A redaction hook is an optional deployment choice (Scope §6.4); the template
default wires ``None`` (no redaction) so the seam exists without inventing a
policy a derived application would have to undo.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from app.ai.providers.factory import get_provider_factory
from app.ai.providers.gcs_managed_url import GcsManagedUrlStager
from app.ai.providers.openai_upload import OpenAITransferStore
from app.ai.providers.vertex_gcs import GcsTransferStore
from app.ai.registry import load_registry_bundle
from app.ai.service import AIService
from app.ai.staging import TransferStore
from app.ai.storage_resolver import StorageAttachmentResolver
from app.ai.transfer import TransferDeploymentPolicy, TransferMode
from app.ai.transfer_orchestrator import ManagedUrlStager
from app.core.config import get_settings
from app.core.endpoint_safety import is_private_http_host
from app.storage import get_storage


def _transfer_deployment_policy() -> TransferDeploymentPolicy:
    """Close the typed deployment transfer settings over the executor.

    v0.8 Scope §2.2/§6.2: the process-wide service must select transfer modes
    from the exact deployment it boots with — the enabled non-inline modes, the
    aggregate inline threshold and the large-file ceiling — so routing can
    never drift from configuration. The mode ids come from the typed settings;
    the validation lives in ``app/ai/deployment.py`` at construction time.
    """
    settings = get_settings()
    return TransferDeploymentPolicy(
        inline_aggregate_threshold_bytes=settings.ai_inline_aggregate_threshold_bytes,
        max_large_attachment_bytes=settings.ai_max_large_attachment_bytes,
        enabled_transfer_modes=frozenset(
            TransferMode(mode_id) for mode_id in settings.ai_enabled_transfer_modes
        ),
        managed_url_ttl_seconds=settings.ai_managed_url_ttl_seconds,
    )


def _transfer_stores() -> dict[str, TransferStore]:
    """Build the provider-neutral transfer stores the deployment enables.

    v0.8 Scope §2.4/§6.4-§6.5: the ``storage_reference`` mode is served by the
    private same-region GCS staging adapter, and the ``provider_upload`` mode
    for OpenAI by the Files API upload store (purpose ``user_data``, configured
    ``expires_after``). Each store is built only when the deployment enabled
    the mode and the owning provider is enabled (the typed-settings validator
    already required the mode's configuration); a deployment that enables no
    non-inline mode builds no store, so selection fails closed exactly where
    the policy says it must.
    """
    settings = get_settings()
    deployment = _transfer_deployment_policy()
    stores: dict[str, TransferStore] = {}
    if (
        TransferMode.STORAGE_REFERENCE in deployment.enabled_transfer_modes
        and "vertex" in settings.ai_enabled_providers
    ):
        stores["vertex"] = GcsTransferStore(
            project=settings.ai_vertex_project,
            location=settings.ai_vertex_location,
            bucket=settings.ai_vertex_temp_gcs_bucket,
            credentials_path=settings.ai_vertex_credentials_path,
            timeout_seconds=settings.ai_http_timeout_seconds,
        )
    if (
        TransferMode.PROVIDER_UPLOAD in deployment.enabled_transfer_modes
        and "openai" in settings.ai_enabled_providers
    ):
        stores["openai"] = OpenAITransferStore(
            api_key=settings.ai_openai_api_key,
            base_url=settings.ai_openai_base_url,
            region=settings.ai_openai_region,
            upload_expiry_seconds=settings.ai_upload_expiry_seconds,
            timeout_seconds=settings.ai_http_timeout_seconds,
        )
    return stores


def _endpoint_is_local(endpoint: str) -> bool:
    """Whether a presign endpoint is plain HTTP on a loopback/private host."""
    if not endpoint:
        return False
    parsed = urlsplit(endpoint)
    return parsed.scheme == "http" and is_private_http_host(parsed.hostname or "")


def _storage_is_local() -> bool:
    """Whether the storage seam presigns against a plain-HTTP local/private host.

    The S3 adapter presigns against ``storage_public_endpoint_url`` when set
    (the host the provider can reach), otherwise against
    ``storage_endpoint_url``. A scheme of ``http`` on a loopback/private host
    means the minted URL would be rejected by the HTTPS-only minter and
    unreachable by any cloud provider — the managed-URL path must stage
    through GCS instead (v0.8 Scope §2.3, §6.4/§6.5).
    """
    settings = get_settings()
    presign_endpoint = settings.storage_public_endpoint_url or settings.storage_endpoint_url
    return _endpoint_is_local(presign_endpoint)


def _managed_url_stager() -> ManagedUrlStager | None:
    """Build the dev managed-URL staging seam when the storage cannot presign
    an HTTPS URL a provider could reach (v0.8 Scope §2.3, §6.4/§6.5).

    With local MinIO in development, a retained >5 MB source is staged into
    the user-provisioned GCS temp bucket (the same one Vertex ``storage_reference``
    uses, with the deployer's ``age = 1`` lifecycle backstop) and the provider
    receives a GCS v4 RSA-signed HTTPS URL. The stager is built only when the
    deployment actually enables the managed-signed-url mode — a local storage
    seam without the mode never pays for the wiring — and then the Vertex GCS
    configuration is required (fail fast at startup, BP §27). With a public
    HTTPS storage no stager is built and URLs are minted directly, so no copy
    is ever made.
    """
    if not _storage_is_local():
        return None
    if TransferMode.MANAGED_SIGNED_URL not in _transfer_deployment_policy().enabled_transfer_modes:
        return None
    settings = get_settings()
    return GcsManagedUrlStager(
        project=settings.ai_vertex_project,
        location=settings.ai_vertex_location,
        bucket=settings.ai_vertex_temp_gcs_bucket,
        credentials_path=settings.ai_vertex_credentials_path,
        timeout_seconds=settings.ai_http_timeout_seconds,
    )


@lru_cache
def get_ai_service() -> AIService:
    """Return the process-wide, fully wired :class:`AIService`.

    Builds the enabled-provider map from the typed-settings factory, loads the
    checked-in registry bundle (validated at import), wires the
    storage-backed attachment resolver and closes the deployment-level
    transfer policy over the executor. Exactly one provider must be enabled —
    the deterministic fake adapter under test, one or more real adapters in a
    configured deployment — or :class:`AIService` itself rejects construction.
    """
    bundle = load_registry_bundle()
    factory = get_provider_factory()
    providers = {
        provider_id: factory.create(provider_id) for provider_id in factory.enabled_provider_ids
    }
    return AIService(
        task_registry=bundle.tasks,
        prompt_registry=bundle.prompts,
        model_registry=bundle.models,
        providers=providers,
        attachment_resolver=StorageAttachmentResolver(get_storage()),
        transfer_deployment=_transfer_deployment_policy(),
        storage=get_storage(),
        transfer_stores=_transfer_stores(),
        managed_url_stager=_managed_url_stager(),
    )
