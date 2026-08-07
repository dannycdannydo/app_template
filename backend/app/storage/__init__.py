"""Provider-neutral object storage (blueprint §17, ADR-0006, Scope §6.1).

Application code imports the :class:`ObjectStorage` interface from here and
never a provider SDK; the concrete adapter is selected from settings through
:func:`get_storage`. Adding a new provider means adding one adapter class in
``app/storage/`` — no other module changes.
"""

from app.storage.base import DEFAULT_SIGNED_URL_TTL, ObjectStorage
from app.storage.factory import get_storage
from app.storage.fake import FakeObjectStorage
from app.storage.types import ObjectInfo, SignedUrl

__all__ = [
    "DEFAULT_SIGNED_URL_TTL",
    "FakeObjectStorage",
    "ObjectInfo",
    "ObjectStorage",
    "SignedUrl",
    "get_storage",
]
