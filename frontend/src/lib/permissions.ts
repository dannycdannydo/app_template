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
 * Organisation role codes offered by the platform admin centre (Scope §6.9).
 *
 * Mirrors the seeded `ROLE_PERMISSION_MAP` keys in
 * `backend/app/modules/permissions/constants.py`; the backend is the
 * enforcement point and rejects unknown role codes, so this list only shapes
 * the role select in the invite form and the memberships table.
 */
export const ORGANISATION_ROLE_CODES: readonly string[] = [
  'owner',
  'administrator',
  'manager',
  'member',
  'viewer',
]

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

/**
 * File write capabilities derived from the role codes returned by `/me`
 * (Scope §6.6, blueprint §14). Mirrors the backend `ROLE_PERMISSION_MAP`
 * seed (backend/app/modules/permissions/constants.py) for the `documents.*`
 * permissions:
 *
 * - `owner`, `administrator`, `manager`: upload and delete;
 * - `member`: upload only (no delete);
 * - `viewer`: read only (no write actions at all).
 *
 * Like the records bundle, this is the template's single frontend copy of the
 * `documents.*` role bundle; the backend stays the enforcement point.
 */
const FILE_ROLE_PERMISSIONS: Record<string, { canUpload: boolean; canDelete: boolean }> = {
  owner: { canUpload: true, canDelete: true },
  administrator: { canUpload: true, canDelete: true },
  manager: { canUpload: true, canDelete: true },
  member: { canUpload: true, canDelete: false },
  viewer: { canUpload: false, canDelete: false },
}

export interface FilePermissions {
  canUpload: boolean
  canDelete: boolean
}

const NO_FILE_PERMISSIONS: FilePermissions = { canUpload: false, canDelete: false }

/**
 * Union of file write permissions across the caller's role codes. Same
 * generous per-role union as `recordPermissionsForRoles`; enforcement is
 * server-side either way.
 */
export function filePermissionsForRoles(roles: readonly string[] | undefined): FilePermissions {
  if (!roles || roles.length === 0) {
    return NO_FILE_PERMISSIONS
  }
  let canUpload = false
  let canDelete = false
  for (const role of roles) {
    const bundle = FILE_ROLE_PERMISSIONS[role]
    if (!bundle) continue
    canUpload ||= bundle.canUpload
    canDelete ||= bundle.canDelete
  }
  return { canUpload, canDelete }
}

/**
 * Reactive file permissions for the current user (Scope §6.6). Reads the
 * roles from `useMeQuery` and exposes a single computed object so the files
 * view and upload component gate write actions in one place.
 */
export function useFilePermissions() {
  const { data, isPending } = useMeQuery()
  const roles = computed<readonly string[] | undefined>(() => data.value?.roles)
  const permissions = computed<FilePermissions>(() => filePermissionsForRoles(roles.value))
  return { roles, permissions, mePending: isPending }
}
