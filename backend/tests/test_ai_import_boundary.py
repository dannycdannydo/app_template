"""Import-boundary tests for the AI layer (v0.7 Scope §6.1/§6.3, ADR-0017).

The provider-neutral contract is enforced structurally: no provider SDK may be
imported outside ``app/ai/providers/``. This mirrors the storage boto3 rule
(ADR-0014) and keeps the promise that feature modules call ``AIService`` by
task name and never see an SDK. The Scope §6.3 adapters are thin pinned HTTP
clients (httpx, google-auth for Vertex credentials only) and any future SDK
import is confined to the same directory; this test guards the boundary from
day one so an adapter can never leak.

v0.8 Scope §6.1 checkbox 3 adds the transfer-mode boundary: the
provider-neutral transfer/reference contracts in ``app/ai/transfer.py`` and
``app/ai/staging.py`` are internal to the AI layer — feature modules must not
name a transfer mode or a provider reference — and ``AIRequest`` remains
unchanged (a feature supplies only a task name and a private storage
reference, never a transfer mode, provider file id, ``gs://`` URI or URL).
"""

from __future__ import annotations

from pathlib import Path

from app.ai.schemas import AIRequest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"

# Top-level SDK modules that belong exclusively inside app/ai/providers/.
# Imports may appear as `import x`, `from x import y` or `import x.y`; the
# first dotted component is what we match.
AI_PROVIDER_SDKS = ("openai", "anthropic", "deepseek", "vertexai", "google", "azure")
ALLOWED_DIR = "app/ai/providers"
AI_DEMO_ROOT = APP_ROOT / "modules" / "ai_demo"

# v0.8 Scope §6.1 checkbox 3: the provider-neutral transfer contracts live
# under ``app/ai/`` and are internal. Feature modules may import ``AIService``
# and the request/result schemas, but never the transfer-mode contract module,
# the staging seam, or the provider contract fixtures — transfer modes and
# provider references are selected and constructed inside the AI layer only.
AI_INTERNAL_CONTRACT_MODULES = (
    "app.ai.transfer",
    "app.ai.staging",
    "app.ai.contracts",
    # §6.3 internal seams: the streaming/temp-file carrier, the managed-URL
    # minter, the durable reference store and the orchestrator all construct
    # or name transfer modes and provider references — they are internal to
    # app/ai/ exactly like the contract modules.
    "app.ai.streamed_source",
    "app.ai.managed_url",
    "app.ai.persistence.references",
    "app.ai.transfer_orchestrator",
    # §6.4 internal seam: the provider-neutral Vertex GCS staging contracts
    # and fake construct ``gs://`` references and name the storage_reference
    # mode — internal to app/ai/ exactly like the contract modules. The real
    # adapter lives under app/ai/providers/ and is already covered by the
    # providers-only SDK rule.
    "app.ai.vertex_staging",
)


