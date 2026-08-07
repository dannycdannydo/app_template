"""Storage provider factory wired from settings (blueprint §17, Scope §6.1).

``get_storage`` is the process-wide singleton for the object storage adapter,
mirroring ``get_settings``: it reads the selected provider from settings once
and returns the same instance for the lifetime of the process. The pytest
suite pins ``STORAGE_PROVIDER=fake`` in ``tests/conftest.py``, so the default
suite never constructs a real provider.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.storage.base import ObjectStorage
from app.storage.fake import FakeObjectStorage


@lru_cache
def get_storage() -> ObjectStorage:
    """Return the process-wide :class:`ObjectStorage` selected by settings."""
    settings = get_settings()
    if settings.storage_provider == "fake":
        return FakeObjectStorage(bucket=settings.storage_bucket)
    if settings.storage_provider == "s3":
        # The S3-compatible adapter lands in Scope §6.2. Until then the default
        # provider is unavailable, so a stack configured for S3 fails loudly
        # instead of silently using an in-memory stand-in.
        raise ValueError(
            "storage_provider=s3 requires the S3Storage adapter, which lands in "
            "Scope §6.2; set STORAGE_PROVIDER=fake for the test suite"
        )
    raise ValueError(f"unknown storage_provider: {settings.storage_provider!r}")
