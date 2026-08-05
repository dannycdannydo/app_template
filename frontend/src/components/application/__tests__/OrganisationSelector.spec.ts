import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { components } from '@/api/generated/openapi'

const mockUseMeQuery = vi.hoisted(() => vi.fn<() => unknown>())
vi.mock('@/queries/me', () => ({
  useMeQuery: mockUseMeQuery,
}))

import OrganisationSelector from '@/components/application/OrganisationSelector.vue'
import { useOrganisationStore } from '@/stores/organisation'

type MeResponse = components['schemas']['MeResponse']

const ORG_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const ORG_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'

function meWithMemberships(ids: string[]): MeResponse {
  return {
    user: {
      id: 'u1',
      email: 'ada@example.com',
      name: 'Ada Lovelace',
      is_active: true,
      created_at: '2026-01-01T00:00:00Z',
    },
    memberships: ids.map((organisation_id, index) => ({
      id: `m${index}`,
      organisation_id,
      user_id: 'u1',
      status: 'active',
      created_at: '2026-01-01T00:00:00Z',
    })),
    roles: ['owner'],
  }
}

const mountedWrappers: VueWrapper[] = []

async function mountSelector(): Promise<VueWrapper> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: { template: '<div>shell</div>' } }],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(OrganisationSelector, { global: { plugins: [createPinia(), router] } })
  mountedWrappers.push(wrapper)
  return wrapper
}

async function openMenu(wrapper: VueWrapper): Promise<void> {
  await wrapper.find('[data-testid="org-selector-trigger"]').trigger('click')
  await flushPromises()
}

describe('OrganisationSelector', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockUseMeQuery.mockReset()
  })

  afterEach(() => {
    // Unmount and clear portal leftovers (reka-ui teleports open menus to
    // document.body, which would otherwise leak into the next test).
    for (const wrapper of mountedWrappers.splice(0)) wrapper.unmount()
    document.body.innerHTML = ''
  })

  it('auto-selects the first active membership when nothing is persisted', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(meWithMemberships([ORG_A, ORG_B])) })
    const wrapper = await mountSelector()

    const organisation = useOrganisationStore()
    expect(organisation.selectedOrganisationId).toBe(ORG_A)
    expect(wrapper.find('[data-testid="org-selector-trigger"]').text()).toContain(
      'Organisation aaaaaaaa',
    )
  })

  it('keeps a persisted selection that is still a membership', async () => {
    localStorage.setItem('app-template:selected-organisation', ORG_B)
    mockUseMeQuery.mockReturnValue({ data: ref(meWithMemberships([ORG_A, ORG_B])) })
    const wrapper = await mountSelector()

    const organisation = useOrganisationStore()
    expect(organisation.selectedOrganisationId).toBe(ORG_B)
    expect(wrapper.find('[data-testid="org-selector-trigger"]').text()).toContain(
      'Organisation bbbbbbbb',
    )
  })

  it('replaces a persisted selection that is no longer a membership', async () => {
    localStorage.setItem(
      'app-template:selected-organisation',
      'zzzzzzzz-zzzz-4zzz-8zzz-zzzzzzzzzzzz',
    )
    mockUseMeQuery.mockReturnValue({ data: ref(meWithMemberships([ORG_A])) })
    await mountSelector()

    const organisation = useOrganisationStore()
    expect(organisation.selectedOrganisationId).toBe(ORG_A)
  })

  it('lists the memberships and persists the selected one on switch', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(meWithMemberships([ORG_A, ORG_B])) })
    const wrapper = await mountSelector()

    await openMenu(wrapper)

    const options = document.querySelectorAll('[data-testid="org-selector-option"]')
    expect(options).toHaveLength(2)
    expect(options[0]!.textContent).toContain('Organisation aaaaaaaa')
    expect(options[1]!.textContent).toContain('Organisation bbbbbbbb')

    ;(options[1] as HTMLElement).click()
    await flushPromises()

    const organisation = useOrganisationStore()
    expect(organisation.selectedOrganisationId).toBe(ORG_B)
    expect(localStorage.getItem('app-template:selected-organisation')).toBe(ORG_B)
    expect(wrapper.find('[data-testid="org-selector-trigger"]').text()).toContain(
      'Organisation bbbbbbbb',
    )
  })

  it('shows an empty state when the user has no memberships', async () => {
    mockUseMeQuery.mockReturnValue({ data: ref(meWithMemberships([])) })
    const wrapper = await mountSelector()

    const organisation = useOrganisationStore()
    expect(organisation.selectedOrganisationId).toBeNull()
    expect(wrapper.find('[data-testid="org-selector-trigger"]').text()).toContain('No organisation')
  })
})
