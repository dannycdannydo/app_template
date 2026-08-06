import { computed } from 'vue'

import { useMeQuery } from '@/queries/me'

/**
 * Record write capabilities derived from the role codes returned by `/me`
 * (v0.3 Scope §6.7, blueprint §14).
 *
 * The backend remains the enforcement point: `require_permission` gates every
 * records route with default deny (blueprint §9), so a viewer who somehow
 * reaches a write action still gets `403`. This module only decides what the
 * UI offers, mirroring the backend's `ROLE_PERMISSION_MAP` seed
 * (backend/app/modules/permissions/constants.py) for the `records.*`
 * permissions:
 *
 * - `owner`, `administrator`: create, update and delete;
 * - `manager`: create and update (no delete);
 * - `member`: create only (no update, no delete);
 * - `viewer`: read only (no write actions at all).
 *
 * The map is the template's single frontend copy of the role-to-permission
 * bundle. If a later release exposes permissions server-side (e.g. a
 * `GET /api/v1/me/permissions` endpoint), this module becomes a thin client
 * of that and the duplicated bundle disappears; until then the bundle is
 * mirrored here deliberately so a new role or bundle change fails review on
 * both sides of the stack.
 */
const RECORD_ROLE_PERMISSIONS: Record<
  string,
  { canCreate: boolean; canUpdate: boolean; canDelete: boolean }
> = {
  owner: { canCreate: true, canUpdate: true, canDelete: true },
  administrator: { canCreate: true, canUpdate: true, canDelete: true },
  manager: { canCreate: true, canUpdate: true, canDelete: false },
  member: { canCreate: true, canUpdate: false, canDelete: false },
  viewer: { canCreate: false, canUpdate: false, canDelete: false },
}

export interface RecordPermissions {
  canCreate: boolean
  canUpdate: boolean
  canDelete: boolean
}

const NO_RECORD_PERMISSIONS: RecordPermissions = {
  canCreate: false,
  canUpdate: false,
  canDelete: false,
}

/**
 * Union of record write permissions across the caller's role codes.
 *
 * `/me` returns the distinct role codes across all of the caller's
 * memberships, not per-organisation roles (v0.2 contract). Treating any role
 * that grants a permission as granting it app-wide is the generous reading;
 * a user with owner in one organisation and viewer in another sees write
 * actions in the viewer organisation, where the backend still answers `403`.
 * The strict per-membership alternative is impossible without a backend
 * change and is deferred; enforcement is server-side either way.
 */
export function recordPermissionsForRoles(roles: readonly string[] | undefined): RecordPermissions {
  if (!roles || roles.length === 0) {
    return NO_RECORD_PERMISSIONS
  }
  let canCreate = false
  let canUpdate = false
  let canDelete = false
  for (const role of roles) {
    const bundle = RECORD_ROLE_PERMISSIONS[role]
    if (!bundle) continue
    canCreate ||= bundle.canCreate
    canUpdate ||= bundle.canUpdate
    canDelete ||= bundle.canDelete
  }
  return { canCreate, canUpdate, canDelete }
}

/**
 * Read-only helper for the common case: a viewer (or an unknown role set)
 * sees no write actions anywhere.
 */
export function isReadOnlyRoles(roles: readonly string[] | undefined): boolean {
  const permissions = recordPermissionsForRoles(roles)
  return !permissions.canCreate && !permissions.canUpdate && !permissions.canDelete
}

/**
 * Reactive record permissions for the current user (v0.3 Scope §6.7).
 *
 * Reads the roles from `useMeQuery` and exposes a single computed object so
 * views can gate write actions in one place. The query layer already owns
 * `/me`; this composable only derives UI affordances from it and never
 * touches the HTTP client (blueprint §14, §15).
 */
export function useRecordPermissions() {
  const { data, isPending, isError } = useMeQuery()
  const roles = computed<readonly string[] | undefined>(() => data.value?.roles)
  const permissions = computed<RecordPermissions>(() => recordPermissionsForRoles(roles.value))
  const isReadOnly = computed(() => {
    const p = permissions.value
    return !p.canCreate && !p.canUpdate && !p.canDelete
  })
  return { roles, permissions, isReadOnly, mePending: isPending, meError: isError }
}
