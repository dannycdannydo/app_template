"""Reusable feature-flag queries (blueprint §27, Scope §6.7).

The statements carry the org/key WHERE clauses the enforcement helper and the
platform management endpoints rely on; the tests share this one place where
the filter columns are named. ``organisation_features_statement`` resolves
every override of one organisation in a single query so the platform catalogue
listing never degenerates into N+1 lookups (the cache-friendly property of the
module, alongside the per-session memo in ``core/feature_flags.py``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select

from app.modules.feature_flags.models import OrganisationFeature


def organisation_feature_statement(
    *,
    organisation_id: uuid.UUID,
    feature_key: str,
) -> Select[tuple[OrganisationFeature]]:
    """Return a statement selecting one organisation's override for one key.

    The unique ``(organisation_id, feature_key)`` pair means this is at most
    one row; the statement is the single-row lookup used by the enforcement
    helper.
    """
    return select(OrganisationFeature).where(
        OrganisationFeature.organisation_id == organisation_id,
        OrganisationFeature.feature_key == feature_key,
    )


def organisation_features_statement(
    *,
    organisation_id: uuid.UUID,
) -> Select[tuple[OrganisationFeature]]:
    """Return a statement selecting every override row of one organisation.

    Used by the platform listing to merge an organisation's overrides with the
    catalogue in one query instead of one lookup per known flag.
    """
    return select(OrganisationFeature).where(OrganisationFeature.organisation_id == organisation_id)
