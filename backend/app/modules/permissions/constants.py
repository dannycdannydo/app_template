"""Permission catalogue and default role grants (blueprint §9).

The permission codes come from blueprint §9's example permission set
(``properties.*``, ``documents.*``, ``users.invite``, ``users.manage_roles``,
``organisation.manage``) plus the ``records.*`` permissions the tenant-scoped
example module (v0.2 Scope §6.5) gates on. ``ROLE_PERMISSION_MAP`` is the seed data
the data migration applies; the exact bundle each role carries is this
template's decision, because the blueprint defines roles as permission bundles
but does not specify the bundle contents.

Default deny is the model: a permission not listed for any of the caller's
roles is denied, whatever the frontend shows (BP §9 rules).
"""

from __future__ import annotations

# (code, human-readable name) pairs. Codes are stable identifiers; names are
# display labels only.
PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("records.read", "View records"),
    ("records.create", "Create records"),
    ("records.update", "Update records"),
    ("records.delete", "Delete records"),
    ("properties.read", "View properties"),
    ("properties.create", "Create properties"),
    ("properties.update", "Update properties"),
    ("properties.delete", "Delete properties"),
    ("documents.read", "View documents"),
    ("documents.upload", "Upload documents"),
    ("documents.delete", "Delete documents"),
    ("users.invite", "Invite users"),
    ("users.manage_roles", "Manage member roles"),
    ("organisation.manage", "Manage the organisation"),
)

ALL_PERMISSION_CODES: tuple[str, ...] = tuple(code for code, _ in PERMISSIONS)

# The stable code of the platform-admin role seeded into ``platform_roles``.
# A platform membership holding this role is what makes a user a platform
# administrator; kept in sync with the seeded row in the platform-plane
# migration (Scope §6.2).
PLATFORM_ADMIN_ROLE_CODE = "platform_admin"

# Platform-plane permission codes. Kept in a separate catalogue on purpose:
# ``ALL_PERMISSION_CODES`` feeds ``ROLE_PERMISSION_MAP``, and the org-plane
# role bundles must never include platform permissions — the platform plane is
# a dedicated authorisation layer (Scope §6.2), never a global bypass of the
# organisation permission system, so an organisation owner holds no platform
# permission even though the owner bundle includes every org code.
PLATFORM_PERMISSIONS: tuple[tuple[str, str], ...] = (("platform.admin", "Platform administration"),)
PLATFORM_ALL_PERMISSION_CODES: tuple[str, ...] = tuple(code for code, _ in PLATFORM_PERMISSIONS)

# Role code -> permission codes granted by the seed. ``owner`` holds
# everything; ``administrator`` everything except ``organisation.manage`` so
# the owner keeps the organisation-level authority; the remaining roles are
# progressively narrower bundles. Acceptance §5.5 requires a ``viewer`` to be
# able to read records while every write returns 403, which this map provides.
ROLE_PERMISSION_MAP: dict[str, tuple[str, ...]] = {
    "owner": ALL_PERMISSION_CODES,
    "administrator": tuple(code for code in ALL_PERMISSION_CODES if code != "organisation.manage"),
    "manager": (
        "records.read",
        "records.create",
        "records.update",
        "properties.read",
        "properties.create",
        "properties.update",
        "documents.read",
        "documents.upload",
        "documents.delete",
        "users.invite",
    ),
    "member": (
        "records.read",
        "records.create",
        "properties.read",
        "properties.create",
        "documents.read",
        "documents.upload",
    ),
    "viewer": (
        "records.read",
        "properties.read",
        "documents.read",
    ),
}
