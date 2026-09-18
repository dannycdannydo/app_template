import { describe, expect, it } from 'vitest'

import type { components } from '@/api/generated/openapi'
import {
  isReadOnlyRoles,
  notificationPermissionsForRoles,
  recordPermissionsForRoles,
  selectedOrganisationRoles,
} from '@/lib/permissions'

type MeMembershipListItem = components['schemas']['MeMembershipListItem']

function membership(
  organisationId: string,
  roles: string[],
  overrides: Partial<MeMembershipListItem> = {},
): MeMembershipListItem {
  return {
    id: `m-${organisationId}`,
    organisation_id: organisationId,
    organisation_name: `Org ${organisationId}`,
    user_id: 'u1',
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
    roles,
    ...overrides,
  }
}

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

  it('unions permissions across multiple roles within one organisation', () => {
    // A membership may hold several roles; the union is scoped to that
    // organisation's membership, never across organisations.
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

  it('unions permissions across multiple roles within one membership', () => {
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

describe('selectedOrganisationRoles', () => {
  const memberships = [membership('org-a', ['owner']), membership('org-b', ['viewer'])]

  it('returns the roles of the selected active membership only', () => {
    expect(selectedOrganisationRoles(memberships, 'org-a')).toEqual(['owner'])
    expect(selectedOrganisationRoles(memberships, 'org-b')).toEqual(['viewer'])
  })

  it('never returns the union when another organisation grants a role', () => {
    // The owner role in org-a must not leak into org-b's selected authority.
    const viewerRoles = selectedOrganisationRoles(memberships, 'org-b')
    expect(recordPermissionsForRoles(viewerRoles)).toEqual({
      canCreate: false,
      canUpdate: false,
      canDelete: false,
    })
  })

  it('returns undefined when no organisation is selected', () => {
    expect(selectedOrganisationRoles(memberships, null)).toBeUndefined()
    expect(selectedOrganisationRoles(memberships, '')).toBeUndefined()
  })

  it('returns undefined for an organisation the user does not belong to', () => {
    expect(selectedOrganisationRoles(memberships, 'org-missing')).toBeUndefined()
  })

  it('grants nothing for a non-active membership', () => {
    const suspended = [membership('org-a', ['owner'], { status: 'suspended' })]
    expect(selectedOrganisationRoles(suspended, 'org-a')).toBeUndefined()
  })

  it('returns undefined when memberships are not loaded', () => {
    expect(selectedOrganisationRoles(undefined, 'org-a')).toBeUndefined()
  })
})
