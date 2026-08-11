"""Platform AI-settings API schemas (v0.7 Scope §6.5, BP §7, §12).

The PUT payload is the only user-supplied input and is ``extra="forbid"`` so no
server-controlled field (timestamps, the row id, reserved budget, updater id)
can be smuggled in. The organisation id comes from the path, never from the
body — the platform plane has no ``X-Org-Id`` and the caller administers
organisations they do not belong to (BP §9 platform plane). Provider/model ids
are validated against the registries by the service before any row is written,
so the response never needs to carry raw registry data.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PlatformOrganisationAISettingsUpdate(BaseModel):
    """Request payload for replacing one organisation's AI policy.

    ``allowed_provider_ids`` / ``allowed_model_ids`` are the registry-validated
    allowlists; an empty list means "no restriction from this knob".
    ``monthly_budget`` ``None`` disables the budget; ``retention_policy_days``
    ``None`` disables scheduled retention deletion.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    allowed_provider_ids: list[str] = Field(default_factory=list, max_length=64)
    allowed_model_ids: list[str] = Field(default_factory=list, max_length=256)
    provider_override: str | None = Field(default=None, max_length=128)
    model_override: str | None = Field(default=None, max_length=128)
    monthly_budget: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=6)
    retention_policy_days: int | None = Field(default=None, ge=1, le=3650)


class PlatformOrganisationAISettingsResponse(BaseModel):
    """One organisation's AI policy as the admin centre sees it.

    ``reserved_budget`` is deliberately absent: the in-flight reservation is
    internal accounting (v0.7 Scope §6.5), not a management control.
    """

    organisation_id: uuid.UUID
    enabled: bool
    allowed_provider_ids: list[str]
    allowed_model_ids: list[str]
    provider_override: str | None
    model_override: str | None
    monthly_budget: Decimal | None
    retention_policy_days: int | None
    updated_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
