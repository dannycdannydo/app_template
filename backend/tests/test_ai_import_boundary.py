"""Import-boundary tests for the AI layer (v0.7 Scope §6.1/§6.3, ADR-0017).

The provider-neutral contract is enforced structurally: no provider SDK may be
imported outside ``app/ai/providers/``. This mirrors the storage boto3 rule
(ADR-0014) and keeps the promise that feature modules call ``AIService`` by
task name and never see an SDK. The Scope §6.3 adapters are thin pinned HTTP
clients (httpx, google-auth for Vertex credentials only) and any future SDK
import is confined to the same directory; this test guards the boundary from
day one so an adapter can never leak.
"""

from __future__ import annotations

from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"

# Top-level SDK modules that belong exclusively inside app/ai/providers/.
# Imports may appear as `import x`, `from x import y` or `import x.y`; the
# first dotted component is what we match.
AI_PROVIDER_SDKS = ("openai", "anthropic", "deepseek", "vertexai", "google", "azure")
ALLOWED_DIR = "app/ai/providers"


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
