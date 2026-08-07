"""Feature-flag catalogue and backend enforcement helper (blueprint §27).

Feature state is enforced here, never in the frontend: services consult
:func:`is_feature_enabled` and change behaviour accordingly (Scope §6.7). The
catalogue is the single source of truth for which flags exist — the platform
management endpoints (``modules/feature_flags``) list it and write
``organisation_features`` overrides against it. Every v0.4 flag defaults to
**off** (default deny, mirroring the permission model): an organisation with
no override row for a key is treated as having the flag disabled, and a
platform administrator must explicitly enable it per organisation.

The helper is cache-friendly in two ways. First, the lookup is a single
indexed read on the ``(organisation_id, feature_key)`` unique pair, so it adds
one cheap query per request. Second, the result is memoised on the request's
session (``session.info``), so several services consulting the same flag
inside one request share a single database read with no staleness risk — the
memo lives and dies with the request, and a request never flips flags
mid-flight. The platform listing (``modules/feature_flags``) additionally
resolves a whole organisation's overrides in one query so the catalogue view
never degenerates into N+1 lookups.

The database row lookup deliberately lives in ``modules/feature_flags`` and
not here: ``app.core`` stays free of ORM model imports except this one helper,
which needs the row to decide the effective state. The table is registered for
Alembic autogenerate by ``app.db.base`` like every other model module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.feature_flags.queries import organisation_feature_statement

# The stable keys of the v0.4 catalogue. ``records.deletion`` is the wired
# example flag the records service enforces (off by default — an organisation
# keeps the destructive operation unavailable until the platform enables it).
FEATURE_RECORDS_DELETION = "records.deletion"


@dataclass(frozen=True)
class FeatureFlagDefinition:
    """One known feature flag: its stable key, display labels and default."""

    key: str
    name: str
    description: str
    default_enabled: bool = False


FEATURE_FLAG_CATALOGUE: tuple[FeatureFlagDefinition, ...] = (
    FeatureFlagDefinition(
        key=FEATURE_RECORDS_DELETION,
        name="Record deletion",
        description=(
            "Allow members to delete records. Off by default so organisations "
            "cannot lose data until a platform administrator enables it."
        ),
        default_enabled=False,
    ),
)

FEATURE_FLAG_LOOKUP: dict[str, FeatureFlagDefinition] = {
    definition.key: definition for definition in FEATURE_FLAG_CATALOGUE
}


def feature_flag_definition(feature_key: str) -> FeatureFlagDefinition | None:
    """Return the catalogue entry for a key, or ``None`` for an unknown key.

    Unknown keys are never grantable: :func:`is_feature_enabled` treats them
    as off, and the platform set endpoint rejects them with a 404 before any
    row is written.
    """
    return FEATURE_FLAG_LOOKUP.get(feature_key)


def feature_flag_definitions() -> tuple[FeatureFlagDefinition, ...]:
    """Return the whole catalogue in stable, display order."""
    return FEATURE_FLAG_CATALOGUE


async def is_feature_enabled(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    feature_key: str,
) -> bool:
    """Return whether a flag is effectively enabled for an organisation.

    An unknown key, or an organisation without an override row, resolves to
    the catalogue default (off for every v0.4 flag); an override row decides
    on its ``enabled`` value. The result is memoised per session so repeated
    checks inside one request cost one database read.
    """
    definition = feature_flag_definition(feature_key)
    if definition is None:
        return False

    # The memo assumes a request never flips a flag it has already read: the
    # management endpoint builds its response from the override it just wrote
    # (never through this helper), and every other request only reads. The
    # memo lives on the session, so it dies with the request and can never go
    # stale across requests.
    cache: dict[tuple[Any, str], bool] = session.info.setdefault("_feature_flag_cache", {})
    cache_key = (organisation_id, feature_key)
    if cache_key in cache:
        return cache[cache_key]

    rows = (
        await session.scalars(
            organisation_feature_statement(
                organisation_id=organisation_id,
                feature_key=feature_key,
            )
        )
    ).all()
    # The compiled statement carries the org/key WHERE clauses (proven by the
    # query-construction and real-database tests); the re-filter in Python is
    # what keeps the in-memory test session honest, matching the pattern of
    # the membership and invitation services.
    row = next(
        (
            candidate
            for candidate in rows
            if candidate.organisation_id == organisation_id and candidate.feature_key == feature_key
        ),
        None,
    )
    enabled = row.enabled if row is not None else definition.default_enabled
    cache[cache_key] = enabled
    return enabled
