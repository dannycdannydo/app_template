"""Feature-flag management services (blueprint §27, Scope §6.7).

The platform endpoints stay thin and delegate here: listing merges the
catalogue with one organisation's override rows (a single query, never one
lookup per flag), and setting writes or updates the ``(organisation_id,
feature_key)`` override in the same transaction as the ``feature_flag.changed``
audit event. The enforcement side lives in ``core/feature_flags.py``
(``is_feature_enabled``), which services call directly — the management
surface here never decides whether a flag gates anything.

``set_feature_flag`` is an upsert: an existing override row is updated, a
missing one is inserted, and the unique pair is the race guard. A concurrent
writer hitting the constraint in the same instant loses the race with an
``IntegrityError``; the service then re-reads the now-committed row and
applies the update to it, mirroring the invitation linking service (Scope
§6.5) — last write wins on the enabled state, and the retried write carries
its own audit event. Setting ``enabled=false`` is how an override is switched
off; the row is deliberately kept so the admin centre can show the explicit
state and the audit trail can record the change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ServiceUnavailableError
from app.core.feature_flags import (
    FeatureFlagDefinition,
    feature_flag_definition,
    feature_flag_definitions,
)
from app.modules.audit.service import ACTION_FEATURE_FLAG_CHANGED, record_event
from app.modules.feature_flags.models import OrganisationFeature
from app.modules.feature_flags.queries import (
    organisation_feature_statement,
    organisation_features_statement,
)
from app.modules.organisations.models import Organisation
from app.modules.users.models import User


@dataclass(frozen=True)
class FeatureFlagState:
    """One catalogue entry merged with its optional per-organisation override.

    ``enabled`` is the effective state the admin centre should render and the
    enforcement helper would return for the organisation; ``overridden`` tells
    the UI whether an explicit override row exists.
    """

    definition: FeatureFlagDefinition
    override: OrganisationFeature | None

    @property
    def enabled(self) -> bool:
        if self.override is not None:
            return self.override.enabled
        return self.definition.default_enabled

    @property
    def overridden(self) -> bool:
        return self.override is not None

    @property
    def configuration_json(self) -> dict[str, Any] | None:
        if self.override is not None:
            return self.override.configuration_json
        return None


async def _get_organisation_or_404(session: AsyncSession, organisation_id: uuid.UUID) -> None:
    """Raise the standard 404 when the organisation does not exist."""
    organisation = await session.scalar(
        select(Organisation).where(Organisation.id == organisation_id)
    )
    if organisation is None:
        raise NotFoundError(
            code="organisation_not_found",
            message="The organisation could not be found.",
        )


def _state_for(
    definition: FeatureFlagDefinition, override: OrganisationFeature | None
) -> FeatureFlagState:
    """Assemble one effective state from a definition and its override row."""
    return FeatureFlagState(definition=definition, override=override)


async def list_feature_flags(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID | None,
) -> list[FeatureFlagState]:
    """Return the catalogue merged with one organisation's overrides.

    Without an ``organisation_id`` this is the plain catalogue (every flag at
    its default); with one, the overrides are resolved in a single query and
    merged per key, so the listing stays O(catalogue) regardless of how many
    flags exist. An unknown organisation is a 404, matching the memberships
    listing — the platform surface always operates on a concrete organisation.
    """
    overrides: dict[str, OrganisationFeature] = {}
    if organisation_id is not None:
        await _get_organisation_or_404(session, organisation_id)
        rows = (
            await session.scalars(
                organisation_features_statement(
                    organisation_id=organisation_id,
                )
            )
        ).all()
        overrides = {row.feature_key: row for row in rows if row.organisation_id == organisation_id}
    return [
        _state_for(definition, overrides.get(definition.key))
        for definition in feature_flag_definitions()
    ]


async def _apply_override(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    feature_key: str,
    enabled: bool,
    configuration_json: dict[str, Any] | None,
) -> OrganisationFeature:
    """Find the pair's override row or create it, then apply the new state.

    The row is looked up through the org/key statement and re-filtered in
    Python because the in-memory test session cannot apply the SQL WHERE
    clauses (they are proven by the query-construction and real-database
    tests). The caller owns the commit and the audit write.
    """
    rows = (
        await session.scalars(
            organisation_feature_statement(
                organisation_id=organisation_id,
                feature_key=feature_key,
            )
        )
    ).all()
    override = next(
        (
            candidate
            for candidate in rows
            if candidate.organisation_id == organisation_id and candidate.feature_key == feature_key
        ),
        None,
    )
    if override is None:
        override = OrganisationFeature(
            organisation_id=organisation_id,
            feature_key=feature_key,
            enabled=enabled,
            configuration_json=configuration_json or {},
        )
        session.add(override)
    else:
        override.enabled = enabled
        if configuration_json is not None:
            override.configuration_json = configuration_json
    return override


async def set_feature_flag(
    session: AsyncSession,
    *,
    actor: User,
    feature_key: str,
    organisation_id: uuid.UUID,
    enabled: bool,
    configuration_json: dict[str, Any] | None,
) -> FeatureFlagState:
    """Upsert an organisation's override for one flag and audit the change.

    An unknown feature key is a 404 (the catalogue is the closed set of known
    flags, and an unknown key can never gate anything); an unknown organisation
    is a 404 too. The existing row is updated in place when present, otherwise
    a new one is inserted, and the ``feature_flag.changed`` audit event commits
    inside the same transaction.

    A concurrent toggle of the same pair can lose the unique-constraint race
    between the SELECT and the INSERT; the commit then raises an
    ``IntegrityError``. Like the invitation linking service (Scope §6.5), the
    lost transaction is rolled back and retried once against the now-committed
    row — the update wins and its audit event is written. A second collision
    surfaces as a 503 rather than a silent no-op.
    """
    definition = feature_flag_definition(feature_key)
    if definition is None:
        raise NotFoundError(
            code="feature_flag_unknown",
            message="The feature flag could not be found.",
        )
    await _get_organisation_or_404(session, organisation_id)

    async def _write() -> OrganisationFeature:
        """Apply the override, audit it, and commit (one attempt)."""
        override = await _apply_override(
            session,
            organisation_id=organisation_id,
            feature_key=feature_key,
            enabled=enabled,
            configuration_json=configuration_json,
        )
        await record_event(
            session,
            organisation_id=organisation_id,
            actor_user_id=actor.id,
            action=ACTION_FEATURE_FLAG_CHANGED,
            resource_type="feature_flag",
            resource_id=feature_key,
            metadata={
                "feature_key": feature_key,
                "enabled": enabled,
                "configuration_json": configuration_json,
            },
        )
        await session.commit()
        return override

    try:
        override = await _write()
    except IntegrityError:
        await session.rollback()
        # A concurrent toggle committed the row first; our insert lost the
        # unique-pair race. Retry once: the second pass finds the committed
        # row and updates it instead of inserting a duplicate.
        try:
            override = await _write()
        except IntegrityError:
            await session.rollback()
            raise ServiceUnavailableError(
                code="feature_flag_update_failed",
                message="The feature flag could not be updated. Please try again.",
            ) from None
    return _state_for(definition, override)
