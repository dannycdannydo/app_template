import { readFile, readdir } from 'node:fs/promises'
import { join, resolve } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

interface FakeWorkOSClient {
  getSignInUrl: Mock<(options?: unknown) => Promise<string>>
  getAccessToken: Mock<() => Promise<string>>
  signOut: Mock<(options?: unknown) => void>
}

interface CreateClientOptionsLike {
  redirectUri?: string
  onRedirectCallback?: (params: { accessToken: string; state: unknown }) => void
}

const createClientMock = vi.hoisted(() =>
  vi.fn<(clientId: string, options?: CreateClientOptionsLike) => Promise<FakeWorkOSClient>>(),
)

vi.mock('@workos-inc/authkit-js', () => ({
  createClient: createClientMock,
}))

type WorkOSModule = typeof import('@/features/auth/workos')

function makeClient(): FakeWorkOSClient {
  return {
    getSignInUrl: vi
      .fn<(options?: unknown) => Promise<string>>()
      .mockResolvedValue('https://api.workos.com/user_management/authorize'),
    getAccessToken: vi.fn<() => Promise<string>>(),
    signOut: vi.fn<(options?: unknown) => void>(),
  }
}

describe('WorkOS adapter (src/features/auth/workos)', () => {
  let workos: WorkOSModule
  let originalLocation: Location

  beforeEach(async () => {
    vi.resetModules()
    createClientMock.mockReset()
    localStorage.clear()
    document.cookie = 'workos-has-session=; Max-Age=0; Path=/'
    vi.stubEnv('VITE_WORKOS_CLIENT_ID', 'client_test123')
    vi.stubEnv('VITE_WORKOS_REDIRECT_URI', 'https://app.example.test/auth/callback')
    // jsdom location methods are not spyable; swap in a stub we can assert on.
    originalLocation = window.location
    const assignMock = vi.fn<(path: string) => void>()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...originalLocation, origin: originalLocation.origin, assign: assignMock },
    })
    workos = await import('@/features/auth/workos')
  })

  afterEach(() => {
    document.cookie = 'workos-has-session=; Max-Age=0; Path=/'
    Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
    vi.unstubAllEnvs()
  })

  it('creates the WorkOS client with the configured client id and callback URL', async () => {
    createClientMock.mockResolvedValue(makeClient())

    await workos.startLogin()

    expect(createClientMock).toHaveBeenCalledWith('client_test123', {
      redirectUri: 'https://app.example.test/auth/callback',
      onRedirectCallback: expect.any(Function),
    })
  })

  it('defaults the callback URL to the /auth/callback route on the current origin', async () => {
    vi.stubEnv('VITE_WORKOS_REDIRECT_URI', '')
    vi.resetModules()
    workos = await import('@/features/auth/workos')
    createClientMock.mockResolvedValue(makeClient())

    await workos.startLogin()

    const expected = new URL('/auth/callback', window.location.origin).toString()
    expect(createClientMock).toHaveBeenCalledWith(
      'client_test123',
      expect.objectContaining({ redirectUri: expected }),
    )
  })

  it('removes a stale localhost session hint when its refresh token is absent', async () => {
    document.cookie = 'workos-has-session=client_test123; Path=/'
    createClientMock.mockResolvedValue(makeClient())

    await workos.startLogin()

    expect(document.cookie).not.toContain('workos-has-session=client_test123')
  })

  it('retains the localhost session hint when its refresh token exists', async () => {
    document.cookie = 'workos-has-session=client_test123; Path=/'
    localStorage.setItem('workos:refresh-token:client_test123', 'refresh-token')
    createClientMock.mockResolvedValue(makeClient())

    await workos.startLogin()

    expect(document.cookie).toContain('workos-has-session=client_test123')
  })

  it('startLogin redirects the browser to the WorkOS authorization URL', async () => {
    const client = makeClient()
    client.getSignInUrl.mockResolvedValue(
      'https://api.workos.com/user_management/authorize?client_id=client_test123&redirect_uri=https%3A%2F%2Fapp.example.test%2Fauth%2Fcallback',
    )
    createClientMock.mockResolvedValue(client)

    await workos.startLogin()

    expect(client.getSignInUrl).toHaveBeenCalledWith({})
    const assignMock = window.location.assign as ReturnType<typeof vi.fn<(path: string) => void>>
    expect(assignMock).toHaveBeenCalledWith(expect.stringContaining('client_id=client_test123'))
  })

  it('startLogin carries a returnTo target in the OAuth state', async () => {
    const client = makeClient()
    createClientMock.mockResolvedValue(client)

    await workos.startLogin({ returnTo: '/records' })

    expect(client.getSignInUrl).toHaveBeenCalledWith({ state: { returnTo: '/records' } })
  })

  it('startLogin fails fast when the client id is not configured', async () => {
    vi.stubEnv('VITE_WORKOS_CLIENT_ID', '')
    vi.resetModules()
    workos = await import('@/features/auth/workos')

    await expect(workos.startLogin()).rejects.toThrow(/VITE_WORKOS_CLIENT_ID/)
    expect(createClientMock).not.toHaveBeenCalled()
  })

  it('completeLogin returns the access token exchanged from the callback code', async () => {
    createClientMock.mockResolvedValue(makeClient())

    const completing = workos.completeLogin()
    const options = createClientMock.mock.calls[0]?.[1]
    options?.onRedirectCallback?.({ accessToken: 'token-from-exchange', state: null })

    await expect(completing).resolves.toEqual({ accessToken: 'token-from-exchange', returnTo: null })
  })

  it('completeLogin falls back to the SDK access token when no fresh exchange occurred', async () => {
    const client = makeClient()
    client.getAccessToken.mockResolvedValue('token-restored')
    createClientMock.mockResolvedValue(client)

    await expect(workos.completeLogin()).resolves.toEqual({
      accessToken: 'token-restored',
      returnTo: null,
    })
  })

  it('getSession returns the current access token', async () => {
    const client = makeClient()
    client.getAccessToken.mockResolvedValue('token-1')
    createClientMock.mockResolvedValue(client)

    await expect(workos.getSession()).resolves.toBe('token-1')
  })

  it('getSession returns null without a session', async () => {
    const client = makeClient()
    client.getAccessToken.mockRejectedValue(new Error('No access token available'))
    createClientMock.mockResolvedValue(client)

    await expect(workos.getSession()).resolves.toBeNull()
  })

  it('getSession returns null when WorkOS is not configured', async () => {
    vi.stubEnv('VITE_WORKOS_CLIENT_ID', '')
    vi.resetModules()
    workos = await import('@/features/auth/workos')
    createClientMock.mockResolvedValue(makeClient())

    await expect(workos.getSession()).resolves.toBeNull()
    expect(createClientMock).not.toHaveBeenCalled()
  })

  it('signOut uses a top-level WorkOS logout navigation', async () => {
    const client = makeClient()
    createClientMock.mockResolvedValue(client)

    await workos.signOut()

    expect(client.signOut).toHaveBeenCalledWith({
      returnTo: `${window.location.origin}/login`,
    })
  })

  it('signOutForInvalidSession uses a top-level WorkOS logout navigation', async () => {
    const client = makeClient()
    createClientMock.mockResolvedValue(client)

    await expect(workos.signOutForInvalidSession()).resolves.toBe(true)

    expect(client.signOut).toHaveBeenCalledWith()
  })
})

async function listSourceFiles(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true })
  const files: string[] = []
  for (const entry of entries) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      files.push(...(await listSourceFiles(full)))
    } else if (/\.(ts|vue)$/.test(entry.name)) {
      files.push(full)
    }
  }
  return files
}

const SRC_DIR = resolve(process.cwd(), 'src')

describe('WorkOS SDK isolation', () => {
  it('is imported only by the auth adapter', async () => {
    const offenders: string[] = []
    for (const file of await listSourceFiles(SRC_DIR)) {
      if (file.includes('__tests__') || file.endsWith('/api/generated/openapi.d.ts')) continue
      const source = await readFile(file, 'utf8')
      if (source.includes('@workos-inc/authkit-js') && !file.endsWith('/features/auth/workos.ts')) {
        offenders.push(file)
      }
    }
    expect(offenders).toEqual([])
  })

  it('login and callback views never submit identity fields to the backend', async () => {
    for (const name of ['LoginView.vue', 'AuthCallbackView.vue']) {
      const source = await readFile(join(SRC_DIR, 'views', name), 'utf8')
      expect(source).not.toContain('@/api/client')
      expect(source).not.toMatch(/<input/)
    }
  })
})
