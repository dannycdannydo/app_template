import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import type { Router } from 'vue-router'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { components } from '@/api/generated/openapi'

const mockUseMeQuery = vi.hoisted(() => vi.fn<() => unknown>())
vi.mock('@/queries/me', () => ({
  useMeQuery: mockUseMeQuery,
}))

vi.mock('@/features/auth/workos', () => ({
  signOut: vi.fn<() => Promise<void>>(),
}))

import UserMenu from '@/components/application/UserMenu.vue'
import { signOut } from '@/features/auth/workos'
import { useSessionStore } from '@/stores/session'

const signOutMock = vi.mocked(signOut)

type MeResponse = components['schemas']['MeResponse']

const me: MeResponse = {
  user: {
    id: 'u1',
    email: 'ada@example.com',
    name: 'Ada Lovelace',
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
  },
  memberships: [],
  roles: ['owner'],
}

const mountedWrappers: VueWrapper[] = []

async function mountUserMenu(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/login', name: 'login', component: { template: '<div>login</div>' } }],
  })
  await router.push('/')
  await router.isReady()
  mockUseMeQuery.mockReturnValue({ data: ref(me) })
  const wrapper = mount(UserMenu, { global: { plugins: [createPinia(), router] } })
  mountedWrappers.push(wrapper)
  return { wrapper, router }
}

describe('UserMenu', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    signOutMock.mockReset()
    signOutMock.mockResolvedValue(undefined)
    mockUseMeQuery.mockReset()
  })

  afterEach(() => {
    // Unmount and clear portal leftovers (reka-ui teleports open menus to
    // document.body, which would otherwise leak into the next test).
    for (const wrapper of mountedWrappers.splice(0)) wrapper.unmount()
    document.body.innerHTML = ''
  })

  it('shows the current user name and initials from /me', async () => {
    const { wrapper } = await mountUserMenu()

    expect(wrapper.find('[data-testid="user-menu-name"]').text()).toBe('Ada Lovelace')
    expect(wrapper.find('[data-testid="user-menu-fallback"]').text()).toBe('AL')
  })

  it('shows name and email in the opened menu', async () => {
    const { wrapper } = await mountUserMenu()

    await wrapper.find('[data-testid="user-menu-trigger"]').trigger('click')
    await flushPromises()

    expect(document.querySelector('[data-testid="user-menu-display-name"]')?.textContent).toBe(
      'Ada Lovelace',
    )
    expect(document.querySelector('[data-testid="user-menu-email"]')?.textContent).toBe(
      'ada@example.com',
    )
  })

  it('signs out, clears the session and returns to /login', async () => {
    const { wrapper, router } = await mountUserMenu()
    const session = useSessionStore()
    session.setSession('token-123')

    await wrapper.find('[data-testid="user-menu-trigger"]').trigger('click')
    await flushPromises()

    const signOutItem = document.querySelector('[data-testid="user-menu-sign-out"]')
    expect(signOutItem).not.toBeNull()
    ;(signOutItem as HTMLElement).click()
    await flushPromises()

    expect(signOutMock).toHaveBeenCalledOnce()
    expect(session.token).toBeNull()
    expect(router.currentRoute.value.path).toBe('/login')
  })
})
