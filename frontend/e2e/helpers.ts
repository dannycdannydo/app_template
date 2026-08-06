import type { Page } from '@playwright/test'

/**
 * Shared session-injection helpers for the Playwright journeys (v0.3 Scope §6.7).
 *
 * The journeys exercise the real frontend shell end-to-end, but the shell's
 * two external dependencies are stubbed at the network boundary the browser
 * controls:
 *
 * - **WorkOS session**: the AuthKit SDK stores a refresh token in
 *   localStorage (dev mode, `workos:refresh-token:<clientId>`) and restores
 *   the session by POSTing to `https://api.workos.com/user_management/
 *   authenticate`. That endpoint is intercepted and answered with a fake
 *   session (an unverifiable but well-formed JWT; the SDK only decodes the
 *   `exp`/`iat` claims, it never validates the signature), so the app boots
 *   authenticated without real WorkOS credentials.
 * - **Backend API**: `/api/v1/**` requests are answered from an in-memory
 *   fixture so the journey is deterministic and needs no running backend.
 *
 * Enforcement is unchanged: this only supplies the session the real backend
 * would validate; the generated client, query layer, router guard and
 * components are the production code under test.
 */

export const TEST_ORG_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
export const TEST_USER_ID = 'user_01_test_profile'

export function makeFakeJwt(payload: Record<string, unknown>): string {
  const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${encode({ alg: 'RS256', typ: 'JWT' })}.${encode(payload)}.mock-signature`
}

function makeAccessToken(): string {
  const now = Math.floor(Date.now() / 1000)
  return makeFakeJwt({ sub: TEST_USER_ID, exp: now + 3600, iat: now, org_id: TEST_ORG_ID })
}

/** The authentication payload the mocked WorkOS token endpoint returns. */
export function makeAuthResponse() {
  return {
    user: {
      object: 'user',
      id: TEST_USER_ID,
      email: 'ada@example.com',
      email_verified: true,
      first_name: 'Ada',
      last_name: 'Lovelace',
      profile_picture_url: null,
      last_sign_in_at: null,
      external_id: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    organization_id: TEST_ORG_ID,
    access_token: makeAccessToken(),
    refresh_token: 'test-refresh-token',
    authentication_method: 'SSO',
  }
}

/**
 * Seed the AuthKit session before any app code runs: a refresh token in
 * localStorage (dev mode key) plus the PKCE verifier the callback flow
 * needs. The SDK then restores the session through the mocked token endpoint
 * on boot (or completes the code exchange on the callback route).
 */
export async function injectSession(page: Page, clientId: string): Promise<void> {
  await page.addInitScript(
    ({ cid, verifier }) => {
      localStorage.setItem(`workos:refresh-token:${cid}`, 'test-refresh-token')
      sessionStorage.setItem('workos:code-verifier', verifier)
    },
    { cid: clientId, verifier: 'test-code-verifier' },
  )
}

/**
 * Intercept the WorkOS token endpoint so the SDK's refresh (and callback
 * code-exchange) round-trips succeed without real credentials. The endpoint
 * is cross-origin, so the fulfilled responses carry explicit CORS headers
 * and preflight (OPTIONS) requests are answered: without them the browser
 * would reject the exchange before the mock is ever consumed.
 */
export async function mockWorkOsTokenEndpoint(page: Page): Promise<void> {
  const corsHeaders = {
    'access-control-allow-origin': '*',
    'access-control-allow-methods': 'GET,POST,OPTIONS',
    'access-control-allow-headers': 'content-type,authorization',
  }

  // One catch-all for the WorkOS host; the handler dispatches on the path.
  // (Playwright glob matching is unreliable for literal full URLs, so the
  // dispatch happens in code rather than in the route pattern.)
  await page.route('https://api.workos.com/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname !== '/user_management/authenticate') {
      await route.abort()
      return
    }
    if (route.request().method() === 'OPTIONS') {
      await route.fulfill({ status: 204, headers: corsHeaders })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      headers: corsHeaders,
      body: JSON.stringify(makeAuthResponse()),
    })
  })
}

/**
 * In-memory records fixture served over the mocked `/api/v1/**` surface.
 * Mutations mutate the array, so create → list → edit → delete flow data the
 * way the real backend would.
 */
export function createRecordFixture() {
  const records: Array<{
    id: string
    title: string
    body: string
    created_at: string
    updated_at: string
  }> = [
    {
      id: '11111111-1111-4111-8111-111111111111',
      title: 'Welcome note',
      body: 'The first record in this organisation.',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-02T00:00:00Z',
    },
  ]

  return {
    records,
    nextId: () => `22222222-2222-4222-8222-222222222222`,
  }
}

/**
 * Mock the backend `/api/v1/**` surface consumed by the shell (blueprint
 * §5, §15): `me`, and the records list/detail/create/update/delete routes
 * with the standard pagination envelope (blueprint §12). Captured request
 * headers (the Bearer token and `X-Org-Id`) are recorded for assertions.
 */
export async function mockBackendApi(
  page: Page,
  fixture: ReturnType<typeof createRecordFixture>,
): Promise<{ capturedHeaders: Array<{ authorization: string | null; orgId: string | null }> }> {
  const capturedHeaders: Array<{ authorization: string | null; orgId: string | null }> = []

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const orgId = request.headers()['x-org-id'] ?? null
    const authorization = request.headers()['authorization'] ?? null

    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (method === 'GET' && url.pathname === '/api/v1/me') {
      capturedHeaders.push({ authorization, orgId })
      return json({
        user: {
          id: TEST_USER_ID,
          email: 'ada@example.com',
          name: 'Ada Lovelace',
          is_active: true,
          created_at: '2026-01-01T00:00:00Z',
        },
        memberships: [
          {
            id: 'm1',
            organisation_id: TEST_ORG_ID,
            user_id: TEST_USER_ID,
            status: 'active',
            created_at: '2026-01-01T00:00:00Z',
          },
        ],
        roles: ['owner'],
      })
    }

    if (method === 'GET' && url.pathname === '/api/v1/records') {
      capturedHeaders.push({ authorization, orgId })
      const pageNumber = Number(url.searchParams.get('page') ?? '1')
      const pageSize = Number(url.searchParams.get('page_size') ?? '25')
      const start = (pageNumber - 1) * pageSize
      const items = fixture.records
        .slice(start, start + pageSize)
        .map(({ body: _body, ...item }) => item)
      return json({ items, page: pageNumber, page_size: pageSize, total: fixture.records.length })
    }

    const recordMatch = url.pathname.match(/^\/api\/v1\/records\/([^/]+)$/)
    if (recordMatch) {
      const recordId = recordMatch[1]
      const record = fixture.records.find((entry) => entry.id === recordId)
      if (method === 'GET' && record) {
        capturedHeaders.push({ authorization, orgId })
        return json(record)
      }
      if (method === 'PATCH' && record) {
        capturedHeaders.push({ authorization, orgId })
        const body = request.postDataJSON()
        Object.assign(record, {
          title: body.title ?? record.title,
          body: body.body ?? record.body,
          updated_at: '2026-03-01T00:00:00Z',
        })
        return json(record)
      }
      if (method === 'DELETE' && record) {
        capturedHeaders.push({ authorization, orgId })
        fixture.records.splice(fixture.records.indexOf(record), 1)
        return route.fulfill({ status: 204 })
      }
      return json({ code: 'not_found', message: 'Record not found', request_id: 'mock-404' }, 404)
    }

    if (method === 'POST' && url.pathname === '/api/v1/records') {
      capturedHeaders.push({ authorization, orgId })
      const body = request.postDataJSON()
      const created = {
        id: fixture.nextId(),
        title: body.title,
        body: body.body ?? '',
        created_at: '2026-04-01T00:00:00Z',
        updated_at: '2026-04-01T00:00:00Z',
      }
      fixture.records.push(created)
      return json(created, 201)
    }

    return json({ code: 'not_found', message: 'Not found', request_id: 'mock-404' }, 404)
  })

  return { capturedHeaders }
}

/**
 * Full journey setup: injected session + mocked WorkOS token endpoint +
 * mocked backend API. Returns the captured request headers so tests can
 * assert the Bearer token and `X-Org-Id` were attached (v0.3 Scope §6.3).
 */
export async function setupAuthenticatedJourney(page: Page, clientId: string) {
  const fixture = createRecordFixture()
  await injectSession(page, clientId)
  await mockWorkOsTokenEndpoint(page)
  const api = await mockBackendApi(page, fixture)
  return { fixture, capturedHeaders: api.capturedHeaders }
}

/**
 * Read the WorkOS client id the Vite dev server runs with. The repo-root
 * `.env` is loaded by `playwright.config.ts` before any spec runs, so the
 * value (if configured) is already in the process environment; this accessor
 * exists so tests can decide whether the authenticated journeys can run and
 * keep the value in one place. Returns null when unconfigured.
 */
export function readWorkOsClientId(): string | null {
  return process.env.VITE_WORKOS_CLIENT_ID ?? null
}
