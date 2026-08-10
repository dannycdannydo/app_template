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

const mockUseUnreadNotificationsCountQuery = vi.hoisted(() => vi.fn<() => unknown>())
const mockUseNotificationsQuery = vi.hoisted(() => vi.fn<(params: unknown) => unknown>())
const mockUseMarkNotificationReadMutation = vi.hoisted(() =>
  vi.fn<(options?: unknown) => unknown>(),
)
const mockUseMarkAllNotificationsReadMutation = vi.hoisted(() =>
  vi.fn<(options?: unknown) => unknown>(),
)
vi.mock('@/queries/notifications', () => ({
  useUnreadNotificationsCountQuery: mockUseUnreadNotificationsCountQuery,
  useNotificationsQuery: mockUseNotificationsQuery,
  useMarkNotificationReadMutation: mockUseMarkNotificationReadMutation,
  useMarkAllNotificationsReadMutation: mockUseMarkAllNotificationsReadMutation,
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
      organisation_name: 'Example Organisation',
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
  mockUseMeQuery.mockReturnValue({ data: ref(me), isPending: ref(false), isError: ref(false) })
  mockUseUnreadNotificationsCountQuery.mockReturnValue({ data: ref({ unread_count: 0 }) })
  mockUseNotificationsQuery.mockReturnValue({
    data: ref({ items: [], page: 1, page_size: 5, total: 0, unread_count: 0 }),
    isPending: ref(false),
  })
  mockUseMarkNotificationReadMutation.mockReturnValue({
    mutate: vi.fn<(id: string) => void>(),
    isPending: ref(false),
  })
  mockUseMarkAllNotificationsReadMutation.mockReturnValue({
    mutate: vi.fn<() => void>(),
    isPending: ref(false),
  })
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
    mockUseUnreadNotificationsCountQuery.mockReset()
    mockUseNotificationsQuery.mockReset()
    mockUseMarkNotificationReadMutation.mockReset()
    mockUseMarkAllNotificationsReadMutation.mockReset()
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
    // Sidebar navigation links are present (Home, Records, Files, Notifications, About).
    const links = wrapper.find('[data-testid="sidebar"] nav').findAll('a')
    expect(links.map((link) => link.text())).toEqual([
      'Home',
      'Records',
      'Files',
      'Notifications',
      'About',
    ])
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
