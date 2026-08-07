"""Storage provider factory wired from settings (blueprint §17, Scope §6.1).

``get_storage`` is the process-wide singleton for the object storage adapter,
mirroring ``get_settings``: it reads the selected provider from settings once
and returns the same instance for the lifetime of the process. The pytest
suite pins ``STORAGE_PROVIDER=fake`` in ``tests/conftest.py``, so the default
suite never constructs a real provider (Scope §6.2).
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.storage.base import ObjectStorage
from app.storage.fake import FakeObjectStorage
from app.storage.s3 import S3Storage


@lru_cache
def get_storage() -> ObjectStorage:
    """Return the process-wide :class:`ObjectStorage` selected by settings."""
    settings = get_settings()
    if settings.storage_provider == "fake":
        return FakeObjectStorage(bucket=settings.storage_bucket)
    if settings.storage_provider == "s3":
        return S3Storage(
            bucket=settings.storage_bucket,
            endpoint_url=settings.storage_endpoint_url,
            region=settings.storage_region,
            access_key_id=settings.storage_access_key_id,
            secret_access_key=settings.storage_secret_access_key,
            public_endpoint_url=settings.storage_public_endpoint_url,
        )
    raise ValueError(f"unknown storage_provider: {settings.storage_provider!r}")
