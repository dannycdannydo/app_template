"""Unit tests for the upload scanning boundary (plan P6, blueprint §30).

The scanner seam is provider-neutral: the template ships the reviewed
``NoScanner`` position and the worker gates ``ready`` on the verdict. These
hermetic tests prove the closed verdict set, the settings-driven factory and
that an unavailable scanner propagates as a transient failure rather than a
clean verdict (so unscanned content can never become trusted).
"""

from __future__ import annotations

import pytest

from app.modules.files import service as files_service
from app.scanning import (
    ContentScanner,
    NoScanner,
    ScannerUnavailableError,
    ScanVerdict,
    get_scanner,
)
from app.storage import ObjectStorage


def test_no_scanner_returns_not_required() -> None:
    async def _run() -> ScanVerdict:
        return await NoScanner().scan(
            storage=object(),  # type: ignore[arg-type]
            object_key="organisations/x/documents/y/original",
            content_type="application/pdf",
            max_bytes=1024,
        )

    import asyncio

    assert asyncio.run(_run()) is ScanVerdict.NOT_REQUIRED


def test_factory_returns_no_scanner_for_the_reviewed_default() -> None:
    get_scanner.cache_clear()
    assert isinstance(get_scanner(), NoScanner)


def test_scan_file_object_propagates_scanner_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unavailable scanner raises transiently; it is never a clean verdict."""

    class _UnavailableScanner(ContentScanner):
        async def scan(
            self,
            *,
            storage: ObjectStorage,
            object_key: str,
            content_type: str | None,
            max_bytes: int,
        ) -> ScanVerdict:
            raise ScannerUnavailableError("scanner down")

    monkeypatch.setattr(files_service, "get_scanner", lambda: _UnavailableScanner())

    import asyncio

    with pytest.raises(ScannerUnavailableError):
        asyncio.run(
            files_service.scan_file_object(
                object_key="organisations/x/documents/y/original",
                content_type="application/pdf",
            )
        )
