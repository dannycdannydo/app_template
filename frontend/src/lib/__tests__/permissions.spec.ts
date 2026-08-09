import { describe, expect, it } from 'vitest'

import {
  isReadOnlyRoles,
  notificationPermissionsForRoles,
  recordPermissionsForRoles,
} from '@/lib/permissions'

describe('recordPermissionsForRoles', () => {
  it('grants owner full record write access', () => {
    expect(recordPermissionsForRoles(['owner'])).toEqual({
      canCreate: true,
      canUpdate: true,
      canDelete: true,
    })
  })

  it('grants administrator full record write access', () => {
    expect(recordPermissionsForRoles(['administrator'])).toEqual({
      canCreate: true,
      canUpdate: true,
      canDelete: true,
    })
  })

  it('grants manager create and update but not delete', () => {
    expect(recordPermissionsForRoles(['manager'])).toEqual({
      canCreate: true,
      canUpdate: true,
      canDelete: false,
    })
  })

  it('grants member create but neither update nor delete', () => {
    expect(recordPermissionsForRoles(['member'])).toEqual({
      canCreate: true,
      canUpdate: false,
      canDelete: false,
    })
  })

  it('grants viewer no write access at all', () => {
    expect(recordPermissionsForRoles(['viewer'])).toEqual({
      canCreate: false,
      canUpdate: false,
      canDelete: false,
    })
  })

  it('unions permissions across multiple roles (generous reading of /me)', () => {
    // `/me` returns the roles across all memberships; the union is the
    // documented approximation until the API exposes per-membership roles.
    expect(recordPermissionsForRoles(['viewer', 'manager'])).toEqual({
      canCreate: true,
      canUpdate: true,
      canDelete: false,
    })
  })

  it('denies everything for an unknown role', () => {
    expect(recordPermissionsForRoles(['auditor'])).toEqual({
      canCreate: false,
      canUpdate: false,
      canDelete: false,
    })
  })

  it('denies everything for no roles', () => {
    expect(recordPermissionsForRoles([])).toEqual({
      canCreate: false,
      canUpdate: false,
      canDelete: false,
    })
    expect(recordPermissionsForRoles(undefined)).toEqual({
      canCreate: false,
      canUpdate: false,
      canDelete: false,
    })
  })
})

describe('isReadOnlyRoles', () => {
  it('is true for a viewer', () => {
    expect(isReadOnlyRoles(['viewer'])).toBe(true)
  })

  it('is true for an unknown or empty role set', () => {
    expect(isReadOnlyRoles(['auditor'])).toBe(true)
    expect(isReadOnlyRoles([])).toBe(true)
    expect(isReadOnlyRoles(undefined)).toBe(true)
  })

  it('is false for roles with any write permission', () => {
    expect(isReadOnlyRoles(['owner'])).toBe(false)
    expect(isReadOnlyRoles(['manager'])).toBe(false)
    expect(isReadOnlyRoles(['member'])).toBe(false)
  })
})

describe('notificationPermissionsForRoles', () => {
  it('grants owner, administrator and manager read and manage', () => {
    expect(notificationPermissionsForRoles(['owner'])).toEqual({
      canRead: true,
      canManage: true,
    })
    expect(notificationPermissionsForRoles(['administrator'])).toEqual({
      canRead: true,
      canManage: true,
    })
    expect(notificationPermissionsForRoles(['manager'])).toEqual({
      canRead: true,
      canManage: true,
    })
  })

  it('grants member read but not manage', () => {
    expect(notificationPermissionsForRoles(['member'])).toEqual({
      canRead: true,
      canManage: false,
    })
  })

  it('grants viewer nothing (default deny)', () => {
    expect(notificationPermissionsForRoles(['viewer'])).toEqual({
      canRead: false,
      canManage: false,
    })
  })

  it('unions permissions across multiple roles (generous reading of /me)', () => {
    expect(notificationPermissionsForRoles(['viewer', 'manager'])).toEqual({
      canRead: true,
      canManage: true,
    })
  })

  it('denies everything for an unknown role or an empty role set', () => {
    expect(notificationPermissionsForRoles(['auditor'])).toEqual({
      canRead: false,
      canManage: false,
    })
    expect(notificationPermissionsForRoles([])).toEqual({
      canRead: false,
      canManage: false,
    })
    expect(notificationPermissionsForRoles(undefined)).toEqual({
      canRead: false,
      canManage: false,
    })
  })
})
