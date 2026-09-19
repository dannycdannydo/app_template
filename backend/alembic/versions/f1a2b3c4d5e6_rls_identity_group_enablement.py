"""Production enablement of the RLS identity/control-plane group (plan P4, group 5).

Plan P4 / ADR-0022 (PostgreSQL Row-Level Security as a tenant-isolation
backstop). This is the first control-plane group of ``docs/rls-rollout.md`` §3:
the identity tables ``organisation_memberships`` (organisation-owned),
``membership_roles`` (indirectly owned) and ``invitations``
(organisation-owned).

Unlike the direct tenant-data groups, this group must keep the **pre-tenant**
authentication flow working: a membership and an invitation are read before any
organisation context exists, so the policies add user-keyed and single-row
bootstrap paths alongside the canonical organisation isolation:

- ``organisation_memberships`` gets the canonical
  ``organisation_memberships_organisation_isolation`` policy plus a **SELECT
  only** ``organisation_memberships_user_isolation`` policy keyed to
  ``app.user_id``. The user policy deliberately grants no write: a user may
  read their own memberships (``/me``, context resolution, teardown) but can
  never insert, update or delete one — every membership write goes through an
  organisation or the validated platform path (ADR-0022 decisions 4 and 8).
- ``membership_roles`` is indirectly owned and has no tenant or user key of its
  own, so it separates **read visibility** from **write authority**:

  - the ``membership_roles_parent_isolation`` policy is ``FOR SELECT`` only and
    admits a role grant exactly when its parent membership is visible under the
    caller's current context. The parent ``organisation_memberships`` policies
    are themselves RLS-filtered, so an organisation context admits the
    organisation's role grants and a pre-tenant user context admits only the
    user's own (ADR-0022 decision 6's reviewed parent strategy, as used for
    ``notification_deliveries`` in group 2);
  - the ``membership_roles_organisation_isolation`` policy is ``FOR ALL`` and
    requires the parent membership's own ``organisation_id`` to equal the
    validated ``app_current_tenant_id()``. A pre-tenant user-only context
    (``app.user_id`` with no organisation) therefore grants role-grant read but
    **no** insert/update/delete authority: the write predicate compares against
    a ``NULL`` tenant and fails closed.
- ``invitations`` gets the canonical policy, an invitee read/update pair keyed
  to the authenticated user's verified email (``app_current_user_email()``) so
  login-time linking can find and accept the invitee's own rows with no
  organisation context, and a single-row webhook bootstrap keyed to the
  verified ``invitation.revoked`` event's provider id so the signature-gated
  consumer can mirror a provider revocation without a bypass. The bootstrap is
  split into a ``FOR SELECT`` policy and a ``FOR UPDATE`` policy
  (``invitations_webhook_provider_select`` /
  ``invitations_webhook_provider_update``) because the verified operation only
  reads/locks one row and flips its ``status``: binding a provider id must not
  create insert or delete authority (ADR-0022 decisions 3 and 4). Runtime
  UPDATE on ``invitations`` is restricted to ``status``/``updated_at`` at the
  grant level, so no invitee policy can move an invitation's organisation,
  email or role.

Absent, empty or malformed context resolves to ``NULL`` in
``app_current_tenant_id()`` / ``app_current_user_id()`` /
``app_current_invitation_provider_id()`` and therefore matches no row and
authorises no write; it never means unrestricted access.

The shared restricted ``app_runtime`` role, ``app_current_tenant_id()`` and
``app_current_user_id()`` helpers are owned by earlier revisions; this migration
owns the new ``app_current_user_email()`` and
``app_current_invitation_provider_id()`` helpers, which no earlier revision
uses, and drops them on downgrade. The application layer remains the first
enforcement layer: every service keeps its membership/invitation predicates and
a foreign row stays a ``404``, and every platform operation binds exactly the
one organisation it targets after the platform permission dependency validated
the caller (the explicit per-organisation platform path, never a bypass —
ADR-0022 decision 4).

The downgrade removes exactly this group's policies and helpers, restores the
unrestricted ``UPDATE`` grant on ``invitations`` and ``NO FORCE``/``DISABLE``s
RLS, leaving earlier groups intact.

Revision ID: f1a2b3c4d5e6
Revises: d0e1f2a3b4c5
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Group 5 from ``docs/rls-rollout.md`` §3.
GROUP_TABLES = ("organisation_memberships", "membership_roles", "invitations")

#: The runtime role shared by every enabled table group (ADR-0022 decision 2).
RUNTIME_ROLE = "app_runtime"

#: Canonical organisation-isolation policy naming convention.
ORGANISATION_POLICY_SUFFIX = "_organisation_isolation"

#: Pre-tenant identity policies.
MEMBERSHIPS_USER_POLICY = "organisation_memberships_user_isolation"
MEMBERSHIP_ROLES_PARENT_POLICY = "membership_roles_parent_isolation"
MEMBERSHIP_ROLES_WRITE_POLICY = "membership_roles_organisation_isolation"
INVITATIONS_INVITEE_SELECT_POLICY = "invitations_invitee_select"
INVITATIONS_INVITEE_UPDATE_POLICY = "invitations_invitee_update"
INVITATIONS_WEBHOOK_SELECT_POLICY = "invitations_webhook_provider_select"
INVITATIONS_WEBHOOK_UPDATE_POLICY = "invitations_webhook_provider_update"

#: The only invitation columns the ordinary runtime path may write. Every
#: invitation update in the application is a status transition; restricting the
#: grant keeps a matching invitee email from becoming authority to rewrite the
#: organisation, email or role of the invitee's own row (ADR-0022 decision 4).
INVITATIONS_UPDATE_COLUMNS = ("status", "updated_at")

#: Functional partial index supporting the login-time invitee lookup
#: (``lower(email) = ? AND status = 'sent'``). The existing ``email`` index
#: cannot serve a ``lower()`` predicate and the pending-uniqueness index leads
#: with ``organisation_id``, so without this index the invitee read (and the
#: matching RLS invitee policy) would sequentially scan ``invitations``
#: (rollout principle 4: no sequential-scan regression).
INVITATIONS_LOWER_EMAIL_INDEX = "ix_invitations_lower_email"

#: Provenance recorded on the production policies for review and rollback.
ORGANISATION_POLICY_COMMENT = f"plan-p4-group5:{revision}:organisation isolation (ADR-0022)"
USER_POLICY_COMMENT = (
    f"plan-p4-group5:{revision}:pre-tenant user-keyed membership read (ADR-0022 decision 8)"
)
PARENT_POLICY_COMMENT = (
    f"plan-p4-group5:{revision}:parent-existence read visibility (ADR-0022 decision 6)"
)
PARENT_WRITE_POLICY_COMMENT = (
    f"plan-p4-group5:{revision}:validated organisation context required for writes "
    "(ADR-0022 decisions 4 and 6)"
)
INVITEE_POLICY_COMMENT = (
    f"plan-p4-group5:{revision}:invitee email-keyed pre-tenant access (ADR-0022 decision 8)"
)
WEBHOOK_POLICY_COMMENT = (
    f"plan-p4-group5:{revision}:verified webhook single-row read/status bootstrap "
    "(ADR-0022 decisions 3, 4 and 8)"
)

# ``current_setting(name, true)`` returns NULL when the setting was never set;
# an absent, empty or malformed value resolves to NULL rather than raising, so
# every "no usable context" case fails closed to "no rows".
_CURRENT_USER_EMAIL_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_user_email() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT lower(u.email) FROM public.users u WHERE u.id = app_current_user_id()
$$;
"""

