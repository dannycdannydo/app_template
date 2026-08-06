import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { Router } from 'vue-router'

import { requiresAuth } from '@/router'
import { useSessionStore } from '@/stores/session'

/**
 * Router-guard tests (v0.3 Scope §6.3, acceptance §5.4). The guard is exercised
 * against a fresh memory-history router with stub components; the session
 * store drives both sides of the decision. Boot-restore is marked immediately
 * in setup so the guard never waits on the app's bootstrap latch.
 */
function buildRouter(): Router {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/',
        name: 'home',
        component: { template: '<div>home</div>' },
        meta: { requiresAuth: true },
      },
      {
        path: '/about',
        name: 'about',
        component: { template: '<div>about</div>' },
        meta: { requiresAuth: true },
      },
      { path: '/login', name: 'login', component: { template: '<div>login</div>' } },
      { path: '/auth/callback', name: 'auth-callback', component: { template: '<div>cb</div>' } },
    ],
  })
  router.beforeEach(requiresAuth)
  return router
}

describe('requiresAuth router guard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('redirects an unauthenticated visitor away from a protected route to /login', async () => {
    const session = useSessionStore()
    session.markBootRestored()
    const router = buildRouter()

    await router.push('/about')
    await router.isReady()

    expect(router.currentRoute.value.path).toBe('/login')
  })

  it('redirects an unauthenticated visitor away from the shell root to /login', async () => {
    const session = useSessionStore()
    session.markBootRestored()
    const router = buildRouter()

    await router.push('/')
    await router.isReady()

    expect(router.currentRoute.value.path).toBe('/login')
  })

  it('lets an authenticated visitor reach a protected route', async () => {
    const session = useSessionStore()
    session.setSession('token-123')
    session.markBootRestored()
    const router = buildRouter()

    await router.push('/about')
    await router.isReady()

    expect(router.currentRoute.value.path).toBe('/about')
  })

  it('redirects an authenticated visitor away from /login to the shell', async () => {
    const session = useSessionStore()
    session.setSession('token-123')
    session.markBootRestored()
    const router = buildRouter()

    await router.push('/login')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('home')
  })

  it('keeps /login public for an unauthenticated visitor', async () => {
    const session = useSessionStore()
    session.markBootRestored()
    const router = buildRouter()

    await router.push('/login')
    await router.isReady()

    expect(router.currentRoute.value.path).toBe('/login')
  })

  it('keeps the auth callback public without a session', async () => {
    const session = useSessionStore()
    session.markBootRestored()
    const router = buildRouter()

    await router.push('/auth/callback?code=abc')
    await router.isReady()

    expect(router.currentRoute.value.path).toBe('/auth/callback')
    expect(router.currentRoute.value.query.code).toBe('abc')
  })

  it('waits for boot-restore before deciding the first navigation', async () => {
    const session = useSessionStore()
    // Boot-restore is deliberately NOT marked yet: the guard must suspend until
    // the session is restored, otherwise an authenticated reload would bounce.
    session.setSession('token-123')
    const router = buildRouter()

    const navigation = router.push('/about')
    let settled = false
    void navigation.finally(() => {
      settled = true
    })

    // Give the guard a chance to (wrongly) settle before restore completes.
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(settled).toBe(false)

    session.markBootRestored()
    await navigation
    await router.isReady()

    expect(router.currentRoute.value.path).toBe('/about')
  })
})
