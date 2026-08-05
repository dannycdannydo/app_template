import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { installOrganisationSwitchInvalidation } from '@/queries/organisationInvalidation'
import { recordsQueryKeys } from '@/queries/records'
import { queryClient } from '@/queries/queryClient'
import { useOrganisationStore } from '@/stores/organisation'

const ORG_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const ORG_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

let stop: (() => void) | null = null

describe('installOrganisationSwitchInvalidation', () => {
  beforeEach(() => {
    localStorage.clear()
    queryClient.clear()
    const pinia = createPinia()
    setActivePinia(pinia)
    stop = installOrganisationSwitchInvalidation(pinia)
  })

  afterEach(() => {
    stop?.()
    stop = null
  })

  it('refetches org-scoped queries when the selected organisation changes', async () => {
    const listKeyA = recordsQueryKeys.list(ORG_A, { page: 1, pageSize: 50 })
    const listKeyB = recordsQueryKeys.list(ORG_B, { page: 1, pageSize: 50 })
    queryClient.setQueryData(listKeyA, { items: [], page: 1, page_size: 50, total: 0 })
    queryClient.setQueryData(listKeyB, { items: [], page: 1, page_size: 50, total: 0 })
    // Global (non-org-scoped) data must be left alone.
    queryClient.setQueryData(['me'], {})

    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    organisation.setSelectedOrganisation(ORG_B)
    await flushPromises()

    expect(queryClient.getQueryState(listKeyA)?.isInvalidated).toBe(true)
    expect(queryClient.getQueryState(listKeyB)?.isInvalidated).toBe(true)
    expect(queryClient.getQueryState(['me'])?.isInvalidated).toBe(false)
  })

  it('does nothing when the selection never changes', async () => {
    const listKeyA = recordsQueryKeys.list(ORG_A, { page: 1, pageSize: 50 })
    queryClient.setQueryData(listKeyA, { items: [], page: 1, page_size: 50, total: 0 })

    await flushPromises()

    expect(queryClient.getQueryState(listKeyA)?.isInvalidated).toBe(false)
  })
})
