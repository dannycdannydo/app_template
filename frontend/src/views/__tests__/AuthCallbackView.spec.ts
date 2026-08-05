import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Router } from 'vue-router'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/features/auth/workos', () => ({
  completeLogin: vi.fn<() => Promise<string>>(),
}))

import { completeLogin } from '@/features/auth/workos'
import { useSessionStore } from '@/stores/session'
import AuthCallbackView from '@/views/AuthCallbackView.vue'

const completeLoginMock = vi.mocked(completeLogin)

async function mountCallback(query: string): Promise<{
  router: Router
  session: ReturnType<typeof useSessionStore>
}> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'home', component: { template: '<div>home</div>' } },
      { path: '/login', name: 'login', component: { template: '<div>login</div>' } },
      { path: '/auth/callback', name: 'auth-callback', component: AuthCallbackView },
    ],
  })
  await router.push(`/auth/callback${query}`)
  await router.isReady()
  mount(AuthCallbackView, { global: { plugins: [pinia, router] } })
  return { router, session: useSessionStore() }
}

describe('AuthCallbackView', () => {
  beforeEach(() => {
    completeLoginMock.mockReset()
  })

  it('completes the code flow, stores the session and redirects to the shell', async () => {
    completeLoginMock.mockResolvedValue('token-from-workos')
    const { router, session } = await mountCallback('?code=auth-code-123')

    await flushPromises()

    expect(completeLoginMock).toHaveBeenCalledOnce()
    expect(session.token).toBe('token-from-workos')
    expect(session.isAuthenticated).toBe(true)
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('stores nothing and returns to login when WorkOS denied the flow', async () => {
    const { router, session } = await mountCallback('?error=access_denied')

    await flushPromises()

    expect(completeLoginMock).not.toHaveBeenCalled()
    expect(session.token).toBeNull()
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query.error).toBe('access_denied')
  })

  it('stores nothing and returns to login when the callback has no code', async () => {
    const { router, session } = await mountCallback('')

    await flushPromises()

    expect(completeLoginMock).not.toHaveBeenCalled()
    expect(session.token).toBeNull()
    expect(router.currentRoute.value.query.error).toBe('invalid_callback')
  })

  it('stores nothing and returns to login when the exchange fails', async () => {
    completeLoginMock.mockRejectedValueOnce(new Error('code exchange failed'))
    const { router, session } = await mountCallback('?code=bad-code')

    await flushPromises()

    expect(session.token).toBeNull()
    expect(router.currentRoute.value.path).toBe('/login')
    expect(router.currentRoute.value.query.error).toBe('login_failed')
  })
})
