"""Production enablement of the RLS platform-only plane (plan P4, group 7).

Plan P4 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). This is the platform-only group of ``docs/rls-rollout.md`` §3:

- ``platform_roles`` — the platform role catalogue;
- ``platform_role_permissions`` — platform role/permission grants;
- ``platform_memberships`` — which user holds which platform role; and
- ``bootstrap_states`` — the one-time platform bootstrap record.

These tables grant no tenant-row access by themselves: platform status is a
separate authorisation plane (BP §9, §30). The group keeps every existing
access path working without a bypass:

- **Pre-authorisation self read (ADR-0022 decision 8, applied to the platform
  plane).** ``require_platform_permission`` and ``/me`` must resolve the
  caller's *own* platform memberships before any platform context can be
  bound — the permission check is the authorisation itself. The authenticated
  user is already bound as transaction-local ``app.user_id``, so
  ``platform_memberships`` takes a **SELECT-only** user-keyed policy: a user can
  read their own platform memberships and no one else's, and can never insert,
  update or delete one. ``platform_roles``/``platform_role_permissions`` are the
  global catalogue those lookups join through and take a runtime read policy,
  exactly as the organisation ``roles``/``permissions`` catalogue is not
  tenant-scoped.
- **Validated platform context (ADR-0022 decision 4).** After
  ``require_platform_permission`` has authorised the caller it binds the
  transaction-local ``app.platform_admin`` flag. The cross-user platform
  administration — list, grant and revoke — is admitted only by
  ``platform_memberships_platform_access`` under that flag (or the service
  context below). A platform administrator cannot read ordinary tenant data
  through this policy: the platform tables carry no tenant rows.
- **Narrow service context for trusted control-plane paths.** Three trusted,
  non-interactive paths need cross-user platform-table access with no platform
  administrator present: the one-time bootstrap grant (verified email +
  unconsumed sentinel), the signature-verified ``user.deleted`` webhook
  deactivation and the operator recovery/teardown CLI. They bind a **separate**
  transaction-local ``app.platform_service`` flag, which is referenced only by
  this group's policies. It is not ``app.platform_admin``: the platform-admin
  flag also opens the group-6 cross-tenant audit read, so reusing it here would
  hand those paths audit access they do not need. ``app.platform_service``
  grants no tenant-row access and no cross-tenant audit read.
- **``bootstrap_states``.** The sentinel row records the consuming
  administrator's verified email, user id and timestamp, so it is **not**
  readable runtime-wide. The bootstrap hook resolves the verified profile
  first, then binds the trusted service context and reads the singleton to
  decide whether it is already consumed; the read, insert and delete are gated
  to the validated platform context or the trusted service context, and there
  is no UPDATE policy or runtime UPDATE grant because the row is never
  modified once consumed.

RLS is enabled **and forced** on all four tables, so a tenant-plane query with a
missed predicate cannot read the platform plane and the ordinary runtime role
can never write platform authority without one of the two explicit contexts.

The downgrade removes exactly this group's policies, helper and enforcement,
leaving earlier groups intact.

Revision ID: b4c5d6e7f8a9
Revises: a2b3c4d5e6f7
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Group 7 from ``docs/rls-rollout.md`` §3.
GROUP_TABLES = (
    "platform_roles",
    "platform_role_permissions",
    "platform_memberships",
    "bootstrap_states",
)

#: The tenant-scoped runtime role shared by every enabled table group.
RUNTIME_ROLE = "app_runtime"

#: Global platform-catalogue read policies.
PLATFORM_ROLES_READ_POLICY = "platform_roles_runtime_read"
PLATFORM_ROLE_PERMISSIONS_READ_POLICY = "platform_role_permissions_runtime_read"

#: ``platform_memberships`` policies.
PLATFORM_MEMBERSHIPS_SELF_POLICY = "platform_memberships_self_isolation"
PLATFORM_MEMBERSHIPS_PLATFORM_POLICY = "platform_memberships_platform_access"

#: ``bootstrap_states`` policies. The sentinel row carries the consuming
#: administrator's email, user id and timestamp, so it is not exposed
#: runtime-wide: read, insert and delete are admitted only under the validated
#: platform/trusted-service context. There is deliberately **no** UPDATE policy
#: and no runtime UPDATE grant — the row is never modified once consumed.
BOOTSTRAP_STATES_SERVICE_READ_POLICY = "bootstrap_states_service_read"
BOOTSTRAP_STATES_SERVICE_INSERT_POLICY = "bootstrap_states_service_insert"
BOOTSTRAP_STATES_SERVICE_DELETE_POLICY = "bootstrap_states_service_delete"

#: Provenance recorded on the production policies for review and rollback.
CATALOGUE_POLICY_COMMENT = (
    f"plan-p4-group7:{revision}:global platform catalogue read (ADR-0022 decision 4)"
)
SELF_POLICY_COMMENT = (
    f"plan-p4-group7:{revision}:pre-authorisation user-keyed platform membership read "
    "(ADR-0022 decision 8)"
)
PLATFORM_POLICY_COMMENT = (
    f"plan-p4-group7:{revision}:validated platform/service context cross-user access "
    "(ADR-0022 decision 4)"
)
BOOTSTRAP_SERVICE_POLICY_COMMENT = (
    f"plan-p4-group7:{revision}:validated platform/service context bootstrap singleton "
    "(ADR-0022 decision 4)"
)

# ``current_setting(name, true)`` returns NULL when the setting was never set.
# An absent, empty or malformed value resolves to false rather than raising, so
# every "no usable service context" case fails closed to "no platform rows".
_CURRENT_PLATFORM_SERVICE_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_platform_service() RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
DECLARE
    raw text;
BEGIN
    raw := current_setting('app.platform_service', true);
    IF raw IS NULL OR raw = '' THEN
        RETURN false;
    END IF;
    RETURN lower(raw) IN ('true', 't', '1', 'on', 'yes');
END;
$$;
"""

