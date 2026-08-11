"""Provider-neutral attachment contract tests (v0.7 Scope §6.2 amendment).

The ``Attachment`` type is the validated carrier for bounded inline document
input: display name, MIME type, bytes and a SHA-256 digest computed from the
bytes. These tests pin the template limits (5 MB per file, 10 MB combined, a
bounded count and a reviewed MIME allowlist) so the registry/router/service
rejection logic has a trustworthy carrier to run on.
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from app.ai.attachments import (
    IMAGE_ATTACHMENT_MIME_TYPES,
    MAX_ATTACHMENT_BYTES,
    Attachment,
    validate_attachment_set,
)


def _attachment(
    *,
    name: str = "lease.pdf",
    mime_type: str = "application/pdf",
    content: bytes = b"%PDF-1.7 fictional fixture",
) -> Attachment:
    return Attachment(display_name=name, mime_type=mime_type, content=content)


def test_attachment_carries_validated_fields_and_computed_digest() -> None:
    attachment = _attachment()
    assert attachment.display_name == "lease.pdf"
    assert attachment.mime_type == "application/pdf"
    assert attachment.content == b"%PDF-1.7 fictional fixture"
    assert attachment.sha256_digest == hashlib.sha256(attachment.content).hexdigest()
    assert attachment.size == len(attachment.content)
    # The digest is derived from content: a supplied value is never accepted
    # and can never drift from the carried bytes.
    with pytest.raises(ValidationError, match="sha256_digest"):
        Attachment.model_validate(
            {
                "display_name": "lease.pdf",
                "mime_type": "application/pdf",
                "content": b"different bytes",
                "sha256_digest": "0" * 64,
            }
        )


def test_attachment_is_immutable_after_construction() -> None:
    """A validated carrier cannot be mutated into an invalid or stale object:
    no field may be reassigned after construction, so ``validate_attachment_set``
    always sees a consistent, still-valid object."""
    attachment = _attachment()
    with pytest.raises(ValidationError):
        attachment.mime_type = "application/octet-stream"
    with pytest.raises(ValidationError):
        attachment.content = b"mutated bytes"
    with pytest.raises(ValidationError):
        attachment.display_name = "other.pdf"
    assert attachment.mime_type == "application/pdf"
    assert attachment.sha256_digest == hashlib.sha256(attachment.content).hexdigest()
    # Unknown fields are rejected at construction, never silently stored.
    with pytest.raises(ValidationError, match="extra"):
        Attachment.model_validate(
            {
                "display_name": "lease.pdf",
                "mime_type": "application/pdf",
                "content": b"%PDF-1.7 fixture",
                "surprise": "injected",
            }
        )


@pytest.mark.parametrize(
    "mime_type",
    ["text/plain", "text/markdown", "text/csv", "application/json", "image/png", "image/webp"],
)
def test_attachment_accepts_allowlisted_mime_types(mime_type: str) -> None:
    assert _attachment(mime_type=mime_type).mime_type == mime_type


@pytest.mark.parametrize(
    "mime_type",
    [
        "text/html",
        "application/octet-stream",
        "application/vnd.ms-excel",
        "image/gif",
        "text/x-python",
    ],
)
def test_attachment_rejects_mime_types_outside_the_allowlist(mime_type: str) -> None:
    with pytest.raises(ValidationError, match="MIME"):
        _attachment(mime_type=mime_type)


@pytest.mark.parametrize(
    "name",
    [
        "../lease.pdf",
        "a/b.pdf",
        "a\\b.pdf",
        ".hidden",
        "name\x00with-null",
        "name\nwith-newline",
        "name\rwith-cr",
        "name\twith-tab",
        "name\x1fwith-unit-separator",
        "name\x7fwith-del",
        "x" * 256,
        "   ",
    ],
)
def test_attachment_rejects_unsafe_display_names(name: str) -> None:
    with pytest.raises(ValidationError, match="display_name"):
        _attachment(name=name)


def test_attachment_rejects_empty_and_oversized_content() -> None:
    with pytest.raises(ValidationError, match="empty"):
        _attachment(content=b"")
    with pytest.raises(ValidationError, match="per-file"):
        _attachment(content=b"x" * (MAX_ATTACHMENT_BYTES + 1))


def test_attachment_set_enforces_count_and_combined_size() -> None:
    small = _attachment(name="a.pdf", content=b"a" * 1024)
    assert validate_attachment_set([small]) == [small]

    too_many = [
        _attachment(name=f"f-{i}.txt", mime_type="text/plain", content=b"x") for i in range(17)
    ]
    with pytest.raises(ValueError, match="too many"):
        validate_attachment_set(too_many)

    big_enough_chunk = MAX_ATTACHMENT_BYTES
    three = [
        _attachment(name=f"f-{i}.txt", mime_type="text/plain", content=b"x" * big_enough_chunk)
        for i in range(3)
    ]
    with pytest.raises(ValueError, match="combined"):
        validate_attachment_set(three)


def test_attachment_image_modality_flag() -> None:
    """Image MIME types are a distinct modality: the router requires the
    model's ``vision`` capability for them, never for documents."""
    for mime_type in IMAGE_ATTACHMENT_MIME_TYPES:
        assert _attachment(name="scan", mime_type=mime_type, content=b"\x89PNG").is_image is True
    assert _attachment().is_image is False
    assert _attachment(name="notes.md", mime_type="text/markdown").is_image is False
