import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useOrganisationStore } from '@/stores/organisation'

describe('organisation store', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('starts with no selected organisation', () => {
    const organisation = useOrganisationStore()
    expect(organisation.selectedOrganisationId).toBeNull()
  })

  it('sets and clears the selected organisation', () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation('org-123')
    expect(organisation.selectedOrganisationId).toBe('org-123')
    organisation.setSelectedOrganisation(null)
    expect(organisation.selectedOrganisationId).toBeNull()
  })

  it('persists the selection to localStorage', () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation('org-123')
    expect(localStorage.getItem('app-template:selected-organisation')).toBe('org-123')
  })

  it('removes the persisted selection when cleared', () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation('org-123')
    organisation.setSelectedOrganisation(null)
    expect(localStorage.getItem('app-template:selected-organisation')).toBeNull()
  })

  it('hydrates the selection from localStorage', () => {
    localStorage.setItem('app-template:selected-organisation', 'org-123')
    const organisation = useOrganisationStore()
    expect(organisation.selectedOrganisationId).toBe('org-123')
  })
})
