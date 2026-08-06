import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useOrganisationStore } from '@/stores/organisation'
import { useSessionStore } from '@/stores/session'

const fetchMock = vi.fn<(input: Request, init?: RequestInit) => Promise<Response>>()
const signOutMock = vi.hoisted(() => vi.fn<() => Promise<void>>())

vi.mock('@/features/auth/workos', () => ({
  signOut: signOutMock,
}))

/**
 * Stub `fetch` before the client module evaluates: openapi-fetch captures the
 * global fetch at `createClient` time, so the mock must be in place before the
 * dynamic import below resolves.
 */
vi.stubGlobal('fetch', fetchMock)

let client: (typeof import('@/api/client'))['client']
let originalLocation: Location

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

beforeEach(async () => {
  vi.resetModules()
  fetchMock.mockReset()
  signOutMock.mockReset()
  signOutMock.mockResolvedValue(undefined)
  localStorage.clear()
  setActivePinia(createPinia())
  // jsdom location methods are not spyable; swap in a stub we can assert on.
  originalLocation = window.location
  const assignMock = vi.fn<(path: string) => void>()
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...originalLocation, assign: assignMock },
  })
  // openapi-fetch builds absolute URLs, so the client needs a base URL.
  vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8000')
  ;({ client } = await import('@/api/client'))
})

afterEach(() => {
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: originalLocation,
  })
  vi.unstubAllEnvs()
})

describe('client bearer-token injection', () => {
  it('attaches the session token as a Bearer Authorization header', async () => {
    const session = useSessionStore()
    session.setSession('token-abc')

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ user: { id: 'u1', email: 'a@b.c' }, memberships: [], roles: [] }),
    )

    await client.GET('/api/v1/me')

    expect(fetchMock).toHaveBeenCalledOnce()
    const [request] = fetchMock.mock.calls[0]!
    expect(request.headers.get('authorization')).toBe('Bearer token-abc')
  })

  it('sends no Authorization header without a session', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ status: 'ok' }))

    await client.GET('/health')

    const [request] = fetchMock.mock.calls[0]!
    expect(request.headers.has('authorization')).toBe(false)
  })

  it('does not overwrite an explicit Authorization header', async () => {
    const session = useSessionStore()
    session.setSession('token-abc')

    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], page: 1, page_size: 50, total: 0 }))

    await client.GET('/api/v1/records', {
      params: { header: { authorization: 'Bearer explicit' } },
    })

    const [request] = fetchMock.mock.calls[0]!
    expect(request.headers.get('authorization')).toBe('Bearer explicit')
  })
})

describe('client organisation-context injection', () => {
  it('attaches the selected organisation as the X-Org-Id header', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation('org-aaa')

    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], page: 1, page_size: 50, total: 0 }))

    await client.GET('/api/v1/records')

    expect(fetchMock).toHaveBeenCalledOnce()
    const [request] = fetchMock.mock.calls[0]!
    expect(request.headers.get('x-org-id')).toBe('org-aaa')
  })

  it('sends no X-Org-Id header without a selected organisation', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], page: 1, page_size: 50, total: 0 }))

    await client.GET('/api/v1/records')

    const [request] = fetchMock.mock.calls[0]!
    expect(request.headers.has('x-org-id')).toBe(false)
  })

  it('does not overwrite an explicit X-Org-Id header', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation('org-aaa')

    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], page: 1, page_size: 50, total: 0 }))

    await client.GET('/api/v1/records', {
      params: { header: { 'x-org-id': 'org-explicit' } },
    })

    const [request] = fetchMock.mock.calls[0]!
    expect(request.headers.get('x-org-id')).toBe('org-explicit')
  })
})

describe('client 401 handling', () => {
  it('clears both session layers and redirects once on 401', async () => {
    const session = useSessionStore()
    const organisation = useOrganisationStore()
    session.setSession('expired-token')
    organisation.setSelectedOrganisation('org-aaa')
    const assignMock = window.location.assign as ReturnType<typeof vi.fn<(path: string) => void>>

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ code: 'unauthorized', message: 'Session expired.', request_id: 'req-9' }, 401),
    )

    await expect(client.GET('/api/v1/me')).rejects.toMatchObject({
      status: 401,
      code: 'unauthorized',
      requestId: 'req-9',
    })

    expect(session.token).toBeNull()
    expect(session.isAuthenticated).toBe(false)
    expect(organisation.selectedOrganisationId).toBeNull()
    expect(signOutMock).toHaveBeenCalledOnce()
    expect(assignMock).toHaveBeenCalledWith('/login?error=session_invalid')
  })

  it('redirects after a WorkOS logout failure instead of retaining the rejected session', async () => {
    const session = useSessionStore()
    session.setSession('rejected-token')
    signOutMock.mockRejectedValueOnce(new Error('logout unavailable'))
    const assignMock = window.location.assign as ReturnType<typeof vi.fn<(path: string) => void>>

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ code: 'unauthorized', message: 'Session rejected.', request_id: 'req-10' }, 401),
    )

    await expect(client.GET('/api/v1/me')).rejects.toMatchObject({ status: 401 })

    expect(session.token).toBeNull()
    expect(assignMock).toHaveBeenCalledWith('/login?error=session_invalid')
  })

  it('normalizes non-2xx responses into the typed error envelope', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { code: 'validation_error', message: 'Bad input.', details: null, request_id: 'req-10' },
        422,
      ),
    )

    const error = await client.GET('/api/v1/records').catch((e: unknown) => e)

    expect(error).toMatchObject({ status: 422, code: 'validation_error', requestId: 'req-10' })
    expect(error).toHaveProperty('message', 'Bad input.')
  })
})