#: The predicate shared by the cross-user platform/service policies. Both flags
#: are transaction-local and bound only by trusted server-side paths after
#: their own validation: ``app.platform_admin`` by the platform permission
#: dependency, ``app.platform_service`` by the verified bootstrap, the
#: signature-verified webhook and the operator CLI.
_PLATFORM_OR_SERVICE = "app_current_platform_admin() OR app_current_platform_service()"


def upgrade() -> None:
    """Enable the platform-only group and install its policies."""
    op.execute(_CURRENT_PLATFORM_SERVICE_FUNCTION)
    op.execute(f"GRANT EXECUTE ON FUNCTION app_current_platform_service() TO {RUNTIME_ROLE}")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
    # The earlier rollout groups granted SELECT, INSERT, UPDATE, DELETE ON ALL
    # TABLES IN SCHEMA public to the runtime role, which includes these four
    # platform tables. Group 7 revokes that inherited table-wide write authority
    # and re-grants the exact least privilege the platform plane needs, so the
    # grant-level claim in the design is true (ADR-0022 decision 4). A per-table
    # REVOKE removes privileges inherited from the earlier schema-wide grant.
    op.execute(f"REVOKE ALL ON platform_roles FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON platform_role_permissions FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON platform_memberships FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON bootstrap_states FROM {RUNTIME_ROLE}")
    # The platform catalogue is read-only to the runtime role.
    op.execute(f"GRANT SELECT ON platform_roles TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT ON platform_role_permissions TO {RUNTIME_ROLE}")
    # Platform authority is written only through the validated platform context
    # or the narrow service context (RLS still gates every row).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON platform_memberships TO {RUNTIME_ROLE}")
    # The bootstrap singleton is read on every login; insert/delete are gated by
    # the policies below. UPDATE is never granted: the row is immutable.
    op.execute(f"GRANT SELECT, INSERT, DELETE ON bootstrap_states TO {RUNTIME_ROLE}")

    # ``platform_roles`` / ``platform_role_permissions``: the global platform
    # catalogue the pre-authorisation self lookup joins through. No tenant or
    # user data; no runtime write path exists (seed migration only).
    op.execute(
        f"CREATE POLICY {PLATFORM_ROLES_READ_POLICY} ON platform_roles "
        f"FOR SELECT TO {RUNTIME_ROLE} USING (true)"
    )
    op.execute(
        f"COMMENT ON POLICY {PLATFORM_ROLES_READ_POLICY} ON platform_roles IS "
        f"'{CATALOGUE_POLICY_COMMENT}'"
    )
    op.execute(
        f"CREATE POLICY {PLATFORM_ROLE_PERMISSIONS_READ_POLICY} ON platform_role_permissions "
        f"FOR SELECT TO {RUNTIME_ROLE} USING (true)"
    )
    op.execute(
        f"COMMENT ON POLICY {PLATFORM_ROLE_PERMISSIONS_READ_POLICY} "
        f"ON platform_role_permissions IS '{CATALOGUE_POLICY_COMMENT}'"
    )

    # ``platform_memberships``: a user reads only their own membership before
    # any platform context exists; the cross-user administration is admitted
    # only under the validated platform or service context. A user-keyed INSERT
    # is deliberately absent, so a user context can never grant itself platform
    # authority.
    op.execute(
        f"CREATE POLICY {PLATFORM_MEMBERSHIPS_SELF_POLICY} ON platform_memberships "
        f"FOR SELECT TO {RUNTIME_ROLE} USING (user_id = app_current_user_id())"
    )
    op.execute(
        f"COMMENT ON POLICY {PLATFORM_MEMBERSHIPS_SELF_POLICY} ON platform_memberships IS "
        f"'{SELF_POLICY_COMMENT}'"
    )
    op.execute(
        f"CREATE POLICY {PLATFORM_MEMBERSHIPS_PLATFORM_POLICY} ON platform_memberships "
        f"FOR ALL TO {RUNTIME_ROLE} "
        f"USING ({_PLATFORM_OR_SERVICE}) WITH CHECK ({_PLATFORM_OR_SERVICE})"
    )
    op.execute(
        f"COMMENT ON POLICY {PLATFORM_MEMBERSHIPS_PLATFORM_POLICY} ON platform_memberships IS "
        f"'{PLATFORM_POLICY_COMMENT}'"
    )

    # ``bootstrap_states``: the one-time sentinel. The row records the
    # consuming administrator's identity, so even the read is gated to the
    # validated platform/service context; the insert and delete are likewise
    # gated, so a tenant context can neither read nor claim nor clear the
    # bootstrap. There is no UPDATE policy and no runtime UPDATE grant: the
    # sentinel is immutable once consumed.
    op.execute(
        f"CREATE POLICY {BOOTSTRAP_STATES_SERVICE_READ_POLICY} ON bootstrap_states "
        f"FOR SELECT TO {RUNTIME_ROLE} USING ({_PLATFORM_OR_SERVICE})"
    )
    op.execute(
        f"COMMENT ON POLICY {BOOTSTRAP_STATES_SERVICE_READ_POLICY} ON bootstrap_states IS "
        f"'{BOOTSTRAP_SERVICE_POLICY_COMMENT}'"
    )
    op.execute(
        f"CREATE POLICY {BOOTSTRAP_STATES_SERVICE_INSERT_POLICY} ON bootstrap_states "
        f"FOR INSERT TO {RUNTIME_ROLE} WITH CHECK ({_PLATFORM_OR_SERVICE})"
    )
    op.execute(
        f"COMMENT ON POLICY {BOOTSTRAP_STATES_SERVICE_INSERT_POLICY} ON bootstrap_states IS "
        f"'{BOOTSTRAP_SERVICE_POLICY_COMMENT}'"
    )
    op.execute(
        f"CREATE POLICY {BOOTSTRAP_STATES_SERVICE_DELETE_POLICY} ON bootstrap_states "
        f"FOR DELETE TO {RUNTIME_ROLE} USING ({_PLATFORM_OR_SERVICE})"
    )
    op.execute(
        f"COMMENT ON POLICY {BOOTSTRAP_STATES_SERVICE_DELETE_POLICY} ON bootstrap_states IS "
        f"'{BOOTSTRAP_SERVICE_POLICY_COMMENT}'"
    )

    for table in GROUP_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Reverse exactly the platform-only group and its helper."""
    policies = {
        "platform_roles": (PLATFORM_ROLES_READ_POLICY,),
        "platform_role_permissions": (PLATFORM_ROLE_PERMISSIONS_READ_POLICY,),
        "platform_memberships": (
            PLATFORM_MEMBERSHIPS_SELF_POLICY,
            PLATFORM_MEMBERSHIPS_PLATFORM_POLICY,
        ),
        "bootstrap_states": (
            BOOTSTRAP_STATES_SERVICE_READ_POLICY,
            BOOTSTRAP_STATES_SERVICE_INSERT_POLICY,
            BOOTSTRAP_STATES_SERVICE_DELETE_POLICY,
        ),
    }
    for table, table_policies in policies.items():
        for policy in table_policies:
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        # Restore the exact group-6 privilege state. The earlier rollout groups
        # grant table-wide DML on every public table (``ON ALL TABLES``), so
        # group 7's narrowing revokes are reversed here rather than leaving the
        # platform tables in a privilege state no earlier revision produced.
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {RUNTIME_ROLE}")

    op.execute(f"REVOKE EXECUTE ON FUNCTION app_current_platform_service() FROM {RUNTIME_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS app_current_platform_service()")
