"""Bounded, digest-verifying multipart upload wrapper (v0.8 Scope §2.3, §6.5/§6.6).

The provider upload stores (OpenAI and Anthropic Files APIs) must stream a
verified secure temporary file into a provider multipart upload without ever
accumulating the large-file ceiling in Python memory, and must be able to
prove the uploaded bytes are byte-identical to the object the transfer
verified (Scope §2.3). httpx encodes multipart ``files`` fields by calling
``read(chunk)`` repeatedly (64 KiB at a time), so wrapping the verified file
with :class:`DigestUploadFile` folds every uploaded byte into a running
SHA-256 and reports a real ``Content-Length`` through ``fileno`` (never
chunked transfer encoding) without reading ahead.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class DigestUploadFile:
    """A bounded file wrapper folding every uploaded byte into a SHA-256.

    The adapter computes the SHA-256 of exactly the uploaded bytes (never the
    whole 50 MB ceiling in Python memory, Scope §2.3), then compares it with
    the verified source digest after the upload: a source that changed while
    being streamed is detected and the just-uploaded provider copy is removed
    instead of being accepted with unverified content.
    """

    def __init__(self, path: Path, *, size_bytes: int, chunk_size: int) -> None:
        self._handle = path.open("rb")
        self._size_bytes = size_bytes
        self._chunk_size = chunk_size
        self._sha256 = hashlib.sha256()
        self._read = 0
        self._closed = False

    def fileno(self) -> int:
        """Delegate to the underlying file so httpx can stat the exact length."""
        return self._handle.fileno()

    def read(self, size: int = -1) -> bytes:
        """Read one bounded chunk and fold it into the running SHA-256."""
        read_size = self._chunk_size if size < 0 else max(1, min(size, self._chunk_size))
        chunk = self._handle.read(read_size)
        if chunk:
            self._read += len(chunk)
            self._sha256.update(chunk)
        return chunk

    @property
    def sha256_hex(self) -> str:
        """The SHA-256 of exactly the uploaded bytes (hex)."""
        return self._sha256.hexdigest()

    @property
    def read_bytes(self) -> int:
        """Total bytes forwarded to the upload so far."""
        return self._read

    @property
    def size_bytes(self) -> int:
        """The verified size the upload must not exceed."""
        return self._size_bytes

    def close(self) -> None:
        if not self._closed:
            self._handle.close()
            self._closed = True


__all__ = ["DigestUploadFile"]