_CURRENT_INVITATION_PROVIDER_FUNCTION = """
CREATE OR REPLACE FUNCTION app_current_invitation_provider_id() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    raw text;
BEGIN
    raw := current_setting('app.invitation_provider_id', true);
    IF raw IS NULL OR raw = '' THEN
        RETURN NULL;
    END IF;
    RETURN raw;
END;
$$;
"""


def _organisation_policy_sql(table: str) -> str:
    return f"""
        CREATE POLICY {table}{ORGANISATION_POLICY_SUFFIX} ON {table}
            FOR ALL
            TO {RUNTIME_ROLE}
            USING (organisation_id = app_current_tenant_id())
            WITH CHECK (organisation_id = app_current_tenant_id())
    """


# Read visibility only: a role grant is readable exactly when its parent
# membership is visible under the current (RLS-filtered) context.
_PARENT_EXISTENCE_PREDICATE = (
    "(EXISTS (SELECT 1 FROM organisation_memberships m "
    "WHERE m.id = membership_roles.membership_id))"
)

# Write authority: the parent membership's own organisation must equal the
# validated transaction-local tenant. A pre-tenant user-only context binds no
# ``app.organisation_id``, so this comparison fails closed and authorises no
# insert/update/delete (ADR-0022 decisions 4 and 6).
_PARENT_ORGANISATION_PREDICATE = (
    "(EXISTS (SELECT 1 FROM organisation_memberships m "
    "WHERE m.id = membership_roles.membership_id "
    "AND m.organisation_id = app_current_tenant_id()))"
)


