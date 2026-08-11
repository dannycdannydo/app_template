import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { clearLocalSession } from '@/features/auth/localSession'
import { queryClient } from '@/queries/queryClient'
import { useOrganisationStore } from '@/stores/organisation'
import { useSessionStore } from '@/stores/session'

describe('clearLocalSession', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    queryClient.clear()
  })

  it('clears authentication, tenant selection and cached server data together', () => {
    const session = useSessionStore()
    const organisation = useOrganisationStore()
    session.setSession('token-for-user-a')
    organisation.setSelectedOrganisation('org-a')
    queryClient.setQueryData(['me'], { user: { id: 'user-a' } })
    queryClient.setQueryData(['records', 'org-a'], { items: [{ id: 'record-a' }] })

    clearLocalSession()

    expect(session.token).toBeNull()
    expect(session.isAuthenticated).toBe(false)
    expect(organisation.selectedOrganisationId).toBeNull()
    expect(localStorage.getItem('app-template:selected-organisation')).toBeNull()
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0)
  })
})
