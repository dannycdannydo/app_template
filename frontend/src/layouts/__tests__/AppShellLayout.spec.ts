import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { Router } from 'vue-router'

import type { components } from '@/api/generated/openapi'

const mockUseMeQuery = vi.hoisted(() => vi.fn<() => unknown>())
vi.mock('@/queries/me', () => ({
  useMeQuery: mockUseMeQuery,
}))

import AppShellLayout from '@/layouts/AppShellLayout.vue'
import { requiresAuth } from '@/router'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'

type MeResponse = components['schemas']['MeResponse']

const me: MeResponse = {
  user: {
    id: 'u1',
    email: 'ada@example.com',
    name: 'Ada Lovelace',
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
  },
  memberships: [
    {
      id: 'm1',
      organisation_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      user_id: 'u1',
      status: 'active',
      created_at: '2026-01-01T00:00:00Z',
    },
  ],
  roles: ['owner'],
  platform_roles: [],
}

/** Mounts the layout the way the app does: through a root RouterView. */
async function mountShell(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/',
        component: AppShellLayout,
        meta: { requiresAuth: true },
        children: [{ path: '', component: { template: '<div>shell content</div>' } }],
      },
    ],
  })
  await router.push('/')
  await router.isReady()
  mockUseMeQuery.mockReturnValue({ data: ref(me) })
  const wrapper = mount(
    { template: '<RouterView />' },
    { global: { plugins: [createPinia(), router] } },
  )
  return { wrapper, router }
}

describe('AppShellLayout', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockUseMeQuery.mockReset()
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('renders the sidebar, header controls and routed content', async () => {
    const { wrapper } = await mountShell()

    expect(wrapper.find('[data-testid="sidebar"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="org-selector-trigger"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="user-menu-trigger"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="mobile-nav-trigger"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="sidebar-toggle"]').exists()).toBe(true)
    // Sidebar navigation links are present (Home, Records, About).
    const links = wrapper.find('[data-testid="sidebar"] nav').findAll('a')
    expect(links.map((link) => link.text())).toEqual(['Home', 'Records', 'About'])
  })

  it('toggles the collapsed sidebar through the header button and persists it', async () => {
    const { wrapper } = await mountShell()
    const ui = useUiStore()
    expect(ui.sidebarCollapsed).toBe(false)

    await wrapper.find('[data-testid="sidebar-toggle"]').trigger('click')

    expect(ui.sidebarCollapsed).toBe(true)
    expect(localStorage.getItem('app-template:sidebar-collapsed')).toBe('true')
  })

  it('respects a persisted collapsed state on load', async () => {
    localStorage.setItem('app-template:sidebar-collapsed', 'true')
    const { wrapper } = await mountShell()
    const ui = useUiStore()

    expect(ui.sidebarCollapsed).toBe(true)
    expect(wrapper.find('[data-testid="sidebar"]').classes()).toContain('w-16')
  })

  it('keeps the shell reachable only with a session (guard integration)', async () => {
    const session = useSessionStore()
    session.markBootRestored()
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/login', name: 'login', component: { template: '<div>login</div>' } },
        {
          path: '/',
          component: AppShellLayout,
          meta: { requiresAuth: true },
          children: [{ path: '', component: { template: '<div>shell</div>' } }],
        },
      ],
    })
    router.beforeEach(requiresAuth)

    await router.push('/')
    await router.isReady()

    expect(router.currentRoute.value.path).toBe('/login')
  })
})