def upgrade() -> None:
    """Enable the identity group: canonical, pre-tenant and webhook policies."""
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON organisation_memberships TO {RUNTIME_ROLE}")
    # Membership status is written through the canonical organisation policy
    # (the platform plane binds the target organisation). A user context alone
    # grants no write: ``organisation_memberships_user_isolation`` is SELECT
    # only, so the table-level UPDATE grant is reachable only under an
    # organisation context.
    op.execute(f"GRANT UPDATE ON organisation_memberships TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON membership_roles TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON invitations TO {RUNTIME_ROLE}")
    # Runtime invitation UPDATE is column-restricted: every application update
    # is a status transition, and the invitee's own-row policy must never grant
    # authority to rewrite the organisation, email or role (ADR-0022 decision 4).
    op.execute(f"REVOKE UPDATE ON invitations FROM {RUNTIME_ROLE}")
    op.execute(
        "GRANT UPDATE ("
        + ", ".join(INVITATIONS_UPDATE_COLUMNS)
        + f") ON invitations TO {RUNTIME_ROLE}"
    )

    op.execute(_CURRENT_USER_EMAIL_FUNCTION)
    op.execute(_CURRENT_INVITATION_PROVIDER_FUNCTION)
    op.execute(f"GRANT EXECUTE ON FUNCTION app_current_user_email() TO {RUNTIME_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION app_current_invitation_provider_id() TO {RUNTIME_ROLE}")

    # Support the pre-tenant invitee lookup without a sequential scan.
    op.execute(
        f"CREATE INDEX {INVITATIONS_LOWER_EMAIL_INDEX} ON invitations (lower(email)) "
        "WHERE status = 'sent'"
    )

    # organisation_memberships: canonical organisation isolation plus the
    # pre-tenant, SELECT-only user read.
    op.execute(
        f"DROP POLICY IF EXISTS organisation_memberships{ORGANISATION_POLICY_SUFFIX} "
        "ON organisation_memberships"
    )
    op.execute(_organisation_policy_sql("organisation_memberships"))
    op.execute(
        f"COMMENT ON POLICY organisation_memberships{ORGANISATION_POLICY_SUFFIX} "
        f"ON organisation_memberships IS '{ORGANISATION_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {MEMBERSHIPS_USER_POLICY} ON organisation_memberships")
    op.execute(
        f"""
        CREATE POLICY {MEMBERSHIPS_USER_POLICY} ON organisation_memberships
            FOR SELECT
            TO {RUNTIME_ROLE}
            USING (user_id = app_current_user_id())
        """
    )
    op.execute(
        f"COMMENT ON POLICY {MEMBERSHIPS_USER_POLICY} ON organisation_memberships IS "
        f"'{USER_POLICY_COMMENT}'"
    )

    # membership_roles read: parent-existence, evaluated against the
    # RLS-filtered parent membership so the current context decides visibility.
    op.execute(f"DROP POLICY IF EXISTS {MEMBERSHIP_ROLES_PARENT_POLICY} ON membership_roles")
    op.execute(
        f"""
        CREATE POLICY {MEMBERSHIP_ROLES_PARENT_POLICY} ON membership_roles
            FOR SELECT
            TO {RUNTIME_ROLE}
            USING {_PARENT_EXISTENCE_PREDICATE}
        """
    )
    op.execute(
        f"COMMENT ON POLICY {MEMBERSHIP_ROLES_PARENT_POLICY} ON membership_roles IS "
        f"'{PARENT_POLICY_COMMENT}'"
    )
    # membership_roles write: split from the read policy above. A pre-tenant
    # user context can read its own grants but must never reach a mutation, so
    # writes require the parent membership's durable organisation to equal the
    # validated tenant (plan P4 group 5 review).
    op.execute(f"DROP POLICY IF EXISTS {MEMBERSHIP_ROLES_WRITE_POLICY} ON membership_roles")
    op.execute(
        f"""
        CREATE POLICY {MEMBERSHIP_ROLES_WRITE_POLICY} ON membership_roles
            FOR ALL
            TO {RUNTIME_ROLE}
            USING {_PARENT_ORGANISATION_PREDICATE}
            WITH CHECK {_PARENT_ORGANISATION_PREDICATE}
        """
    )
    op.execute(
        f"COMMENT ON POLICY {MEMBERSHIP_ROLES_WRITE_POLICY} ON membership_roles IS "
        f"'{PARENT_WRITE_POLICY_COMMENT}'"
    )

    # invitations: canonical organisation isolation, the invitee read/update
    # pair for pre-tenant login linking, and the webhook single-row bootstrap.
    op.execute(f"DROP POLICY IF EXISTS invitations{ORGANISATION_POLICY_SUFFIX} ON invitations")
    op.execute(_organisation_policy_sql("invitations"))
    op.execute(
        f"COMMENT ON POLICY invitations{ORGANISATION_POLICY_SUFFIX} ON invitations IS "
        f"'{ORGANISATION_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {INVITATIONS_INVITEE_SELECT_POLICY} ON invitations")
    op.execute(
        f"""
        CREATE POLICY {INVITATIONS_INVITEE_SELECT_POLICY} ON invitations
            FOR SELECT
            TO {RUNTIME_ROLE}
            USING (lower(email) = app_current_user_email())
        """
    )
    op.execute(
        f"COMMENT ON POLICY {INVITATIONS_INVITEE_SELECT_POLICY} ON invitations IS "
        f"'{INVITEE_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {INVITATIONS_INVITEE_UPDATE_POLICY} ON invitations")
    op.execute(
        f"""
        CREATE POLICY {INVITATIONS_INVITEE_UPDATE_POLICY} ON invitations
            FOR UPDATE
            TO {RUNTIME_ROLE}
            USING (lower(email) = app_current_user_email())
            WITH CHECK (lower(email) = app_current_user_email())
        """
    )
    op.execute(
        f"COMMENT ON POLICY {INVITATIONS_INVITEE_UPDATE_POLICY} ON invitations IS "
        f"'{INVITEE_POLICY_COMMENT}'"
    )
    # The verified revocation operation reads/locks one row and flips its
    # status, so the bootstrap is split into SELECT and UPDATE only: a provider
    # id must never authorise an insert or a delete (ADR-0022 decisions 3, 4
    # and 8).
    op.execute(f"DROP POLICY IF EXISTS {INVITATIONS_WEBHOOK_SELECT_POLICY} ON invitations")
    op.execute(
        f"""
        CREATE POLICY {INVITATIONS_WEBHOOK_SELECT_POLICY} ON invitations
            FOR SELECT
            TO {RUNTIME_ROLE}
            USING (workos_invitation_id = app_current_invitation_provider_id())
        """
    )
    op.execute(
        f"COMMENT ON POLICY {INVITATIONS_WEBHOOK_SELECT_POLICY} ON invitations IS "
        f"'{WEBHOOK_POLICY_COMMENT}'"
    )
    op.execute(f"DROP POLICY IF EXISTS {INVITATIONS_WEBHOOK_UPDATE_POLICY} ON invitations")
    op.execute(
        f"""
        CREATE POLICY {INVITATIONS_WEBHOOK_UPDATE_POLICY} ON invitations
            FOR UPDATE
            TO {RUNTIME_ROLE}
            USING (workos_invitation_id = app_current_invitation_provider_id())
            WITH CHECK (workos_invitation_id = app_current_invitation_provider_id())
        """
    )
    op.execute(
        f"COMMENT ON POLICY {INVITATIONS_WEBHOOK_UPDATE_POLICY} ON invitations IS "
        f"'{WEBHOOK_POLICY_COMMENT}'"
    )

    for table in GROUP_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Reverse exactly the identity group, its helpers and the column grant."""
    invitation_policies = (
        f"invitations{ORGANISATION_POLICY_SUFFIX}",
        INVITATIONS_INVITEE_SELECT_POLICY,
        INVITATIONS_INVITEE_UPDATE_POLICY,
        INVITATIONS_WEBHOOK_SELECT_POLICY,
        INVITATIONS_WEBHOOK_UPDATE_POLICY,
    )
    for policy in invitation_policies:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON invitations")
    op.execute(f"DROP INDEX IF EXISTS {INVITATIONS_LOWER_EMAIL_INDEX}")
    op.execute("ALTER TABLE invitations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invitations DISABLE ROW LEVEL SECURITY")

    op.execute(f"DROP POLICY IF EXISTS {MEMBERSHIP_ROLES_PARENT_POLICY} ON membership_roles")
    op.execute(f"DROP POLICY IF EXISTS {MEMBERSHIP_ROLES_WRITE_POLICY} ON membership_roles")
    op.execute("ALTER TABLE membership_roles NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE membership_roles DISABLE ROW LEVEL SECURITY")

    op.execute(
        f"DROP POLICY IF EXISTS organisation_memberships{ORGANISATION_POLICY_SUFFIX} "
        "ON organisation_memberships"
    )
    op.execute(f"DROP POLICY IF EXISTS {MEMBERSHIPS_USER_POLICY} ON organisation_memberships")
    op.execute("ALTER TABLE organisation_memberships NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organisation_memberships DISABLE ROW LEVEL SECURITY")

    # Restore the unrestricted UPDATE grant the earlier groups established.
    op.execute(f"REVOKE UPDATE ON invitations FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT UPDATE ON invitations TO {RUNTIME_ROLE}")

    op.execute(
        f"REVOKE EXECUTE ON FUNCTION app_current_invitation_provider_id() FROM {RUNTIME_ROLE}"
    )
    op.execute(f"REVOKE EXECUTE ON FUNCTION app_current_user_email() FROM {RUNTIME_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS app_current_invitation_provider_id()")
    op.execute("DROP FUNCTION IF EXISTS app_current_user_email()")
