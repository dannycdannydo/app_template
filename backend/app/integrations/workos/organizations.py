"""WorkOS organisation mapping adapter (Scope §6.3, blueprint §5, §30, ADR-0001).

Creates and reads WorkOS organisations through the WorkOS Management API and
keeps the ``WORKOS_API_KEY`` inside this layer, exactly like
``WorkOSUserProfileClient`` in ``core/security.py``: no router, service or
response schema ever sees the key. The SDK's ``Organization`` model never
leaves this module either — the adapter returns its own small
:class:`WorkOSOrganisation` value so the rest of the application depends on a
stable, testable contract instead of a generated SDK type.

Mapping rules (design plan §2.1, §3.1, §9):

- Every internal organisation that needs WorkOS delivery carries a
  ``workos_organisation_id`` mapping, created eagerly at platform org creation
  and lazily as a backfill for pre-existing organisations at first invite.
- The internal organisation id is sent as the WorkOS ``external_id`` so an
  orphaned WorkOS organisation (created successfully while the database
  transaction that should have recorded the mapping failed) can be found again
  by ``get_workos_organisation_by_external_id`` and reconciled: either the
  mapping is persisted for the matching internal organisation or the orphan is
  deleted from WorkOS. The reconciliation itself is a documented operational
  step, not application code, because it needs a management context the
  request path deliberately lacks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from workos import WorkOSClient, WorkOSError

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError


@dataclass(frozen=True)
class WorkOSOrganisation:
    """A WorkOS organisation as seen by the application (SDK model stays out)."""

    id: str
    name: str
    external_id: str | None


class WorkOSOrganizationsProvider(Protocol):
    """The mapping adapter contract; services depend on this, not the SDK."""

    async def create_workos_organisation(
        self, *, name: str, external_id: str
    ) -> WorkOSOrganisation:
        """Create a WorkOS organisation and return its id, name and external id."""
        ...

    async def get_workos_organisation(self, workos_organisation_id: str) -> WorkOSOrganisation:
        """Return one WorkOS organisation by its WorkOS id."""
        ...

    async def get_workos_organisation_by_external_id(
        self, external_id: str
    ) -> WorkOSOrganisation | None:
        """Return one WorkOS organisation by its external id, or None."""
        ...


class WorkOSOrganizationsClient:
    """The WorkOS-backed adapter; the Management API key never leaves it."""

    def __init__(
        self,
        *,
        api_key: str,
        client_id: str,
        api_base_url: str,
    ) -> None:
        self._client = WorkOSClient(
            api_key=api_key,
            client_id=client_id,
            base_url=api_base_url,
        )

    async def create_workos_organisation(
        self, *, name: str, external_id: str
    ) -> WorkOSOrganisation:
        """Create the WorkOS organisation and return the mapped value.

        The internal organisation id is passed as the WorkOS ``external_id``
        so a mapping row lost to a failed transaction can be reconciled later
        (module docstring). A failure here is an external-service failure: the
        caller's transaction must roll back so no internal organisation is
        created without its WorkOS mapping.
        """
        try:
            organisation = await asyncio.to_thread(
                self._client.organizations.create_organization,
                name=name,
                external_id=external_id,
            )
        except WorkOSError as exc:
            raise ExternalServiceError(
                code="workos_organisation_create_failed",
                message="The WorkOS organisation could not be created. Please try again.",
            ) from exc
        return WorkOSOrganisation(
            id=organisation.id,
            name=organisation.name,
            external_id=organisation.external_id,
        )

    async def get_workos_organisation(self, workos_organisation_id: str) -> WorkOSOrganisation:
        """Return one WorkOS organisation by its WorkOS id."""
        try:
            organisation = await asyncio.to_thread(
                self._client.organizations.get_organization, workos_organisation_id
            )
        except WorkOSError as exc:
            raise ExternalServiceError(
                code="workos_organisation_unavailable",
                message="The WorkOS organisation could not be read. Please try again.",
            ) from exc
        return WorkOSOrganisation(
            id=organisation.id,
            name=organisation.name,
            external_id=organisation.external_id,
        )

    async def get_workos_organisation_by_external_id(
        self, external_id: str
    ) -> WorkOSOrganisation | None:
        """Return one WorkOS organisation by its external id, or None.

        Used by the documented orphan-reconciliation step to find the WorkOS
        organisation created for an internal organisation id.
        """
        try:
            organisation = await asyncio.to_thread(
                self._client.organizations.get_organization_by_external_id, external_id
            )
        except WorkOSError:
            return None
        return WorkOSOrganisation(
            id=organisation.id,
            name=organisation.name,
            external_id=organisation.external_id,
        )


@lru_cache
def get_workos_organizations_client() -> WorkOSOrganizationsProvider:
    """Dependency factory for the process-wide WorkOS organisations client."""
    settings = get_settings()
    return WorkOSOrganizationsClient(
        api_key=settings.workos_api_key,
        client_id=settings.workos_client_id,
        api_base_url=settings.workos_api_base_url,
    )