def _sdk_import_lines() -> list[tuple[Path, int, str]]:
    """Return (file, line_number, line) for every line importing a provider SDK."""
    hits: list[tuple[Path, int, str]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for sdk in AI_PROVIDER_SDKS:
                if _matches_import(stripped, sdk):
                    hits.append((path, index, stripped))
                    break
    return hits


def _matches_import(line: str, sdk: str) -> bool:
    return line.startswith((f"import {sdk}", f"import {sdk}.", f"from {sdk}", f"from {sdk}."))


def _matches_module_import(line: str, module: str) -> bool:
    return line.startswith((f"import {module}", f"from {module}"))


def test_no_provider_sdk_imported_outside_app_ai_providers() -> None:
    """Every provider-SDK import lives under app/ai/providers/ (ADR-0017)."""
    violations = [
        (path.relative_to(BACKEND_ROOT).as_posix(), line_number, line)
        for path, line_number, line in _sdk_import_lines()
        if not path.relative_to(BACKEND_ROOT).as_posix().startswith(f"{ALLOWED_DIR}/")
    ]
    assert violations == [], (
        "provider SDKs must only be imported inside app/ai/providers/ "
        f"(ADR-0017); found: {violations}"
    )


def test_ai_demo_does_not_import_ai_persistence_internals() -> None:
    """The example feature consumes the platform execution boundary only."""
    violations: list[tuple[str, int, str]] = []
    for path in sorted(AI_DEMO_ROOT.rglob("*.py")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("from app.ai.persistence", "import app.ai.persistence")):
                violations.append(
                    (path.relative_to(BACKEND_ROOT).as_posix(), line_number, stripped)
                )
    assert violations == [], (
        f"feature modules must not import app.ai.persistence internals; found: {violations}"
    )


def test_transfer_contracts_are_not_imported_outside_app_ai() -> None:
    """Feature modules cannot name transfer modes or provider references.

    v0.8 Scope §6.1 checkbox 3: every import of the transfer contract module,
    the staging seam or the provider contract fixtures must live inside
    ``app/ai/``. A feature module importing ``app.ai.transfer`` could name a
    transfer mode or construct a provider reference, which the Scope §2.2
    caller boundary forbids — the caller supplies only a task name and a
    private storage reference.
    """
    violations: list[tuple[str, int, str]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(BACKEND_ROOT).as_posix()
        if relative.startswith("app/ai/"):
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for module in AI_INTERNAL_CONTRACT_MODULES:
                if _matches_module_import(stripped, module):
                    violations.append((relative, line_number, stripped))
                    break
    assert violations == [], (
        "transfer modes and provider references are internal to app/ai/ "
        f"(v0.8 Scope §2.2); found: {violations}"
    )


def test_feature_modules_do_not_name_transfer_mode_literals() -> None:
    """Feature code never names a transfer mode or constructs a gs:// reference.

    A structural guard complementing the import check: modules outside
    ``app/ai/`` must not write the transfer-mode literal strings or build
    ``gs://`` / provider file-id references, because those concepts are
    internal to the AI layer (Scope §2.2). ``storage_reference`` itself is the
    legitimate caller-supplied field name and is not guarded here — a caller
    can never *select* a mode, since ``AIRequest`` has no mode field. Comments
    are ignored.
    """
    forbidden = {"provider_upload", "managed_signed_url", "gs://"}
    # The v0.8 §6.7 observability seam is the single sanctioned boundary
    # outside ``app/ai/`` that may name a mode: the low-cardinality
    # ``ai_transfer_cleanup_backlog{mode="provider_upload"}`` gauge label (the
    # mode name is the label value, never a routing/selection input — Scope
    # §2.2's caller boundary is untouched).
    allowed_observability = {
        ("app/observability/metrics.py", "provider_upload"),
    }
    violations: list[tuple[str, int, str]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(BACKEND_ROOT).as_posix()
        if relative.startswith("app/ai/"):
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for token in forbidden:
                if token in stripped and (relative, token) not in allowed_observability:
                    violations.append((relative, line_number, stripped))
                    break
    assert violations == [], (
        "feature modules must not name transfer modes or provider references "
        f"(v0.8 Scope §2.2); found: {violations}"
    )


def test_ai_request_contract_is_unchanged() -> None:
    """The application-facing request carries no transfer/provider fields.

    v0.8 Scope §2.2: ``AIRequest`` remains the same contract — task, text/
    messages/storage_reference, output_schema, organisation/user ids and
    bounded metadata. A caller can never request or override a transfer mode,
    and no provider file id, ``gs://`` URI, URL or provider name field may be
    added to the request schema.
    """
    forbidden_fields = {
        "transfer_mode",
        "provider",
        "provider_file_id",
        "provider_reference",
        "gs_uri",
        "url",
        "signed_url",
    }
    present = set(AIRequest.model_fields)
    assert present.isdisjoint(forbidden_fields), (
        "AIRequest must not expose transfer-mode or provider-reference fields "
        f"(v0.8 Scope §2.2); found: {sorted(present & forbidden_fields)}"
    )
    assert "storage_reference" in present
    assert "task" in present
