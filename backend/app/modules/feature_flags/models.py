"""Organisation feature-flag ORM model (blueprint §27, Scope §6.7).

One row is one platform-controlled override for one organisation: the
``(organisation_id, feature_key)`` unique pair is the invariant that an
organisation has at most one override per known flag. There is no row when an
organisation has no override — the enforcement helper then falls back to the
flag's catalogue default, which is off for every v0.4 flag (default deny).
``configuration_json`` carries per-org flag configuration; it is opaque to the
enforcement helper, which only consults ``enabled``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.conventions import TimestampMixin, UuidV7, uuid7


class OrganisationFeature(Base, TimestampMixin):
    """One platform-controlled feature-flag override for one organisation."""

    __tablename__ = "organisation_features"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "feature_key",
            name="uq_organisation_features_organisation_id_feature_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UuidV7, primary_key=True, default=uuid7)
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    feature_key: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    configuration_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
