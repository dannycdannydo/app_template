"""Scanner adapter selection from typed settings (plan P6).

The process-wide scanner is chosen once from ``FILE_SCAN_PROVIDER``; only the
reviewed ``none`` adapter ships today. Adding a provider means adding one
adapter class behind this factory and widening the settings validator — no
file-lifecycle change.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.scanning.base import ContentScanner, NoScanner


@lru_cache
def get_scanner() -> ContentScanner:
    """Return the configured upload scanner (default: the no-scanner position)."""
    provider = get_settings().file_scan_provider
    if provider == "none":
        return NoScanner()
    # Unreachable while the settings validator only accepts 'none'; kept
    # fail-closed so a future adapter cannot be silently omitted.
    raise ValueError(f"unsupported file_scan_provider: {provider!r}")
