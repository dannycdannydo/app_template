"""AIService — the only application-facing entry point to the AI layer.

v0.7 Scope §6.1/§6.4, ADR-0017: application code calls
``AIService.execute(request: AIRequest) -> AIResult`` and names a task, never
a provider or model. The service resolves the task → prompt → model through
the registry interfaces, renders the prompt with a safe allowlisted renderer,
applies the optional redaction hook, resolves private storage references into
bounded attachments at the service boundary, dispatches through the provider
boundary, validates structured output against the task's Pydantic schema, and
returns a result with usage/cost/routing metadata. Provider SDKs never appear
here (BP §33, ADR-0017).

v0.7 Scope §6.4 (structured outputs, retry and safety controls): every result is
validated against the declared Pydantic model before it is returned. The
service supplies the JSON Schema it generated from that model to the adapter,
which requests native structured output where it truthfully supports it
(OpenAI ``json_schema``, Vertex ``responseJsonSchema``, Azure by pinned
api-version) and otherwise falls back to the documented JSON-mode prompt
contract. Malformed output triggers at most one bounded repair request, then
consumes bounded task retries; a transient provider failure inside the repair
consumes the same bounded retry budget. Transient provider failures retry
within the task's bounded ``max_attempts`` (using the router's region-safe
cross-provider fallback only when the task allows it); permanent
validation/policy failures never retry. Usage/cost aggregate every actual
attempt. Unvalidated structured data is never returned as success.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.attachments import ALLOWED_ATTACHMENT_MIME_TYPES, Attachment, validate_attachment_set
from app.ai.errors import (
    AIError,
    AIInputValidationError,
    AIRequestReplayError,
    AIUnavailableError,
    ModelNotAvailableError,
    OutputSchemaError,
    OutputValidationError,
    PromptNotFoundError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    TaskNotFoundError,
    TransferExecutionUnavailableError,
    TransferModeUnavailableError,
)
from app.ai.pdf_inspection import validate_pdf_page_limit
from app.ai.persistence.port import AIPersistencePort, OrganisationAIPolicy
from app.ai.persistence.references import TransferReferenceStore
from app.ai.providers.base import LLMProvider, ProviderRequest
from app.ai.registry import (
    ModelDefinition,
    ModelRegistry,
    PromptDefinition,
    PromptRegistry,
    RegistryValidationError,
    RoutingDecision,
    TaskDefinition,
    TaskRegistry,
    TransferRoutingContext,
    estimate_maximum_cost,
    estimate_tokens,
    model_inline_ceiling,
    model_transfer_mode_limits,
    resolve_output_schema,
)
from app.ai.schemas import AIRequest, AIResult, CostEstimate, RoutingMetadata, TokenUsage
from app.ai.staging import ExternalFileReference, StagedFile, TransferStore
from app.ai.storage_resolver import (
    EXTENSION_MIME_TYPES,
    AttachmentResolutionContext,
    AttachmentResolver,
)
from app.ai.streamed_source import StreamedSource
from app.ai.transfer import (
    NON_INLINE_MIME_TYPES,
    SourceLifecycle,
    TransferContracts,
    TransferDeploymentPolicy,
    TransferMode,
    load_transfer_contracts,
    select_transfer_mode_for_policy,
    source_lifecycle_for_reference,
)
from app.ai.transfer_orchestrator import ManagedUrlStager, TransferOrchestrator
from app.modules.audit.service import ACTION_AI_TRANSFER_SELECTED, record_event
from app.observability.metrics import (
    observe_ai_attempt,
    observe_ai_fallback,
    observe_ai_retry,
    observe_ai_transfer_selection,
    observe_ai_validation_failure,
)
from app.storage.base import ObjectStorage

#: Module logger. AI log lines bind ``ai_request_id``, task, provider/model,
#: prompt name/version and safe error codes only — never prompts, provider
#: responses, attachment bytes or retained input/output content (BP §28,
#: ADR-0017). The request middleware and worker entry points bind the
#: surrounding request/job context.
logger = structlog.get_logger()

SchemaResolver = Callable[[str], type[BaseModel]]


class RepairNotPossibleError(AIError):
    """A repair request cannot be dispatched within the task/model bounds.

    Raised when the enlarged repair prompt would exceed the task's context or
    cost ceilings (v0.7 Scope §6.4/§6.5). Terminal: retrying the identical
    malformed cycle cannot shrink the repair prompt, so no bounded task retry
    is attempted. Carries a safe message that never echoes provider output.
    """

    error_code = "repair_not_possible"


#: Optional pre-dispatch redaction hook (v0.7 Scope §6.4): applied to the request's
#: text and message content before the prompt is rendered, so sensitive input
#: never reaches the provider or the rendered prompt. Default is identity.
Redactor = Callable[[str], str]

#: Bounded context for one repair request (v0.7 Scope §6.4): the previous invalid
#: provider output is truncated before it is sent back, so a repair can never
#: amplify a response into an unbounded second request.
MAX_REPAIR_CONTEXT_LENGTH = 8 * 1024

_REPAIR_INSTRUCTION = (
    "\n\nYour previous response was not valid structured output for the declared "
    "contract. Return ONLY a single corrected JSON object that validates against "
    "that contract. Do not include explanations, markdown fences or extra text.\n"
    "Previous response:\n{previous}"
)


@lru_cache(maxsize=1)
def _transfer_contracts() -> TransferContracts:
    """The checked-in provider transfer contract fixture, cached (immutable).

    v0.8 Scope §6.2: the deterministic mode selector intersects the reviewed
    per-mode provider contracts with the organisation/task/model policy before
    any external transfer. The fixture is validated at load (Scope §6.1) so an
    inconsistent declaration fails at startup and in CI, never at dispatch.
    """
    return load_transfer_contracts()


def import_schema(path: str) -> type[BaseModel]:
    """Resolve a dotted import path to a Pydantic model class.

    The task registry's ``output_schema`` (v0.7 Scope §6.2) and the request's
    optional override are dotted paths; this is the default resolver. Raises
    :class:`OutputSchemaError` when the path is unknown or does not name a
    Pydantic model — fail fast, never fall back to unvalidated data.
    """

    try:
        return resolve_output_schema(path)
    except RegistryValidationError as exc:
        raise OutputSchemaError(str(exc)) from exc


def _merge_restriction_list(
    caller: list[str] | None, organisation: list[str] | None
) -> list[str] | None:
    """Merge the caller's restrictions with the organisation's allowlist.

    ``None`` means "no restriction from this source"; two non-``None`` lists
    intersect, so both the feature-level and the organisation-level constraint
    must hold. An empty intersection simply means no model is eligible, which
    the router surfaces as the safe :class:`ModelNotAvailableError`.
    """
    if caller is None:
        return organisation
    if organisation is None:
        return caller
    return [item for item in caller if item in organisation]


def _merge_organisation_policy(
    policy: OrganisationAIPolicy,
    allowed_providers: list[str] | None,
    allowed_model_ids: list[str] | None,
    model_override: str | None,
) -> tuple[list[str] | None, list[str] | None, str | None]:
    """Merge the organisation policy with the caller's routing restrictions.

    The organisation's allowlists apply on top of the caller's (intersection);
    the organisation's overrides are folded into the caller's (a forced
    provider narrows the provider set, a forced model is the effective
    ``model_override``). The organisation is authoritative: its override wins
    over a caller-supplied one, and its allowlist can only ever restrict.
    """
    providers = _merge_restriction_list(allowed_providers, policy.allowed_provider_ids or None)
    if policy.provider_override is not None:
        providers = _merge_restriction_list([policy.provider_override], providers)
    models = _merge_restriction_list(allowed_model_ids, policy.allowed_model_ids or None)
    override = policy.model_override if policy.model_override is not None else model_override
    return providers, models, override


def _attachment_set_digest(attachments: Sequence[Attachment]) -> str | None:
    """SHA-256 of the resolved attachment set, or ``None`` without attachments.

    The digest is the privacy-safe identity of the source input recorded on
    the durable rows (v0.7 Scope §6.5) — the bytes themselves are never persisted
    (BP §28, ADR-0017). The combined content is bounded by the 10 MB template
    ceiling, so hashing it here is cheap and deterministic.
    """
    if not attachments:
        return None
    return hashlib.sha256(b"".join(attachment.content for attachment in attachments)).hexdigest()


def _validated_output_digest(output: Any) -> str:
    """Return a stable SHA-256 digest without retaining validated content."""

    if isinstance(output, BaseModel):
        value: Any = output.model_dump(mode="json")
    elif isinstance(output, str):
        value = {"text": output}
    else:
        value = output
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class _PendingAttempt:
    """Bookkeeping for one reserved attempt row inside :meth:`AIService.execute`.

    One instance exists per attempted provider execution: the durable row id,
    the routing decision actually used (a fallback may change model/provider
    between attempts), the adapter region it ran in, the billed usage, and the
    safe error code when that attempt failed. The terminal tail settles each
    attempt's row with its own actuals priced with its own model's rates.
    """

    row_id: UUID | None
    model: ModelDefinition
    decision: RoutingDecision
    region: str
    usage: TokenUsage
    latency_ms: int = 0
    error_code: str | None = None


@dataclass
class _LargeSource:
    """Head-verified facts about a source object above the inline threshold.

    v0.8 Scope §2.3/§6.3: the non-inline path decides from head metadata
    *before* any bytes are read. The inline resolver caps at 5,000,000 bytes
    (``MAX_ATTACHMENT_BYTES``), so a storage-referenced object whose head size
    exceeds the deployment's inline threshold is routed to the streaming/
    staging seam instead: only the size, allowlisted MIME type and display
    name are known here; the verified copy and digest come from
    :class:`StreamedSource` later.
    """

    reference: str
    size_bytes: int
    mime_type: str
    display_name: str


def _resolve_large_mime_type(content_type: str | None, reference: str) -> str:
    """Resolve the allowlisted MIME type of a large object, failing closed.

    Mirrors the inline resolver's MIME resolution (stored content type wins,
    extension fallback otherwise) against the full template allowlist; the
    v0.8 large-path shape gate (exactly one ``application/pdf``) is applied by
    the mode selector itself so a large non-PDF gets the shape error, not a
    confusing MIME error (Scope §2.1 decision 3, §5.3).
    """
    if content_type:
        candidate = content_type.strip().lower().split(";")[0]
        if candidate in ALLOWED_ATTACHMENT_MIME_TYPES:
            return candidate
        raise AIInputValidationError(
            "the referenced storage object has an unsupported content type"
        )
    fallback = EXTENSION_MIME_TYPES.get(Path(reference.rstrip("/")).suffix.lower())
    if fallback is None:
        raise AIInputValidationError(
            "the referenced storage object declares no supported content type"
        )
    return fallback


class AIService:
    """Provider-neutral executor for one AI task.

    Constructed with the registry interfaces and the configured provider
    adapter(s); the wiring is owned by the application (v0.7 Scope §6.3
    factory, §6.5 organisation settings). ``provider`` is the single-provider
    shorthand; ``providers`` maps provider id → adapter for deployments that
    enable more than one provider, which is what makes the router's reviewed
    cross-provider fallback actually executable (v0.7 Scope §6.2/§6.4). The
    fake provider is the default adapter under test.

    ``attachment_resolver`` (v0.7 Scope §6.4, ADR-0017) resolves a request's
    private ``storage_reference`` into bounded in-memory attachments at the
    service boundary; ``None`` rejects storage-referenced requests with a
    clear error. ``redactor`` is applied to text/message content before
    dispatch.

    ``transfer_deployment`` (v0.8 Scope §2.2/§6.2) closes the deployment-level
    transfer policy over the executor: the aggregate inline threshold, the
    large-file template ceiling and the enabled non-inline modes from the
    typed settings. ``None`` applies the template default (inline only, at the
    5,000,000-byte threshold), which is also the deterministic default under
    test.

    ``storage`` and ``transfer_stores`` (v0.8 Scope §2.3/§6.3) provision the
    non-inline execution seam: ``storage`` streams a verified private source
    bounded into a secure temporary file and ``transfer_stores`` maps provider
    id → the provider-neutral :class:`TransferStore` that stages it (Vertex
    GCS staging under §6.4, the deterministic fake under test). Without them a
    selected non-inline mode fails closed before any external transfer, so a
    service wired for inline-only testing can never stage anywhere.

    Enforcement is fail closed (v0.7 Scope §2/§6.5): the documented
    application-facing entry point ``execute`` requires the persistence/policy
    port, and execution without it raises unless the explicit test-only
    ``allow_unmanaged_execution`` seam is opted into at construction. No
    caller can accidentally dispatch with no enabled-state, allowlist, budget,
    persistence or audit enforcement.
    """

    def __init__(
        self,
        *,
        task_registry: TaskRegistry,
        prompt_registry: PromptRegistry,
        model_registry: ModelRegistry,
        provider: LLMProvider | None = None,
        providers: Mapping[str, LLMProvider] | None = None,
        schema_resolver: SchemaResolver = import_schema,
        attachment_resolver: AttachmentResolver | None = None,
        redactor: Redactor | None = None,
        transfer_deployment: TransferDeploymentPolicy | None = None,
        storage: ObjectStorage | None = None,
        transfer_stores: Mapping[str, TransferStore] | None = None,
        managed_url_stager: ManagedUrlStager | None = None,
        allow_unmanaged_execution: bool = False,
    ) -> None:
        if provider is not None:
            if providers is not None:
                raise ValueError("AIService accepts either provider or providers, not both")
            self._providers: dict[str, LLMProvider] = {provider.provider_id: provider}
        elif providers is not None:
            self._providers = dict(providers)
        else:
            raise ValueError("AIService requires at least one configured provider")
        if not self._providers:
            raise ValueError("AIService requires at least one configured provider")
        self._task_registry = task_registry
        self._prompt_registry = prompt_registry
        self._model_registry = model_registry
        self._schema_resolver = schema_resolver
        self._attachment_resolver = attachment_resolver
        self._redactor = redactor
        # v0.8 Scope §2.2/§6.2: the deployment-level transfer policy the
        # selector closes over. Default-deny: ``None`` is the template
        # default (inline only at the 5,000,000-byte threshold), so a service
        # wired without the typed settings can never select a non-inline mode.
        self._transfer_deployment = transfer_deployment or TransferDeploymentPolicy()
        # v0.8 Scope §2.3/§6.3: the non-inline execution seam. ``storage``
        # streams a verified private source bounded into a secure temporary
        # file; ``transfer_stores`` maps provider id → the provider-neutral
        # store that stages it. Both are optional: a service without them
        # fails closed on a selected non-inline mode before any transfer.
        self._storage = storage
        self._transfer_stores = dict(transfer_stores or {})
        # v0.8 Scope §2.3/§6.4-§6.5: the dev managed-URL staging seam — used
        # when the source storage cannot produce a provider-reachable HTTPS
        # signed URL (local MinIO); ``None`` mints the URL directly.
        self._managed_url_stager = managed_url_stager
        # Test-only seam (v0.7 Scope §6.5): ``execute`` without a recorder
        # port is refused by default so the supported entry point can never
        # bypass organisation enforcement; deterministic service tests that
        # exercise routing/dispatch in isolation opt into unmanaged execution.
        self._allow_unmanaged_execution = allow_unmanaged_execution

    def _redact(self, text: str) -> str:
        return self._redactor(text) if self._redactor is not None else text

    def _region_of_provider(self) -> dict[str, str]:
        """Provider id → configured region for every configured adapter.

        The router needs the *complete* map so a reviewed fallback can prove
        it never implicitly moves a request across regions (v0.7 Scope §6.3
        regional amendment, ADR-0017): a fallback candidate in another region
        is excluded, and an omitted region cannot be used to bypass the rule.
        """
        return {provider_id: provider.region for provider_id, provider in self._providers.items()}

    def _audit_recorder(self, session: AsyncSession | None):
        """Build the transfer-lifecycle audit recorder bound to a session.

        The orchestrator emits low-cardinality transfer events
        (``ai.transfer_staged``/``ai.transfer_reused``/``ai.transfer_expired``/
        ``ai.transfer_deleted``, Scope §2.5/§6.7) through this seam. With no
        caller-bound session (hermetic service tests) auditing is disabled;
        every executed path wires the caller's session exactly like the
        durable transfer-reference store (v0.8 Scope §6.3).
        """

        if session is None:
            return None

        async def _record(
            action: str,
            resource_type: str,
            resource_id: str,
            organisation_id: UUID,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            await record_event(
                session,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                organisation_id=organisation_id,
                metadata=metadata,
            )

        return _record

    def _select_transfer_mode(
        self,
        *,
        policy: OrganisationAIPolicy | None,
        task: TaskDefinition,
        model: ModelDefinition,
        resolved_attachments: Sequence[Attachment],
        source_reference: str | None,
        organisation_id: UUID,
        large_source: _LargeSource | None = None,
    ) -> TransferMode:
        """Deterministically select the transfer mode for a resolved set.

        v0.8 Scope §2.2/§6.2: *every* attachment set passes through the full
        policy intersection (``select_transfer_mode_for_policy``) — there is no
        inline shortcut below the deployment threshold, so a task/model whose
        reviewed declarations exclude inline can never dispatch inline (Scope
        §5.2). The deployment policy (``TransferDeploymentPolicy``), the
        organisation policy (``allowed_transfer_modes`` /
        ``max_large_attachment_bytes``), the task declaration, the routed
        model's declarations and the provider's reviewed contract all gate the
        selected mode, preferring a provider upload for transient sources, a
        managed signed URL for retained private sources and Vertex GCS staging
        when the provider declares it. Dispatch occurs only when every gate
        allows the selected mode; otherwise the service fails before any
        external transfer (:class:`TransferModeUnavailableError`), never
        silently downgrading to a less private mode.

        The v0.8 large path carries exactly one ``application/pdf`` (Scope
        §2.1 decision 3, §5.3): a set above the deployment threshold with any
        other count or MIME type fails before transfer, and the lowest of the
        organisation, deployment, model and provider ceilings decides whether
        a candidate mode can carry it.

        The inline path remains the only *executable* path in §6.2 — the
        streaming/staging execution seam for a selected non-inline mode is
        provisioned by §6.3+ — so an eligible-but-not-yet-executable non-inline
        selection fails closed with :class:`TransferExecutionUnavailableError`
        until that seam lands. The selection itself is fully exercised by the
        fake-backed contract suite.

        The selector additionally gates the inline path on the model's own
        inline declarations (:func:`model_inline_ceiling`), so inline dispatch
        never violates a model's inline MIME or byte limits — the same
        coherent decision routing already made, re-checked here before
        dispatch.
        """
        deployment = self._transfer_deployment
        if large_source is not None:
            # v0.8 Scope §2.3: a large source was headed before any bytes were
            # read. Selection runs on the head metadata (size + MIME); the
            # verified copy and digest are streamed later by the seam, so the
            # 50 MB ceiling is never accumulated in memory.
            aggregate_bytes = large_source.size_bytes
            attachment_mime_types = [large_source.mime_type]
            attachment_sizes = [large_source.size_bytes]
            resolved_attachments = []  # type: ignore[assignment]
        else:
            aggregate_bytes = sum(attachment.size for attachment in resolved_attachments)
            attachment_mime_types = [attachment.mime_type for attachment in resolved_attachments]
            attachment_sizes = [attachment.size for attachment in resolved_attachments]
        # v0.8 large-path shape gate (Scope §2.1 decision 3, §5.3): exactly one
        # PDF, and nothing else above the deployment threshold — an operator
        # diagnosing a denial gets the shape reason up front rather than a
        # generic no-eligible-mode error. Below the threshold the shape is
        # irrelevant because inline is the only selectable mode there.
        if aggregate_bytes > deployment.inline_aggregate_threshold_bytes and (
            len(attachment_mime_types) != 1 or attachment_mime_types[0] not in NON_INLINE_MIME_TYPES
        ):
            raise TransferModeUnavailableError(
                "the non-inline transfer path accepts exactly one application/pdf; "
                "this attachment set cannot be transferred above the inline threshold"
            )
        source_lifecycle = (
            source_lifecycle_for_reference(source_reference, organisation_id)
            if source_reference
            else SourceLifecycle.TRANSIENT
        )
        contract = _transfer_contracts().providers.get(model.provider)
        if contract is None:
            raise TransferModeUnavailableError(
                "the routed model's provider has no reviewed transfer contract"
            )
        organisation_modes = (
            policy.allowed_transfer_modes if policy is not None else [TransferMode.INLINE]
        )
        organisation_max_bytes = (
            policy.effective_max_large_attachment_bytes() if policy is not None else None
        )
        selected = select_transfer_mode_for_policy(
            aggregate_bytes=aggregate_bytes,
            attachment_sizes=attachment_sizes,
            attachment_mime_types=attachment_mime_types,
            source_lifecycle=source_lifecycle,
            organisation_allowed_modes=organisation_modes,
            organisation_max_large_attachment_bytes=organisation_max_bytes,
            task_allowed_modes=task.allowed_transfer_modes,
            model_allowed_modes=model.allowed_transfer_modes,
            model_mode_limits=model_transfer_mode_limits(model) or None,
            model_inline=model_inline_ceiling(model),
            deployment=deployment,
            contract=contract,
        )
        if selected is None:
            raise TransferModeUnavailableError(
                "no permitted/provider-supported transfer mode is eligible for this attachment set"
            )
        if selected in (TransferMode.STORAGE_REFERENCE, TransferMode.PROVIDER_UPLOAD):
            # v0.8 Scope §2.4/§6.3-§6.5: the provider-copy staging seam. The
            # storage-reference mode stages into the Vertex private GCS bucket;
            # the provider-upload mode stages a transient source through the
            # routed provider's own upload store (OpenAI Files API). Both
            # require the process-wide storage and the routed provider's
            # transfer store, or the selection fails closed before any external
            # transfer (Scope §2.2 "fail closed, never silently downgrade").
            if self._storage is None or self._transfer_stores.get(model.provider) is None:
                raise TransferExecutionUnavailableError(
                    "the selected transfer mode is not executable by this release"
                )
            return selected
        if selected is TransferMode.MANAGED_SIGNED_URL:
            # v0.8 Scope §2.3/§6.3: the just-in-time managed-URL seam has no
            # provider copy — the short-lived signed URL is minted per dispatch
            # from the retained source — so only the process-wide storage is
            # required here (the durable reference store is per-call).
            if self._storage is None:
                raise TransferExecutionUnavailableError(
                    "the selected transfer mode is not executable by this release"
                )
            return selected
        if selected is not TransferMode.INLINE:
            raise TransferExecutionUnavailableError(
                "the selected transfer mode is not executable by this release"
            )
        return selected

    async def execute(
        self,
        request: AIRequest,
        *,
        recorder: AIPersistencePort | None = None,
        request_id: str | None = None,
        allowed_providers: list[str] | None = None,
        allowed_model_ids: list[str] | None = None,
        model_override: str | None = None,
        maximum_estimated_cost: Decimal | None = None,
        attachments: Sequence[Attachment] | None = None,
        input_reference: str | None = None,
        transfer_references: TransferReferenceStore | None = None,
        execution_session: AsyncSession | None = None,
    ) -> AIResult:
        """Execute one task request and return a validated result.

        ``recorder`` (v0.7 Scope §6.5) is the mandatory persistence/policy
        port: the organisation's policy is loaded and enforced *here* — AI
        disabled is rejected, the organisation's allowed providers/models and
        overrides are merged with the caller's restrictions, budget is gated
        before dispatch and settled with actuals afterwards, one
        ``ai_requests`` row per attempted provider execution is persisted
        (v0.7 Scope §2), the validated output record and audit events are
        written, and output content is retained only when the task-level
        opt-in and the organisation retention policy both permit it. Execution
        without a port raises unless the service was constructed with the
        explicit test-only ``allow_unmanaged_execution`` seam.

        ``request_id`` optionally pins the caller-visible AI request id so a
        durable job can re-execute idempotently (the §6.6 ``ai.execute`` job
        passes the id it created before enqueueing); ``None`` generates a new
        id per execution. It is the execution id shared by every attempt row.

        ``allowed_providers`` is the organisation-level provider allowlist
        enforced by the model registry/router (v0.7 Scope §6.5); ``None``
        means no organisation restriction.

        ``attachments`` are bounded inline attachments either passed by the
        caller (already resolved at the service/job boundary) or resolved by
        this service from the request's private ``storage_reference`` through
        the configured ``attachment_resolver`` (v0.7 Scope §6.4, ADR-0017).
        They are validated against the template limits (5 MB per file / 10 MB
        combined), the router only selects models declaring the ``documents``
        capability with sufficient per-model ceilings, image attachments
        additionally require the model's ``vision`` capability, and the
        configured adapter must declare document support — every incompatible
        modality, MIME type and size combination fails before provider
        dispatch.

        ``execution_session`` (v0.8 Scope §6.7) is the caller's session-bound
        persistence boundary used to record transfer-lifecycle audit events
        (mode selection, staging, expiry, deletion) in the same transaction
        family as the durable reference rows; ``None`` disables that audit
        trail for hermetic service tests. The durable-job and demonstration
        flows always pass the caller-bound session, exactly like the durable
        transfer-reference store (Scope §6.3).

        v0.7 Scope §6.4 safety controls: transient provider failures
        (unavailable, rate limited, timeout) retry within the task's
        ``retry_policy`` ``max_attempts``, re-routing through the router's
        region-safe fallback only when the task's ``fallback_policy`` allows
        it; malformed provider output triggers at most ``repair_attempts``
        (≤ 1) bounded repair request, then consumes one bounded task retry per
        malformed output, and a transient failure inside the repair itself
        consumes the same bounded retry budget instead of escaping; permanent
        validation/policy failures never retry. Every attempt's usage/cost is
        accounted and priced with that attempt's own model, so the returned
        result prices the real traffic attempt by attempt. The budget gate
        reserves the bounded worst case for the whole retry/repair policy
        before the first dispatch. Unvalidated structured data is never
        returned.

        Raises an :class:`~app.ai.errors.AIError` subclass with a safe code on
        every failure.
        """

        if recorder is None and not self._allow_unmanaged_execution:
            raise RuntimeError(
                "AIService.execute requires a persistence/policy port; refusing to "
                "dispatch without organisation enforcement (v0.7 Scope §6.5)"
            )

        # The durable-record input provenance (v0.7 Scope §6.6). A request that
        # names a ``storage_reference`` records it directly; the durable-job
        # boundary that decodes a private object into text input for a text
        # task passes the reference here so the ``ai_requests``/``ai_outputs``
        # rows still carry where the input came from (BP §28, ADR-0017).
        effective_input_reference = input_reference or request.storage_reference
        execution_request_id = request_id or uuid4().hex
        task = self._resolve_task(request.task)
        prompt = self._resolve_prompt(task.prompt_name, task.prompt_version)
        logger.info(
            "ai.request.started",
            ai_request_id=execution_request_id,
            task=task.name,
            prompt_name=prompt.name,
            prompt_version=prompt.version,
        )
        # Per-attempt accounting state (v0.7 Scope §2/§6.5): every attempted
        # provider execution gets its own running row (the first via reserve,
        # later attempts via record_attempt) carrying that attempt's routing
        # decision and estimate; settlement prices each row with its own
        # model's rates. ``pending_attempts`` tracks the rows so the terminal
        # tail can settle every one of them with actuals and safe error codes.
        # ``resolved_attachments``/``retain_output_content`` are hoisted so
        # the terminal tail type-checks even though a pre-dispatch failure
        # raises before they are (re)assigned — the failure tail re-raises, so
        # the success path below always sees the in-try values.
        resolved_attachments: list[Attachment] = []
        retain_output_content = False
        policy: OrganisationAIPolicy | None = None
        pending_attempts: list[_PendingAttempt] = []
        dispatch_count = 0
        failure: AIError | None = None
        result: AIResult | None = None
        winning_attempt: _PendingAttempt | None = None
        # v0.8 Scope §2.5: the staged AI-owned reference (if any) for terminal
        # best-effort cleanup. Hoisted so the tail can run after every outcome,
        # including a pre-loop failure where nothing was ever staged.
        staged_orchestrator: TransferOrchestrator | None = None
        staged_reference: ExternalFileReference | None = None
        try:
            # Input-form validation first (v0.7 Scope §6.4): a task whose
            # prompt declares ``text`` must receive text input — a storage
            # reference can never silently satisfy it — and vice versa.
            self._validate_input_form(prompt, request)
            # v0.8 Scope §2.3: a storage-referenced object is headed first. A
            # head size above the deployment's inline threshold routes the
            # request to the streaming/staging seam (the inline resolver caps
            # at MAX_ATTACHMENT_BYTES, so it can never resolve a large object);
            # everything else resolves inline exactly as before.
            large_source = await self._head_large_source(request) if attachments is None else None
            if large_source is not None:
                resolved_attachments = []
            else:
                resolved_attachments = await self._resolve_attachments(request, attachments)
            rendered = self._render_prompt(
                prompt,
                request,
                resolved_attachments,
                reference_display_names=(
                    [large_source.display_name] if large_source is not None else None
                ),
            )
            # The effective output schema is resolved exactly once: a request
            # override wins, and an empty-string override is treated as "no
            # override" so the provider request and output validation can never
            # disagree (v0.7 Scope §6.1). Its JSON Schema (v0.7 Scope §6.4) is
            # generated before dispatch so a bad schema fails fast and every
            # adapter can request native structured output.
            effective_output_schema = request.output_schema or task.output_schema
            output_json_schema = self._output_json_schema(effective_output_schema)
            configured_max_tokens = task.parameter_defaults.get("max_tokens")
            configured_temperature = task.parameter_defaults.get("temperature")
            estimated_input_tokens = estimate_tokens(rendered)
            max_attempts = task.retry_policy.max_attempts
            repair_budget = task.retry_policy.repair_attempts
            excluded_model_ids: list[str] = []
            last_transient: ProviderError | None = None

            # Organisation controls (v0.7 Scope §6.5): the organisation's
            # effective policy is enforced *here* — never in a router or UI
            # (BP §27) — and its restrictions are merged with the caller's
            # before routing. The task-level retention opt-in only takes
            # effect together with a configured organisation retention policy
            # (v0.7 Scope §2). A disabled organisation is a safe pre-dispatch
            # failure: it raises here so the terminal tail below emits
            # ``ai.request.failed`` for the started request (v0.7 Scope §6.7 —
            # the synchronous path has no worker failure log to compensate).
            if recorder is not None:
                policy = await recorder.load_policy(organisation_id=request.organisation_id)
                if not policy.enabled:
                    raise AIUnavailableError("AI is not enabled for this organisation")
                allowed_providers, allowed_model_ids, model_override = _merge_organisation_policy(
                    policy, allowed_providers, allowed_model_ids, model_override
                )
                retain_output_content = (
                    task.retains_output_content and policy.retention_policy_days is not None
                )
            # The provider map is authoritative for routing: when the caller
            # and the organisation policy impose no provider restriction, the
            # router only ever considers models whose adapter is configured in
            # this service. A task's model preferences therefore resolve to
            # the configured provider — Vertex when the adapter is enabled,
            # the deterministic fake otherwise — instead of routing to a model
            # the service could never dispatch through (the same fail-closed
            # outcome as before, raised by the router instead of the provider
            # map).
            if allowed_providers is None:
                allowed_providers = sorted(self._providers)

            # v0.8 Scope §6.2: routing and mode selection are one coherent
            # decision. The router receives the same effective transfer-mode
            # context the selector runs on — source lifecycle, organisation
            # policy, deployment configuration — so a candidate survives only
            # when at least one mode is eligible under the current size/MIME/
            # count, task, lifecycle, organisation, deployment, model/provider
            # contract and inline threshold. The router therefore never commits
            # to a model the selector would then deny while a compatible
            # candidate exists. The context is request-scoped and identical for
            # every attempt.
            transfer_context = TransferRoutingContext(
                source_lifecycle=(
                    source_lifecycle_for_reference(
                        effective_input_reference, request.organisation_id
                    )
                    if effective_input_reference
                    else SourceLifecycle.TRANSIENT
                ),
                organisation_allowed_modes=(
                    policy.allowed_transfer_modes if policy is not None else [TransferMode.INLINE]
                ),
                organisation_max_large_attachment_bytes=(
                    policy.effective_max_large_attachment_bytes() if policy is not None else None
                ),
                deployment=self._transfer_deployment,
            )

            for attempt in range(1, max_attempts + 1):
                try:
                    decision = self._model_registry.route(
                        task,
                        allowed_providers=allowed_providers,
                        allowed_model_ids=allowed_model_ids,
                        model_override=model_override,
                        estimated_input_tokens=estimated_input_tokens,
                        maximum_estimated_cost=maximum_estimated_cost,
                        attachments=resolved_attachments,
                        excluded_model_ids=excluded_model_ids,
                        # Every configured provider's region is declared so a
                        # reviewed fallback can never implicitly change region
                        # (v0.7 Scope §6.3 regional amendment, ADR-0017).
                        region_of_provider=self._region_of_provider(),
                        transfer_context=transfer_context,
                    )
                except (KeyError, ValueError, TransferModeUnavailableError) as exc:
                    # No eligible model at all, or — during an allowed fallback —
                    # no eligible alternative remains after excluding the failed
                    # model(s). The latter must surface the original transient
                    # failure (retryable by the caller/job) instead of converting
                    # it into a permanent error: the in-process routing budget is
                    # exhausted, not the model or the transfer policy itself.
                    if excluded_model_ids and last_transient is not None:
                        raise last_transient from exc
                    if isinstance(exc, TransferModeUnavailableError):
                        raise
                    raise ModelNotAvailableError(f"no model satisfies task {task.name}") from exc
                model = decision.model
                try:
                    provider = self._providers[model.provider]
                except KeyError as exc:
                    raise ModelNotAvailableError(
                        "resolved model provider is not configured for this service"
                    ) from exc
                if resolved_attachments and not provider.supports_documents:
                    raise ModelNotAvailableError(
                        "resolved model provider does not support document attachments"
                    )
                # v0.8 Scope §2.2/§6.2 deterministic transfer gate: the mode is
                # selected from the organisation/task/model/provider policy
                # intersection before any external transfer, and an attachment
                # set that needs a non-inline mode fails closed here — never by
                # silently riding the inline path above the aggregate
                # threshold (Scope §5.2, §6.2).
                selected_transfer_mode = TransferMode.INLINE
                if resolved_attachments or large_source is not None:
                    selected_transfer_mode = self._select_transfer_mode(
                        policy=policy,
                        task=task,
                        model=model,
                        resolved_attachments=resolved_attachments,
                        source_reference=effective_input_reference,
                        organisation_id=request.organisation_id,
                        large_source=large_source,
                    )
                if selected_transfer_mode is not TransferMode.INLINE:
                    # v0.8 Scope §2.5/§6.7: record the deterministic non-inline
                    # mode selection — low-cardinality metric plus the
                    # ``ai.transfer_staged`` audit trail (written by the
                    # orchestrator when the durable reference lands). Never
                    # request ids, object keys, URLs or content (BP §28).
                    observe_ai_transfer_selection(
                        mode=selected_transfer_mode.value,
                        provider=model.provider,
                    )
                    audit_recorder = self._audit_recorder(execution_session)
                    if audit_recorder is not None:
                        with contextlib.suppress(Exception):
                            await audit_recorder(
                                ACTION_AI_TRANSFER_SELECTED,
                                "ai_attachment_reference",
                                execution_request_id,
                                request.organisation_id,
                                {"transfer_mode": selected_transfer_mode.value},
                            )
                # v0.8 Scope §2.3/§2.4/§6.5: a selected non-inline mode stages
                # the verified private source into the provider's staging form
                # (Vertex private GCS bucket for storage_reference, the
                # provider's own upload API for provider_upload, a durable
                # reference plus a just-in-time signed URL for managed_signed_url)
                # and hands the adapter the opaque provider-neutral reference.
                # The stream is bounded and verified before staging; a retry
                # reuses the live durable reference through the orchestrator
                # instead of staging twice (retry-only reuse, Scope §2.1).
                staged_reference: ExternalFileReference | None = None
                staged_orchestrator: TransferOrchestrator | None = None
                if selected_transfer_mode in (
                    TransferMode.STORAGE_REFERENCE,
                    TransferMode.PROVIDER_UPLOAD,
                    TransferMode.MANAGED_SIGNED_URL,
                ):
                    if large_source is None or self._storage is None or transfer_references is None:
                        raise TransferExecutionUnavailableError(
                            "the selected transfer mode is not executable by this release"
                        )
                    if selected_transfer_mode is TransferMode.MANAGED_SIGNED_URL:
                        # No provider-hosted copy: the managed-signed-url mode
                        # only builds the durable reference here; the
                        # short-lived signed URL itself is minted per dispatch
                        # (Scope §2.3), so no provider transfer store is needed.
                        store: TransferStore | None = None
                    else:
                        store = self._transfer_stores.get(model.provider)
                        if store is None:
                            raise TransferExecutionUnavailableError(
                                "the selected transfer mode is not executable by this release"
                            )
                    staged_orchestrator = TransferOrchestrator(
                        storage=self._storage,
                        store=store,
                        references=transfer_references,
                        managed_url_stager=self._managed_url_stager,
                        audit_recorder=self._audit_recorder(execution_session),
                    )
                    source_lifecycle = source_lifecycle_for_reference(
                        large_source.reference, request.organisation_id
                    )
                    # The routed model's reviewed PDF page ceiling is derived
                    # provider-neutrally from the checked-in mode contract and
                    # context window. Inspection belongs at this common source
                    # boundary: every adapter receives the same verified,
                    # already-authorised PDF and never needs to parse it.
                    max_pdf_pages: int | None = None
                    contract = _transfer_contracts().providers.get(model.provider)
                    mode_contract = (
                        contract.transfer_modes.get(selected_transfer_mode)
                        if contract is not None
                        else None
                    )
                    if mode_contract is not None and mode_contract.pdf_pages is not None:
                        max_pdf_pages = mode_contract.pdf_pages.effective_ceiling(
                            model.context_window
                        )
                    async with StreamedSource(
                        storage=self._storage,
                        reference=large_source.reference,
                        organisation_id=request.organisation_id,
                        max_bytes=self._transfer_deployment.max_large_attachment_bytes,
                        allowed_mime_types=NON_INLINE_MIME_TYPES,
                    ) as source:
                        if max_pdf_pages is not None:
                            validate_pdf_page_limit(
                                source.path,
                                max_pages=max_pdf_pages,
                            )
                        # v0.8 Scope §2.1/§2.3 retry-only reuse: a redelivered
                        # execution first looks up the live matching durable
                        # reference (same logical request, provider, mode,
                        # digest and region) and reuses it — emitting the
                        # ``ai.transfer_reused`` audit event — instead of
                        # staging the copy again. Only when no live match
                        # exists is the source staged anew (``ai.transfer_staged``).
                        staged_reference = await staged_orchestrator.find_reusable_reference(
                            organisation_id=request.organisation_id,
                            logical_request_id=execution_request_id,
                            provider_id=model.provider,
                            mode=selected_transfer_mode,
                            source_digest=source.sha256_digest,
                            region=provider.region,
                        )
                        if staged_reference is None:
                            staged_reference = await staged_orchestrator.create_or_reuse_reference(
                                organisation_id=request.organisation_id,
                                logical_request_id=execution_request_id,
                                provider_id=model.provider,
                                mode=selected_transfer_mode,
                                source_reference=large_source.reference,
                                source_digest=source.sha256_digest,
                                size_bytes=source.size_bytes,
                                mime_type=source.mime_type,
                                source_lifecycle=source_lifecycle,
                                region=provider.region,
                                expires_at=None,
                                source_path=source.path,
                            )
                # v0.8 Scope §2.3: a just-in-time managed download URL is
                # minted per dispatch/retry for a selected managed-signed-url
                # mode (never persisted, redacted at every log boundary). The
                # URL is the temporary bearer capability one attempt sends as
                # the provider's URL file input.
                #
                # Local-transient path (Scope §6.6 lesson from the OpenAI
                # build): with a local storage seam a transient ``provider_upload``
                # reference has no provider copy (the Anthropic store yields a
                # no-copy reference — external id = source identity) and is
                # served by staging the verified object into the scratch GCS
                # staging directory and minting a signed URL to that GCS object
                # as the URL document source instead.
                managed_url: str | None = None
                local_transient_dispatch = (
                    selected_transfer_mode is TransferMode.PROVIDER_UPLOAD
                    and staged_reference is not None
                    and staged_reference.source_lifecycle is SourceLifecycle.TRANSIENT
                    and staged_reference.external_id == staged_reference.source_reference
                    and self._managed_url_stager is not None
                )
                if (
                    (
                        selected_transfer_mode is TransferMode.MANAGED_SIGNED_URL
                        or local_transient_dispatch
                    )
                    and staged_reference is not None
                    and staged_orchestrator is not None
                ):
                    signed = await staged_orchestrator.mint_managed_url(
                        reference=staged_reference,
                        ttl_seconds=self._transfer_deployment.managed_url_ttl_seconds,
                    )
                    managed_url = signed.url
                provider_request = ProviderRequest(
                    task=task.name,
                    model=model.model,
                    prompt=rendered,
                    output_schema=effective_output_schema,
                    output_json_schema=output_json_schema,
                    max_tokens=(
                        configured_max_tokens if isinstance(configured_max_tokens, int) else None
                    ),
                    temperature=(
                        float(configured_temperature)
                        if isinstance(configured_temperature, (int, float))
                        else None
                    ),
                    metadata=request.metadata,
                    attachments=resolved_attachments,
                    staged_file=(
                        StagedFile(
                            external_id=staged_reference.external_id,
                            mime_type=staged_reference.mime_type,
                        )
                        if staged_reference is not None
                        and selected_transfer_mode is not TransferMode.MANAGED_SIGNED_URL
                        else None
                    ),
                    managed_url=managed_url,
                )
                # One durable row per actual dispatch (v0.7 Scope §2). The
                # first attempt gates the execution's bounded worst-case budget
                # under the settings-row lock (idempotent on the execution id);
                # every further attempt gets its own row with no separate gate
                # (the bounded worst case was already reserved). Rows are
                # created before dispatch so a crash mid-execution is
                # reconcilable; failures before the first dispatch reserve
                # nothing.
                dispatch_count += 1
                pending_attempt = _PendingAttempt(
                    row_id=None,
                    model=model,
                    decision=decision,
                    region=provider.region,
                    usage=TokenUsage(input_tokens=0, output_tokens=0),
                )
                if recorder is not None:
                    if dispatch_count == 1:
                        # The bounded worst case for the whole retry/repair
                        # policy, so a retry-heavy execution can never
                        # collectively overrun the budget after passing a
                        # per-attempt check (v0.7 Scope §6.5).
                        bounded_estimate = (
                            Decimal(max_attempts + repair_budget) * decision.estimated_max_cost
                        )
                        reservation = await recorder.reserve(
                            organisation_id=request.organisation_id,
                            user_id=request.user_id,
                            request_id=execution_request_id,
                            task=task.name,
                            provider=model.provider,
                            model=model.model,
                            prompt_name=prompt.name,
                            prompt_version=prompt.version,
                            routing_reason=decision.reason,
                            fallback_used=decision.fallback_used,
                            region=provider.region,
                            estimated_cost=decision.estimated_max_cost,
                            execution_maximum_estimated_cost=bounded_estimate,
                            input_reference=effective_input_reference,
                            input_digest=_attachment_set_digest(resolved_attachments),
                        )
                        if not reservation.created:
                            raise AIRequestReplayError(
                                "this AI request id already has a durable execution"
                            )
                        pending_attempt.row_id = reservation.row_id
                    else:
                        pending_attempt.row_id = await recorder.record_attempt(
                            organisation_id=request.organisation_id,
                            user_id=request.user_id,
                            request_id=execution_request_id,
                            attempt_number=dispatch_count,
                            task=task.name,
                            provider=model.provider,
                            model=model.model,
                            prompt_name=prompt.name,
                            prompt_version=prompt.version,
                            routing_reason=decision.reason,
                            fallback_used=decision.fallback_used,
                            region=provider.region,
                            estimated_cost=decision.estimated_max_cost,
                            input_reference=effective_input_reference,
                            input_digest=_attachment_set_digest(resolved_attachments),
                        )
                pending_attempts.append(pending_attempt)
                dispatch_started = perf_counter()
                try:
                    response = await self._call_provider(provider, provider_request)
                except (
                    ProviderUnavailableError,
                    ProviderRateLimitError,
                    ProviderTimeoutError,
                ) as exc:
                    # Bounded transient retry (v0.7 Scope §6.4): only the
                    # retryable provider taxonomy retries, and only up to
                    # max_attempts. When the task's reviewed fallback policy
                    # allows it, the failed model is excluded so the next route
                    # picks an eligible fallback model under the same region
                    # constraints; otherwise the identical model is retried.
                    # Never a retry storm.
                    pending_attempt.error_code = exc.error_code
                    if attempt >= max_attempts:
                        raise
                    last_transient = exc
                    if task.fallback_policy.allowed:
                        excluded_model_ids.append(model.id)
                    continue
                finally:
                    pending_attempt.latency_ms = round((perf_counter() - dispatch_started) * 1000)
                pending_attempt.usage = TokenUsage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
                if response.model != model.model:
                    raise ProviderResponseError(
                        "provider response model did not match the routed model"
                    )
                try:
                    output = self._validate_output(
                        effective_output_schema,
                        task.declares_text_result,
                        response.content,
                        response.structured,
                    )
                except OutputValidationError as exc:
                    # Bounded repair then bounded malformed-output task retries
                    # (v0.7 Scope §6.4, ADR-0017): at most one repair request
                    # per execution using the same approved routing/policy
                    # path; a repair that fails — or that hits a transient
                    # error — consumes one bounded task retry instead of
                    # escaping or looping. When no repair budget remains, each
                    # malformed output likewise consumes one bounded task
                    # retry and a failure on the final attempt is terminal.
                    # Unvalidated data is never returned.
                    pending_attempt.error_code = exc.error_code
                    observe_ai_validation_failure(
                        task=task.name, provider=model.provider, model=model.model
                    )
                    if repair_budget > 0:
                        repair_budget -= 1
                        repair_request = self._prepare_repair_request(
                            task,
                            provider_request,
                            response.content,
                            model,
                            maximum_estimated_cost=maximum_estimated_cost,
                        )
                        dispatch_count += 1
                        repair_attempt = _PendingAttempt(
                            row_id=None,
                            model=model,
                            decision=decision,
                            region=provider.region,
                            usage=TokenUsage(input_tokens=0, output_tokens=0),
                        )
                        if recorder is not None:
                            repair_attempt.row_id = await recorder.record_attempt(
                                organisation_id=request.organisation_id,
                                user_id=request.user_id,
                                request_id=execution_request_id,
                                attempt_number=dispatch_count,
                                task=task.name,
                                provider=model.provider,
                                model=model.model,
                                prompt_name=prompt.name,
                                prompt_version=prompt.version,
                                routing_reason=decision.reason,
                                fallback_used=decision.fallback_used,
                                region=provider.region,
                                estimated_cost=estimate_maximum_cost(
                                    task,
                                    model,
                                    estimate_tokens(repair_request.prompt),
                                ),
                                input_reference=effective_input_reference,
                                input_digest=_attachment_set_digest(resolved_attachments),
                            )
                        pending_attempts.append(repair_attempt)
                        repair_started = perf_counter()
                        try:
                            repair_response = await self._call_provider(provider, repair_request)
                            repair_attempt.usage = TokenUsage(
                                input_tokens=repair_response.usage.input_tokens,
                                output_tokens=repair_response.usage.output_tokens,
                            )
                            if repair_response.model != model.model:
                                raise ProviderResponseError(
                                    "provider response model did not match the routed model"
                                )
                            output = self._validate_output(
                                effective_output_schema,
                                task.declares_text_result,
                                repair_response.content,
                                repair_response.structured,
                            )
                        except (
                            ProviderUnavailableError,
                            ProviderRateLimitError,
                            ProviderTimeoutError,
                        ) as exc:
                            # A transient failure inside the repair consumes one
                            # bounded task retry (ADR-0017) instead of escaping.
                            repair_attempt.error_code = exc.error_code
                            if attempt >= max_attempts:
                                raise
                            last_transient = exc
                            if task.fallback_policy.allowed:
                                excluded_model_ids.append(model.id)
                            continue
                        except RepairNotPossibleError:
                            # The repair cannot be dispatched within the task/model
                            # bounds: terminal — retrying cannot shrink the prompt.
                            raise
                        except OutputValidationError as exc:
                            # The repair was dispatched but its output is also
                            # invalid: consume one bounded malformed-output task
                            # retry; a failure on the final attempt is terminal.
                            repair_attempt.error_code = exc.error_code
                            observe_ai_validation_failure(
                                task=task.name, provider=model.provider, model=model.model
                            )
                            if attempt >= max_attempts:
                                raise
                            continue
                        finally:
                            repair_attempt.latency_ms = round(
                                (perf_counter() - repair_started) * 1000
                            )
                        repair_attempt.error_code = None
                        response = repair_response
                        pending_attempt = repair_attempt
                    else:
                        if attempt >= max_attempts:
                            raise
                        continue
                result = AIResult(
                    request_id=execution_request_id,
                    routing=RoutingMetadata(
                        task=task.name,
                        provider=provider.provider_id,
                        model=response.model,
                        prompt_name=prompt.name,
                        prompt_version=prompt.version,
                        reason=decision.reason,
                        fallback_used=decision.fallback_used,
                        region=response.region,
                    ),
                    output=output,
                    usage=TokenUsage(
                        input_tokens=sum(item.usage.input_tokens for item in pending_attempts),
                        output_tokens=sum(item.usage.output_tokens for item in pending_attempts),
                    ),
                    cost=self._aggregate_cost(pending_attempts, model.pricing.currency),
                    completed_at=datetime.now(UTC),
                )
                winning_attempt = pending_attempt
                break
            else:
                # Every attempt exhausted the bounded retry budget without a
                # result; the last transient failure (or a safe fallback) is
                # the outcome, settled below like any other failure.
                failure = (
                    last_transient
                    if last_transient is not None
                    else ProviderUnavailableError(f"AI execution failed for task {task.name}")
                )
        except AIError as exc:
            failure = exc

        # v0.8 Scope §2.5 terminal cleanup: after success, permanent failure or
        # exhausted retries every reference of this logical request is expired
        # and its AI-owned provider copies are deleted best-effort through the
        # orchestrator (``finalize_request_references`` composes the two
        # terminal transitions; each deletion stamps the row and records the
        # safe failure code when the provider delete fails). Failures are
        # suppressed — the deployer-owned GCS lifecycle (age = 1) and the §6.7
        # reconciliation job are the backstop — and never mask the execution
        # outcome. The orchestrator only ever deletes the provider-side copy,
        # never the feature-owned source object.
        if staged_orchestrator is not None:
            with contextlib.suppress(Exception):
                await staged_orchestrator.finalize_request_references(
                    organisation_id=request.organisation_id,
                    logical_request_id=execution_request_id,
                )

        if failure is not None:
            if recorder is not None and pending_attempts:
                # Settlement with actuals: every reserved attempt row receives
                # its own terminal status, its own billed usage priced with its
                # own model's rates, and its own safe error code — never
                # content (BP §28, ADR-0017). A retry-heavy execution is never
                # misaccounted against a single model's rates.
                # The first row carries the bounded execution reservation.
                # Settle it last so concurrent budget checks remain protected
                # until every later attempt's actual cost is durable.
                settlement_order = [*pending_attempts[1:], pending_attempts[0]]
                for pending_attempt in settlement_order:
                    if pending_attempt.row_id is None:
                        continue
                    await recorder.settle(
                        ai_request_id=pending_attempt.row_id,
                        organisation_id=request.organisation_id,
                        task=task.name,
                        user_id=request.user_id,
                        status="failed",
                        error_code=pending_attempt.error_code or failure.error_code,
                        usage=pending_attempt.usage,
                        cost=self._estimate_cost(
                            pending_attempt.model.pricing.input_price_per_million_tokens,
                            pending_attempt.model.pricing.output_price_per_million_tokens,
                            pending_attempt.usage,
                            currency=pending_attempt.model.pricing.currency,
                        ),
                        latency_ms=pending_attempt.latency_ms,
                        routing_provider=pending_attempt.decision.model.provider,
                        routing_model=pending_attempt.decision.model.model,
                        routing_prompt_name=prompt.name,
                        routing_prompt_version=prompt.version,
                        routing_reason=pending_attempt.decision.reason,
                        fallback_used=pending_attempt.decision.fallback_used,
                        region=pending_attempt.region,
                    )
            self._observe_metrics(
                task=task.name,
                attempts=pending_attempts,
                winning_attempt=None,
            )
            logger.warning(
                "ai.request.failed",
                ai_request_id=execution_request_id,
                task=task.name,
                provider=(
                    pending_attempts[-1].decision.model.provider if pending_attempts else None
                ),
                model=pending_attempts[-1].decision.model.model if pending_attempts else None,
                error_code=failure.error_code,
                # The exception message is the redaction-safe surface (AIError
                # messages never echo references, ids, URLs or content — BP
                # §28) and is the single fastest way to diagnose a staging
                # failure without enabling debug logging.
                error_message=str(failure),
                attempts=len(pending_attempts),
            )
            raise failure

        if result is None:
            # Unreachable: the loop either broke with a result or raised.
            raise ProviderUnavailableError(f"AI execution failed for task {task.name}")

        if recorder is not None:
            settlement_order = [*pending_attempts[1:], pending_attempts[0]]
            for pending_attempt in settlement_order:
                if pending_attempt.row_id is None:
                    continue
                if pending_attempt is winning_attempt:
                    # Terminal success plus the validated output record plus
                    # the audit event commit atomically in the port (BP §11).
                    # Output content is retained only when the task-level
                    # opt-in and the organisation retention policy both permit
                    # it; otherwise the record is references/digests only.
                    await recorder.settle(
                        ai_request_id=pending_attempt.row_id,
                        organisation_id=request.organisation_id,
                        task=task.name,
                        user_id=request.user_id,
                        status="succeeded",
                        error_code=None,
                        usage=pending_attempt.usage,
                        cost=self._estimate_cost(
                            pending_attempt.model.pricing.input_price_per_million_tokens,
                            pending_attempt.model.pricing.output_price_per_million_tokens,
                            pending_attempt.usage,
                            currency=pending_attempt.model.pricing.currency,
                        ),
                        latency_ms=pending_attempt.latency_ms,
                        routing_provider=pending_attempt.decision.model.provider,
                        routing_model=pending_attempt.decision.model.model,
                        routing_prompt_name=prompt.name,
                        routing_prompt_version=prompt.version,
                        routing_reason=pending_attempt.decision.reason,
                        fallback_used=pending_attempt.decision.fallback_used,
                        region=pending_attempt.region,
                        output=result.output,
                        output_reference=None,
                        output_digest=_validated_output_digest(result.output),
                        retain_content=retain_output_content,
                        input_reference=effective_input_reference,
                        input_digest=_attachment_set_digest(resolved_attachments),
                    )
                else:
                    # Earlier attempts of a successful execution failed (transient
                    # error or malformed output); each is settled with its own
                    # error code and actuals so the durable record prices the
                    # real traffic attempt by attempt (v0.7 Scope §2).
                    await recorder.settle(
                        ai_request_id=pending_attempt.row_id,
                        organisation_id=request.organisation_id,
                        task=task.name,
                        user_id=request.user_id,
                        status="failed",
                        error_code=pending_attempt.error_code or "ai_error",
                        usage=pending_attempt.usage,
                        cost=self._estimate_cost(
                            pending_attempt.model.pricing.input_price_per_million_tokens,
                            pending_attempt.model.pricing.output_price_per_million_tokens,
                            pending_attempt.usage,
                            currency=pending_attempt.model.pricing.currency,
                        ),
                        latency_ms=pending_attempt.latency_ms,
                        routing_provider=pending_attempt.decision.model.provider,
                        routing_model=pending_attempt.decision.model.model,
                        routing_prompt_name=prompt.name,
                        routing_prompt_version=prompt.version,
                        routing_reason=pending_attempt.decision.reason,
                        fallback_used=pending_attempt.decision.fallback_used,
                        region=pending_attempt.region,
                    )
        self._observe_metrics(
            task=task.name,
            attempts=pending_attempts,
            winning_attempt=winning_attempt,
        )
        logger.info(
            "ai.request.succeeded",
            ai_request_id=result.request_id,
            task=result.routing.task,
            provider=result.routing.provider,
            model=result.routing.model,
            prompt_name=result.routing.prompt_name,
            prompt_version=result.routing.prompt_version,
            fallback_used=result.routing.fallback_used,
            region=result.routing.region,
            latency_ms=winning_attempt.latency_ms if winning_attempt is not None else None,
            cost=str(result.cost.amount),
        )
        return result

    def _observe_metrics(
        self,
        *,
        task: str,
        attempts: list[_PendingAttempt],
        winning_attempt: _PendingAttempt | None,
    ) -> None:
        """Record the aggregate AI observability signal (v0.7 Scope §6.7).

        One sample per settled attempt, mirroring the durable ``ai_requests``
        rows attempt for attempt: the winning attempt is ``succeeded``, every
        other attempt is ``failed`` with its own usage, latency and usage-priced
        cost. Only low-cardinality registry ids become labels; organisation
        ids, request ids and content never do (BP §28, ADR-0017). Validation
        failures are observed at the point they occur with the failing model;
        retries and reviewed fallbacks are attributed to the attempt that
        caused them.
        """
        for index, attempt in enumerate(attempts):
            observe_ai_attempt(
                task=task,
                provider=attempt.decision.model.provider,
                model=attempt.decision.model.model,
                status="succeeded" if attempt is winning_attempt else "failed",
                latency_ms=attempt.latency_ms,
                input_tokens=attempt.usage.input_tokens,
                output_tokens=attempt.usage.output_tokens,
                cost_usd=float(
                    self._estimate_cost(
                        attempt.model.pricing.input_price_per_million_tokens,
                        attempt.model.pricing.output_price_per_million_tokens,
                        attempt.usage,
                        currency=attempt.model.pricing.currency,
                    ).amount
                ),
            )
            if index > 0:
                observe_ai_retry(
                    task=task,
                    provider=attempt.decision.model.provider,
                    model=attempt.decision.model.model,
                )
            if attempt.decision.fallback_used:
                observe_ai_fallback(
                    task=task,
                    provider=attempt.decision.model.provider,
                    model=attempt.decision.model.model,
                )

    def _aggregate_cost(
        self,
        attempts: list[_PendingAttempt],
        currency: str,
    ) -> CostEstimate:
        """Sum every attempt's usage-priced cost, each priced with its own model.

        Aggregating per-attempt costs (each with its own model's rates) is what
        makes retries and fallback across differently priced models account
        correctly (v0.7 Scope §2/§6.5). ``currency`` is the winning attempt's
        pricing currency; the registry prices every model in the same currency.
        """
        total = sum(
            (
                self._estimate_cost(
                    attempt.model.pricing.input_price_per_million_tokens,
                    attempt.model.pricing.output_price_per_million_tokens,
                    attempt.usage,
                    currency=attempt.model.pricing.currency,
                ).amount
            )
            for attempt in attempts
        )
        return CostEstimate(amount=Decimal(total), currency=currency)

    async def _resolve_attachments(
        self,
        request: AIRequest,
        attachments: Sequence[Attachment] | None,
    ) -> list[Attachment]:
        """Determine the validated attachment set for one request.

        Explicit ``attachments`` (already resolved by the caller) and a
        request ``storage_reference`` are mutually exclusive; a storage
        reference is resolved through the configured resolver at the service
        boundary (v0.7 Scope §6.4, ADR-0017) and validated against the template
        limits before any routing or dispatch.
        """
        if attachments:
            if request.storage_reference is not None:
                raise AIInputValidationError(
                    "attachments and a storage_reference are mutually exclusive"
                )
            return self._validate_attachments(attachments)
        if request.storage_reference is not None:
            if self._attachment_resolver is None:
                raise AIInputValidationError(
                    "storage references require a configured attachment resolver"
                )
            resolved = await self._attachment_resolver(
                AttachmentResolutionContext(
                    reference=request.storage_reference,
                    organisation_id=request.organisation_id,
                )
            )
            try:
                return validate_attachment_set(resolved)
            except ValueError as exc:
                raise AIInputValidationError(str(exc)) from exc
        return []

    async def _head_large_source(self, request: AIRequest) -> _LargeSource | None:
        """Head a storage-referenced object and route oversized ones to the seam.

        v0.8 Scope §2.3: the non-inline path is decided from head metadata
        *before* any bytes are read. Returns ``None`` when the reference is
        at or below the deployment's inline threshold (the inline resolver
        handles it), the service has no storage/deployment wiring (the inline
        path will fail closed with its own error), or the object is missing
        (the inline resolver reports the safe missing-object error). For an
        oversized object it returns the head facts with an allowlisted MIME
        type; the verified copy and digest are streamed by the seam only after
        mode selection.
        """
        if request.storage_reference is None or self._storage is None:
            return None
        info = await self._storage.head_object(request.storage_reference)
        if info is None:
            return None
        if info.size_bytes <= self._transfer_deployment.inline_aggregate_threshold_bytes:
            return None
        return _LargeSource(
            reference=request.storage_reference,
            size_bytes=info.size_bytes,
            mime_type=_resolve_large_mime_type(info.content_type, request.storage_reference),
            display_name=Path(request.storage_reference.rstrip("/")).name or "document",
        )

    def _output_json_schema(self, output_schema: str | None) -> dict[str, Any] | None:
        """Generate the JSON Schema for the task's Pydantic output model.

        Resolving the schema here — before dispatch — makes an unknown schema
        a fail-fast :class:`OutputSchemaError` (v0.7 Scope §6.2/§6.4) and supplies
        the adapter with the shape for native structured output where the
        adapter truthfully supports it.
        """
        if output_schema is None:
            return None
        model_class = self._schema_resolver(output_schema)
        return model_class.model_json_schema()

    @staticmethod
    def _validate_attachments(
        attachments: Sequence[Attachment] | None,
    ) -> list[Attachment]:
        """Validate the resolved attachment set against the template limits.

        Each :class:`Attachment` is already validated at construction (MIME
        allowlist, per-file size, digest); this enforces the bounded count and
        combined 10 MB ceiling and translates a safe ``ValueError`` into the
        AI input-validation taxonomy before any routing or dispatch.
        """

        if not attachments:
            return []
        try:
            return validate_attachment_set(attachments)
        except ValueError as exc:
            raise AIInputValidationError(str(exc)) from exc

    def _resolve_task(self, name: str) -> TaskDefinition:
        try:
            return self._task_registry.get(name)
        except KeyError as exc:
            raise TaskNotFoundError(f"unknown task: {name}") from exc

    def _resolve_prompt(self, name: str, version: int) -> PromptDefinition:
        try:
            return self._prompt_registry.get(name, version)
        except KeyError as exc:
            raise PromptNotFoundError(f"unknown prompt: {name} v{version}") from exc

    def _validate_input_form(self, prompt: PromptDefinition, request: AIRequest) -> None:
        """Fail fast when the request's input form cannot satisfy the prompt.

        v0.7 Scope §6.4 input normalisation: a prompt that declares ``text`` needs
        text input, ``messages`` needs message input, and ``storage_reference``
        needs a storage reference; metadata variables are satisfied by the
        request's bounded metadata. This runs before attachment resolution so
        the informative error wins over a missing-resolver or missing-object
        error when both conditions exist.
        """
        for variable in prompt.input_variables:
            if variable in request.metadata:
                continue
            if variable == "text" and request.text is None:
                raise AIInputValidationError("task requires text input")
            if variable == "messages" and request.messages is None:
                raise AIInputValidationError("task requires message input")
            if variable == "storage_reference" and request.storage_reference is None:
                raise AIInputValidationError("task requires a storage reference")

    def _render_prompt(
        self,
        prompt: PromptDefinition,
        request: AIRequest,
        attachments: list[Attachment],
        reference_display_names: Sequence[str] | None = None,
    ) -> str:
        """Render the prompt template with allowlisted variables only.

        Only identifiers the prompt declares are substituted; undeclared
        placeholders are left untouched (never evaluated), missing declared
        variables fail fast. This is the safe renderer v0.7 Scope §6.2 validates
        against the registry — no arbitrary template execution, no secrets.

        v0.7 Scope §6.4 input normalisation: text and message content pass through
        the configured redaction hook before the prompt is built, so sensitive
        input never reaches the provider. A ``storage_reference`` variable
        renders only the resolved attachments' approved display names — the
        private reference itself is never rendered as if it were document
        content (ADR-0017) and never reaches the provider. A v0.8 large source
        (above the inline threshold) has no inline attachment set; its display
        name is supplied through ``reference_display_names`` so the reference
        renders identically without any bytes being read.
        """

        values: dict[str, str] = {}
        for variable in prompt.input_variables:
            if variable in request.metadata:
                values[variable] = request.metadata[variable]
            elif variable == "text":
                if request.text is None:
                    raise AIInputValidationError("task requires text input")
                values[variable] = self._redact(request.text)
            elif variable == "messages":
                if request.messages is None:
                    raise AIInputValidationError("task requires message input")
                values[variable] = "\n".join(
                    f"{message.role}: {self._redact(message.content)}"
                    for message in request.messages
                )
            elif variable == "storage_reference":
                if request.storage_reference is None:
                    raise AIInputValidationError("task requires a storage reference")
                names = [attachment.display_name for attachment in attachments]
                if not names and reference_display_names:
                    names = list(reference_display_names)
                if not names:
                    raise AIInputValidationError(
                        "task requires a storage reference but no attachment was resolved"
                    )
                values[variable] = ", ".join(names)
            else:
                raise AIInputValidationError(f"task requires input variable {variable!r}")

        try:
            rendered = prompt.render(values)
        except ValueError as exc:
            raise AIInputValidationError("prompt input failed safe rendering") from exc
        return f"Task: {request.task}\n{rendered}"

    async def _call_provider(self, provider: LLMProvider, provider_request: ProviderRequest) -> Any:
        try:
            return await provider.complete(provider_request)
        except (AIError, ProviderError):
            raise
        except Exception as exc:
            # Normalise unexpected adapter failures into the safe taxonomy.
            # Only the exception category is logged — never the message, which
            # could contain URLs, credentials or content (BP §28, ADR-0017).
            logger.warning(
                "ai.provider.unexpected_failure",
                provider=provider.provider_id,
                model=provider_request.model,
                exception_type=type(exc).__name__,
            )
            raise ProviderResponseError("provider returned an unexpected error") from exc

    def _prepare_repair_request(
        self,
        task: TaskDefinition,
        request: ProviderRequest,
        previous_content: str,
        model: ModelDefinition,
        *,
        maximum_estimated_cost: Decimal | None,
    ) -> ProviderRequest:
        """Build one bounded repair request after failed Pydantic validation.

        v0.7 Scope §6.4: the repair reuses the same approved routing/policy path —
        identical provider, model, schema and parameters — with a repair
        instruction and the truncated previous output appended to the prompt.
        The response is validated again; a second validation failure raises
        :class:`OutputValidationError` (terminal for this attempt) so
        unvalidated structured data is never returned.

        The appended repair context enlarges the prompt, so the task/model
        context and the request cost ceilings are re-applied to the repair
        prompt before dispatch (v0.7 Scope §6.4/§6.5): a repair can never push a
        request over a reviewed bound. Exceeding a bound is terminal — retrying
        the identical malformed cycle cannot shrink the prompt. Returns the
        provider call itself happens in :meth:`execute`, after its distinct
        durable attempt row has been created.
        """
        previous = previous_content[:MAX_REPAIR_CONTEXT_LENGTH]
        repair_prompt = f"{request.prompt}{_REPAIR_INSTRUCTION.format(previous=previous)}"
        repair_input_tokens = estimate_tokens(repair_prompt)
        max_output_tokens = int(task.parameter_defaults.get("max_tokens", 0))
        if repair_input_tokens > task.max_input_tokens or (
            repair_input_tokens + max_output_tokens > model.context_window
        ):
            raise RepairNotPossibleError(
                "provider output cannot be repaired within the task context limit"
            )
        cost_limit = maximum_estimated_cost
        if task.max_estimated_cost is not None:
            cost_limit = (
                min(cost_limit, task.max_estimated_cost)
                if cost_limit is not None
                else task.max_estimated_cost
            )
        if cost_limit is not None:
            repair_cost = estimate_maximum_cost(task, model, repair_input_tokens)
            if repair_cost > cost_limit:
                raise RepairNotPossibleError(
                    "provider output cannot be repaired within the cost limit"
                )
        return request.model_copy(
            update={
                "prompt": repair_prompt,
                "repair": True,
            }
        )

    def _validate_output(
        self,
        output_schema: str | None,
        declares_text_result: bool,
        content: str,
        structured: dict[str, Any] | None,
    ) -> Any:
        """Validate the provider output against the declared contract.

        A task with an ``output_schema`` must produce data that validates
        against it — malformed JSON or schema mismatch is an
        :class:`OutputValidationError`, never success. Free text is allowed
        only when the task explicitly declares a text result (v0.7 Scope §6.4).
        """

        if output_schema is None:
            if declares_text_result:
                return content
            raise OutputValidationError("task declares neither an output schema nor a text result")
        model_class = self._schema_resolver(output_schema)
        raw = structured if structured is not None else content
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise OutputValidationError("provider returned malformed JSON") from exc
        try:
            validated = model_class.model_validate(raw)
        except ValidationError as exc:
            raise OutputValidationError("provider output failed schema validation") from exc
        return validated

    @staticmethod
    def _estimate_cost(
        input_price_per_million: Decimal,
        output_price_per_million: Decimal,
        usage: TokenUsage,
        *,
        currency: str = "USD",
    ) -> CostEstimate:
        input_cost = input_price_per_million * Decimal(usage.input_tokens) / Decimal(1_000_000)
        output_cost = output_price_per_million * Decimal(usage.output_tokens) / Decimal(1_000_000)
        # The durable records store cost as NUMERIC(18,6) (v0.7 Scope §6.5, BP
        # §10); the accounting rounds at that declared storage precision so a
        # tiny token count can never produce an unrepresentable cost.
        amount = (input_cost + output_cost).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return CostEstimate(amount=amount, currency=currency)
