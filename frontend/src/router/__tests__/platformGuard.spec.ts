import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { RouteLocationNormalized, Router } from 'vue-router'

import { queryClient } from '@/queries/queryClient'
import { meQueryOptions } from '@/queries/me'
import { requiresPlatformAdmin } from '@/router'
import { useSessionStore } from '@/stores/session'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn<GetSignature>(),
  },
}))

type GetSignature = (url: string, init?: unknown) => Promise<unknown>

import { client } from '@/api/client'

const getMock = vi.mocked(client.GET as unknown as GetSignature)

/**
 * Platform Admin Centre guard tests (Scope §6.9, acceptance §5.10).
 *
 * The guard is exercised against a fresh memory-history router whose only
 * route carries `meta.requiresPlatformAdmin`; the session store and the
 * mocked `/me` payload drive every branch. The shared query client is
 * cleared between tests so the guard always performs its own fetch (the
 * cache never pre-decides an outcome).
 */
function buildRouter(): Router {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/platform',
        name: 'platform',
        component: { template: '<div>platform</div>' },
        meta: { requiresPlatformAdmin: true },
      },
      {
        path: '/records',
        name: 'records',
        component: { template: '<div>records</div>' },
      },
      { path: '/login', name: 'login', component: { template: '<div>login</div>' } },
      { path: '/', name: 'home', component: { template: '<div>home</div>' } },
    ],
  })
  return router
}

/** Resolve a path to the normalized location type the guard consumes. */
function resolveTo(router: Router, path: string): RouteLocationNormalized {
  return router.resolve(path) as unknown as RouteLocationNormalized
}

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

describe('requiresPlatformAdmin router guard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    queryClient.clear()
    getMock.mockReset()
  })

  it('passes routes that do not require the platform plane', async () => {
    const session = useSessionStore()
    session.markBootRestored()
    const router = buildRouter()
    const to = resolveTo(router, '/records')

    const result = await requiresPlatformAdmin(to)
    expect(result).toBe(true)
    expect(getMock).not.toHaveBeenCalled()
  })

  it('sends an unauthenticated visitor to /login with returnTo', async () => {
    const session = useSessionStore()
    session.markBootRestored()
    const router = buildRouter()
    const to = resolveTo(router, '/platform')

    const result = await requiresPlatformAdmin(to)
    expect(result).toEqual({ name: 'login', query: { returnTo: '/platform' } })
  })

  it('lets a user with platform_roles reach the centre', async () => {
    const session = useSessionStore()
    session.setSession('token-123')
    session.markBootRestored()
    getMock.mockResolvedValue({ data: mePayload(['platform_admin']), error: undefined })
    const router = buildRouter()
    const to = resolveTo(router, '/platform')

    const result = await requiresPlatformAdmin(to)
    expect(result).toBe(true)
  })

  it('sends a non-admin back home', async () => {
    const session = useSessionStore()
    session.setSession('token-123')
    session.markBootRestored()
    getMock.mockResolvedValue({ data: mePayload([]), error: undefined })
    const router = buildRouter()
    const to = resolveTo(router, '/platform')

    const result = await requiresPlatformAdmin(to)
    expect(result).toEqual({ name: 'home' })
  })

  it('sends a user home when /me cannot be loaded', async () => {
    const session = useSessionStore()
    session.setSession('token-123')
    session.markBootRestored()
    getMock.mockResolvedValue({ data: undefined, error: { code: 'server_error' } })
    const router = buildRouter()
    const to = resolveTo(router, '/platform')

    const result = await requiresPlatformAdmin(to)
    expect(result).toEqual({ name: 'home' })
  })

  it('reuses the shared /me cache entry for the decision', async () => {
    const session = useSessionStore()
    session.setSession('token-123')
    session.markBootRestored()
    // Seed the query cache first (what useMeQuery components would have done).
    getMock.mockResolvedValue({ data: mePayload(['platform_admin']), error: undefined })
    await queryClient.fetchQuery(meQueryOptions)
    getMock.mockClear()

    const router = buildRouter()
    const to = resolveTo(router, '/platform')
    const result = await requiresPlatformAdmin(to)

    expect(result).toBe(true)
    // The guard read the cached payload instead of issuing a second call.
    expect(getMock).not.toHaveBeenCalled()
  })
})
