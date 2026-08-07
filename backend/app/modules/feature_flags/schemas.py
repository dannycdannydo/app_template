"""Feature-flag API schemas (blueprint §7, §12, Scope §6.7).

The PUT payload is the only user-supplied input: the organisation id, the
enabled state and the optional configuration JSON. Everything else is a
response. The organisation id comes from the request body here because the
platform plane has no ``X-Org-Id`` — the caller is a platform administrator
administering organisations they do not belong to — but the pair is
``extra="forbid"`` so no server-controlled field (feature key from the body,
timestamps, row id) can be smuggled in; the feature key comes from the path.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlatformFeatureFlagUpdate(BaseModel):
    """Request payload for setting one organisation's override for a flag.

    ``enabled`` is the effective switch; ``configuration_json`` is optional
    per-flag configuration that is opaque to the enforcement helper. Writing
    the row with ``enabled=false`` is how an override is turned off — the
    helper still consults the row and the audit trail keeps the change.
    """

    model_config = ConfigDict(extra="forbid")

    organisation_id: uuid.UUID
    enabled: bool
    configuration_json: dict[str, Any] | None = Field(default=None)


class PlatformFeatureFlagItem(BaseModel):
    """One catalogue entry with its optional per-organisation override state.

    ``enabled`` is always the *effective* state (override value, or the
    catalogue default when the organisation has no override row);
    ``overridden`` tells the admin centre whether an explicit override exists
    (so the UI can show "using default" vs "explicitly set").
    """

    feature_key: str
    name: str
    description: str
    default_enabled: bool
    enabled: bool
    overridden: bool
    configuration_json: dict[str, Any] | None = None


class PlatformFeatureFlagListResponse(BaseModel):
    """The envelope for the catalogue listing (optionally org-filtered)."""

    items: list[PlatformFeatureFlagItem]
