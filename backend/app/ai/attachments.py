"""Provider-neutral bounded inline attachments (v0.7 Scope §6.2 amendment).

The v0.7 attachment contract (ADR-0017 amendment, BP §23 "AI providers") lets
a feature supply a private ``storage_reference`` for document-scale work; the
service/job boundary resolves that object into a provider-neutral
:class:`Attachment` carrying only a validated display name, MIME type, bytes
and SHA-256 digest. This module owns the attachment contract and the template
limits: 5 MB per attachment, 10 MB combined, a reviewed MIME allowlist and a
bounded count. No code here can see storage credentials, signed URLs or object
paths; bytes exist only in memory for one provider call and are never
persisted, placed on the broker, or written to logs/audit (ADR-0017).

The per-model inline ceilings live in the model registry (Scope §6.2) and the
router/service reject incompatible modality, MIME type and size combinations
before provider dispatch; this module provides the validated carrier those
checks run on.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, computed_field, field_validator

# Template attachment limits (ADR-0017 amendment, BP §23): one conservative
# bound for all v0.7 providers. Larger or provider-reference transfer modes are
# explicitly deferred to v0.8 (`plans/AI_LARGE_ATTACHMENTS_V0_8_PLAN.md`).
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 16
MAX_ATTACHMENT_DISPLAY_NAME_LENGTH = 255

# Reviewed template MIME allowlist: conservative, ordinary document/image types
# the v0.7 inline path accepts. Adapter-level modality restrictions (Scope §6.3)
# layer on top of this; anything outside the allowlist is rejected at
# construction time, before any routing or dispatch.
ALLOWED_ATTACHMENT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)

#: Image attachments are a distinct modality from documents: the router
#: additionally requires the model to declare the ``vision`` capability
#: (Scope §6.2) so an image can never route to a documents-only model before
#: provider dispatch.
IMAGE_ATTACHMENT_MIME_TYPES = frozenset(
    mime_type for mime_type in ALLOWED_ATTACHMENT_MIME_TYPES if mime_type.startswith("image/")
)

# Canonical per-provider inline MIME capability sets (v0.7 Scope §6.3
# attachment amendment, ADR-0017). Each set mirrors exactly what the matching
# adapter can carry natively in its wire format — the official provider
# contracts below — and is the single source of truth for both the registry
# model declarations (router-side) and the adapter pre-dispatch guards
# (defense in depth), so the two can never drift.
#
# - OpenAI/Azure chat completions: images ride ``image_url`` data-URI parts and
#   documents ride ``type=file`` parts whose file types include PDF, CSV, text,
#   Markdown and JSON (official contract:
#   https://developers.openai.com/api/docs/guides/file-inputs).
# - Anthropic Messages API: base64 ``image``/``document`` sources accept images
#   and PDF only; plain-text formats require a different representation and are
#   rejected before dispatch (official contract:
#   https://platform.claude.com/docs/en/build-with-claude/pdf-support).
# - Vertex AI ``generateContent``: ``inlineData`` accepts images, PDF and
#   plain text (official contract:
#   https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/document-understanding).
OPENAI_INLINE_ATTACHMENT_MIME_TYPES = frozenset(ALLOWED_ATTACHMENT_MIME_TYPES)
ANTHROPIC_INLINE_ATTACHMENT_MIME_TYPES = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "image/webp"}
)
VERTEX_INLINE_ATTACHMENT_MIME_TYPES = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "image/webp", "text/plain"}
)

#: Provider id → adapter inline MIME capability set. The fake accepts the full
#: allowlist (it records attachments deterministically without a wire format);
#: DeepSeek and local are intentionally absent because they declare no document
#: capability at all (ADR-0017).
PROVIDER_INLINE_ATTACHMENT_MIME_TYPES: dict[str, frozenset[str]] = {
    "openai": OPENAI_INLINE_ATTACHMENT_MIME_TYPES,
    "azure_openai": OPENAI_INLINE_ATTACHMENT_MIME_TYPES,
    "anthropic": ANTHROPIC_INLINE_ATTACHMENT_MIME_TYPES,
    "vertex": VERTEX_INLINE_ATTACHMENT_MIME_TYPES,
    "fake": OPENAI_INLINE_ATTACHMENT_MIME_TYPES,
}


class Attachment(BaseModel):
    """A validated, bounded, immutable document attachment in provider-neutral form.

    ``content`` is held in memory only for the duration of one provider call;
    ``sha256_digest`` is computed from ``content`` so the carried digest is by
    construction correct and can never go stale (ADR-0017: records persist the
    digest, never the bytes). The model is frozen and rejects unknown fields:
    after construction no attribute can be reassigned or injected, so a
    validated MIME type or digest cannot be mutated into an invalid or
    inconsistent carrier between validation and provider dispatch.
    ``display_name`` is a bare file name — no path separators, control
    characters or leading dots — so adapters can present it to the provider
    without path confusion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: str
    mime_type: str
    content: bytes

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        name = value.strip()
        if not name or len(name) > MAX_ATTACHMENT_DISPLAY_NAME_LENGTH:
            raise ValueError(
                "display_name must be between 1 and "
                f"{MAX_ATTACHMENT_DISPLAY_NAME_LENGTH} characters"
            )
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError("display_name must be a bare file name without path or dot prefixes")
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise ValueError("display_name must not contain control characters")
        return name

    @field_validator("mime_type")
    @classmethod
    def _validate_mime_type(cls, value: str) -> str:
        mime_type = value.strip().lower()
        if mime_type not in ALLOWED_ATTACHMENT_MIME_TYPES:
            raise ValueError(f"unsupported attachment MIME type: {value!r}")
        return mime_type

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: bytes) -> bytes:
        if not value:
            raise ValueError("attachment content must not be empty")
        if len(value) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"attachment exceeds the {MAX_ATTACHMENT_BYTES} byte per-file limit")
        return value

    @computed_field
    @property
    def sha256_digest(self) -> str:
        """SHA-256 of ``content``, always in sync with the carried bytes."""
        return hashlib.sha256(self.content).hexdigest()

    @property
    def size(self) -> int:
        """The attachment's byte size (matching ``len(content)``)."""
        return len(self.content)

    @property
    def is_image(self) -> bool:
        """Whether this attachment is an image (needs the ``vision`` modality)."""
        return self.mime_type in IMAGE_ATTACHMENT_MIME_TYPES


def validate_attachment_set(attachments: Sequence[Attachment]) -> list[Attachment]:
    """Validate a whole attachment set against the template limits.

    Enforces the bounded count and the 10 MB combined ceiling on top of each
    :class:`Attachment`'s own 5 MB per-file limit. Raises :class:`ValueError`
    with a safe, actionable message; the service translates that into the AI
    input-validation error before any routing or provider dispatch.
    """
    resolved = list(attachments)
    if len(resolved) > MAX_ATTACHMENT_COUNT:
        raise ValueError(f"too many attachments; limit is {MAX_ATTACHMENT_COUNT}")
    total_bytes = sum(attachment.size for attachment in resolved)
    if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
        raise ValueError(f"combined attachments exceed the {MAX_TOTAL_ATTACHMENT_BYTES} byte limit")
    return resolved
