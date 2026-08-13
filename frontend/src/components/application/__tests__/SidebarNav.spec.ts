import { VueQueryPlugin } from '@tanstack/vue-query'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import SidebarNav from '@/components/application/SidebarNav.vue'
import { queryClient } from '@/queries/queryClient'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn<typeof client.GET>(),
  },
}))

import { client } from '@/api/client'

const getMock = vi.mocked(client.GET)

/**
 * SidebarNav platform-entry tests (Scope §6.9, acceptance §5.10).
 *
 * The "Platform Admin" entry is shown only when `/me` reports
 * `platform_roles` (Scope §6.2). While `/me` is loading the entry stays
 * hidden, and for non-admins it never appears — the backend remains the
 * enforcement point, this only shapes what the UI offers.
 */
function mePayload(platformRoles: string[] = []): object {
  return {
    user: {
      id: 'u1',
      email: 'ada@example.com',
      name: 'Ada Lovelace',
      is_active: true,
      created_at: '2026-01-01T00:00:00Z',
    },
    memberships: [],
    roles: [],
    platform_roles: platformRoles,
  }
}

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

function buildRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: { template: '<div>home</div>' } },
      { path: '/records', name: 'records', component: { template: '<div>records</div>' } },
      { path: '/ai/ask', name: 'ai-ask', component: { template: '<div>ai-ask</div>' } },
      { path: '/about', name: 'about', component: { template: '<div>about</div>' } },
      { path: '/platform', name: 'platform', component: { template: '<div>platform</div>' } },
    ],
  })
}

describe('SidebarNav platform entry', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    queryClient.clear()
    getMock.mockReset()
  })

  it('shows the Platform Admin entry for a user with platform_roles', async () => {
    getMock.mockResolvedValue({ data: mePayload(['platform_admin']), error: undefined })
    const wrapper = mount(SidebarNav, {
      global: { plugins: [VueQueryPlugin, buildRouter()] },
    })
    await flushPromises()
    await flushPromises()

    const links = wrapper.findAll('a').map((link) => link.text())
    expect(links).toContain('Platform Admin')
    expect(links).toContain('Home')
    expect(links).toContain('Records')
  })

  it('hides the Platform Admin entry for a non-admin', async () => {
    getMock.mockResolvedValue({ data: mePayload([]), error: undefined })
    const wrapper = mount(SidebarNav, {
      global: { plugins: [VueQueryPlugin, buildRouter()] },
    })
    await flushPromises()
    await flushPromises()

    const links = wrapper.findAll('a').map((link) => link.text())
    expect(links).not.toContain('Platform Admin')
    expect(links).toContain('Home')
  })

  it('does not flash the entry while /me is still loading', () => {
    getMock.mockReturnValue(new Promise(() => undefined))
    const wrapper = mount(SidebarNav, {
      global: { plugins: [VueQueryPlugin, buildRouter()] },
    })

    const links = wrapper.findAll('a').map((link) => link.text())
    expect(links).not.toContain('Platform Admin')
  })
})
