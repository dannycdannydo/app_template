"""Tests for the identity and tenancy data model (Scope §6.1, BP §7, §9, §10).

Metadata checks are pure Python and run everywhere; the migration smoke test in
``test_db.py`` exercises the new migration against PostgreSQL when reachable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Enum, Table, UniqueConstraint

from app.db.base import Base
from app.modules.organisations.models import (
    MembershipStatus,
    Organisation,
    OrganisationMembership,
)
from app.modules.organisations.schemas import (
    MembershipListItem,
    MembershipResponse,
    OrganisationCreate,
    OrganisationResponse,
)
from app.modules.users.models import User
from app.modules.users.schemas import UserListItem


def _table_of(model: type[Base]) -> Table:
    """Return the mapped :class:`Table` with precise typing for introspection."""
    return cast(Table, model.__table__)


def test_new_models_are_registered_on_base_metadata() -> None:
    for table_name in ("users", "organisations", "organisation_memberships"):
        assert table_name in Base.metadata.tables


def test_users_table_has_no_password_column() -> None:
    """The application stores the WorkOS identifier, never passwords."""
    assert "password" not in _table_of(User).c
    assert "password_hash" not in _table_of(User).c


def test_user_identity_columns() -> None:
    table = _table_of(User)
    workos = table.c.workos_user_id
    assert not workos.nullable
    assert not table.c.email.nullable
    assert table.c.email.index is True
    assert table.c.is_active.server_default is not None


def test_workos_user_id_is_unique() -> None:
    unique = next(c for c in _table_of(User).constraints if isinstance(c, UniqueConstraint))
    assert unique.name == "uq_users_workos_user_id"
    assert list(unique.columns) == [_table_of(User).c.workos_user_id]


def test_organisation_table_columns() -> None:
    table = _table_of(Organisation)
    assert not table.c.name.nullable
    assert table.primary_key.name == "pk_organisations"


def test_membership_unique_user_and_organisation() -> None:
    """A user can hold at most one membership per organisation."""
    unique = next(
        c for c in _table_of(OrganisationMembership).constraints if isinstance(c, UniqueConstraint)
    )
    assert unique.name == "uq_organisation_memberships_user_id_organisation_id"
    columns = {column.name for column in unique.columns}
    assert columns == {"user_id", "organisation_id"}


def test_membership_foreign_keys_follow_convention() -> None:
    table = _table_of(OrganisationMembership)
    fk_names = {constraint.name for constraint in table.foreign_key_constraints}
    assert fk_names == {
        "fk_organisation_memberships_user_id_users",
        "fk_organisation_memberships_organisation_id_organisations",
    }


def test_membership_status_has_database_check_constraint() -> None:
    """Allowed status values are enforced in PostgreSQL (BP §10)."""
    table = _table_of(OrganisationMembership)
    check = next(
        c
        for c in table.constraints
        if isinstance(c, CheckConstraint)
        and c.name == "ck_organisation_memberships_membership_status"
    )
    assert "active" in check.sqltext.text
    assert "invited" in check.sqltext.text
    assert "suspended" in check.sqltext.text
    assert "left" in check.sqltext.text


def test_membership_status_enum_values() -> None:
    assert MembershipStatus.ACTIVE.value == "active"
    assert MembershipStatus.INVITED.value == "invited"
    assert MembershipStatus.SUSPENDED.value == "suspended"
    assert MembershipStatus.LEFT.value == "left"


def test_membership_status_persists_values_not_names() -> None:
    """The ORM must store "active", not "ACTIVE", to satisfy the check constraint."""
    status_type = cast(Enum, _table_of(OrganisationMembership).c.status.type)
    assert list(status_type.enums) == ["active", "invited", "suspended", "left"]


def test_organisation_create_validates_name() -> None:
    assert OrganisationCreate(name="Acme").name == "Acme"
    with pytest.raises(ValidationError):
        OrganisationCreate(name="")
    with pytest.raises(ValidationError):
        OrganisationCreate(name="x" * 256)


def test_organisation_response_serialises_from_orm() -> None:
    org = Organisation(id=uuid.uuid4(), name="Acme")
    org.created_at = datetime.now(UTC)
    org.updated_at = datetime.now(UTC)
    response = OrganisationResponse.model_validate(org)
    assert response.name == "Acme"


def test_user_list_item_serialises_from_orm() -> None:
    user = User(workos_user_id="user_123", email="a@b.dev", name="Ada")
    user.id = uuid.uuid4()
    user.is_active = True
    user.created_at = datetime.now(UTC)
    response = UserListItem.model_validate(user)
    assert response.email == "a@b.dev"
    assert response.is_active is True


def test_membership_schemas_serialise_from_orm() -> None:
    membership = OrganisationMembership(
        user_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        status=MembershipStatus.ACTIVE,
    )
    membership.id = uuid.uuid4()
    membership.created_at = datetime.now(UTC)
    membership.updated_at = datetime.now(UTC)
    assert MembershipListItem.model_validate(membership).status == MembershipStatus.ACTIVE
    assert MembershipResponse.model_validate(membership).user_id is not None
