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

from app.ai.providers.factory import get_provider_factory
from app.ai.providers.vertex_gcs import GcsTransferStore
from app.ai.registry import load_registry_bundle
from app.ai.service import AIService
from app.ai.staging import TransferStore
from app.ai.storage_resolver import StorageAttachmentResolver
from app.ai.transfer import TransferDeploymentPolicy, TransferMode
from app.core.config import get_settings
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
    )


def _transfer_stores() -> dict[str, TransferStore]:
    """Build the provider-neutral transfer stores the deployment enables.

    v0.8 Scope §2.4/§6.4: the Vertex ``storage_reference`` mode is served by
    the private same-region GCS staging adapter. The store is built only when
    the deployment enabled the mode and the Vertex provider is enabled (the
    typed-settings validator already required the user-provisioned bucket); a
    deployment that enables no non-inline mode builds no store, so selection
    fails closed exactly where the policy says it must.
    """
    settings = get_settings()
    stores: dict[str, TransferStore] = {}
    if (
        TransferMode.STORAGE_REFERENCE in _transfer_deployment_policy().enabled_transfer_modes
        and "vertex" in settings.ai_enabled_providers
    ):
        stores["vertex"] = GcsTransferStore(
            project=settings.ai_vertex_project,
            location=settings.ai_vertex_location,
            bucket=settings.ai_vertex_temp_gcs_bucket,
            credentials_path=settings.ai_vertex_credentials_path,
            timeout_seconds=settings.ai_http_timeout_seconds,
        )
    return stores


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
    )
