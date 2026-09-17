"""Provider-neutral upload scanning contract (plan P6, blueprint §30).

The file-processing worker resolves a verified stored object through this
interface before it may become ``ready``. The verdict is a closed set:
``clean`` (trusted), ``quarantined`` (a definite rejection) or
``not_required`` (no scanner deployed — the explicit, reviewed template
position). An unavailable scanner raises :class:`ScannerUnavailableError`; the
worker treats that as a transient failure and must never promote the file to
``ready`` while the verdict is unknown, so a scanner outage cannot turn
unscanned content into trusted content.

A real adapter reads the object through the provider-neutral
:class:`~app.storage.base.ObjectStorage` interface (bounded by the template
upload ceiling) and never returns provider-specific exceptions across this
seam.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from app.storage.base import ObjectStorage


class ScanVerdict(StrEnum):
    """The closed set of upload-scanning outcomes (plan P6)."""

    CLEAN = "clean"
    QUARANTINED = "quarantined"
    NOT_REQUIRED = "not_required"


class ScannerUnavailableError(Exception):
    """Raised when the scanner cannot produce a verdict.

    The worker treats this as a transient failure: the file stays
    ``processing`` and the attempt retries, so unscanned bytes are never
    promoted to ``ready`` while the scanner is down.
    """


class ContentScanner(ABC):
    """The seam every upload scanner adapter implements."""

    @abstractmethod
    async def scan(
        self,
        *,
        storage: ObjectStorage,
        object_key: str,
        content_type: str | None,
        max_bytes: int,
    ) -> ScanVerdict:
        """Return a verdict for one stored object.

        Raises :class:`ScannerUnavailableError` when no verdict can be produced
        (network failure, scanner error, timeout) — never a provider-specific
        exception.
        """


class NoScanner(ContentScanner):
    """The reviewed no-scanner position: every upload is ``not_required``.

    This is the explicit template default (``FILE_SCAN_PROVIDER=none``) and
    must be acknowledged in production. It never raises, so existing file
    journeys are unchanged while the quarantine state and the adapter seam
    remain available to a deployment that adds a real scanner.
    """

    async def scan(
        self,
        *,
        storage: ObjectStorage,
        object_key: str,
        content_type: str | None,
        max_bytes: int,
    ) -> ScanVerdict:
        return ScanVerdict.NOT_REQUIRED
