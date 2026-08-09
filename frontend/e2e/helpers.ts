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
 * In-memory platform administration fixture (Scope §6.9) served over the
 * mocked `/api/v1/platform/**` surface. Mutations mutate the arrays, so the
 * platform-admin journey flows data the way the real backend would: inviting
 * writes an invitation and (mirroring login-time linking, Scope §6.5) creates
 * the membership the invitee would gain on acceptance.
 */
export function createPlatformFixture() {
  const organisations = [
    {
      id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      name: 'Acme Ltd',
      workos_organisation_id: 'org_workos_acme',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
  ]

  const memberships = [
    {
      id: 'm1',
      organisation_id: organisations[0].id,
      user_id: TEST_USER_ID,
      user_name: 'Ada Lovelace',
      user_email: 'ada@example.com',
      status: 'active',
      roles: ['owner'],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
  ]

  const invitations: Array<Record<string, string>> = []

  const featureFlags = [
    {
      feature_key: 'records.deletion',
      name: 'Record deletion',
      description: 'Allow owners and administrators to delete records.',
      default_enabled: false,
      enabled: false,
      overridden: false,
      configuration_json: null,
    },
  ]

  const auditEvents = [
    {
      id: 'a1',
      organisation_id: organisations[0].id,
      actor_user_id: TEST_USER_ID,
      action: 'organisation.created',
      resource_type: 'organisation',
      resource_id: organisations[0].id,
      metadata: {},
      created_at: '2026-01-01T00:00:00Z',
    },
  ]

  const envelope = <T>(items: T[], page: number, pageSize: number) => ({
    items,
    page,
    page_size: pageSize,
    total: items.length,
  })

  return {
    organisations,
    memberships,
    invitations,
    featureFlags,
    auditEvents,
    envelope,
    inviteUser: (email: string, roleCode: string) => {
      const invitation = {
        id: 'inv_playwright',
        organisation_id: organisations[0].id,
        email,
        role_code: roleCode,
        workos_invitation_id: 'inv_workos_playwright',
        invited_by_user_id: TEST_USER_ID,
        status: 'sent',
        expires_at: '2027-01-01T00:00:00Z',
        created_at: '2026-02-01T00:00:00Z',
        updated_at: '2026-02-01T00:00:00Z',
      }
      invitations.push(invitation)
      // The invited user accepted at next login (login-time linking,
      // Scope §6.5): they now appear in the memberships.
      memberships.push({
        id: 'm_invitee',
        organisation_id: organisations[0].id,
        user_id: 'user_invitee',
        user_name: '',
        user_email: email,
        status: 'active',
        roles: [roleCode],
        created_at: '2026-02-01T00:00:00Z',
        updated_at: '2026-02-01T00:00:00Z',
      })
      auditEvents.push({
        id: 'a_invitee',
        organisation_id: organisations[0].id,
        actor_user_id: TEST_USER_ID,
        action: 'invitation.sent',
        resource_type: 'invitation',
        resource_id: invitation.id,
        metadata: { email },
        created_at: '2026-02-01T00:00:00Z',
      })
      return invitation
    },
  }
}

/**
 * In-memory files/jobs fixture (Scope §6.6) served over the mocked
 * `/api/v1/**` surface. The fixture is stateful the way the real backend is:
 * the upload intent creates a `pending` file record, completion moves it to
 * `processing` and hands back a job id, and the first job poll returns a
 * running job while subsequent polls succeed and flip the file to `ready`.
 * The signed-URL PUT is not part of this fixture — the journey intercepts the
 * storage host separately (see `e2e/files.spec.ts`).
 */
export function createFileFixture() {
  const files: Array<{
    id: string
    original_filename: string
    content_type: string
    size_bytes: number
    status: string
    created_by_user_id: string
    created_at: string
    checksum: string | null
    updated_at: string
  }> = [
    {
      id: 'aaaaaaaa-aaaa-4aaa-8aaa-111111111111',
      original_filename: 'welcome.pdf',
      content_type: 'application/pdf',
      size_bytes: 4096,
      status: 'ready',
      created_by_user_id: TEST_USER_ID,
      created_at: '2026-01-01T00:00:00Z',
      checksum: null,
      updated_at: '2026-01-01T00:00:00Z',
    },
  ]

  let jobPollCount = 0
  let activeFileId: string | null = null

  const nextFileId = () => 'aaaaaaaa-aaaa-4aaa-8aaa-222222222222'
  const nextJobId = () => 'aaaaaaaa-aaaa-4aaa-8aaa-333333333333'

  return {
    files,
    nextFileId,
    nextJobId,
    /**
     * Job payload for `GET /api/v1/jobs/{job_id}`: running first, then
     * succeeded. The client polls by job id, but the file the job belongs to
     * is tracked via `completeFor` (the job URL id never equals the file id),
     * so the fixture flips that file to `ready` once the poll succeeds.
     */
    jobFor() {
      jobPollCount += 1
      const succeeded = jobPollCount > 1
      const status = succeeded ? 'succeeded' : 'running'
      const progress = succeeded ? 100 : 45
      if (succeeded && activeFileId !== null) {
        const file = files.find((entry) => entry.id === activeFileId)
        if (file) {
          file.status = 'ready'
          file.updated_at = '2026-05-01T00:00:00Z'
        }
      }
      return {
        id: nextJobId(),
        job_type: 'file.processing',
        status,
        progress,
        attempt_count: 1,
        created_by_user_id: TEST_USER_ID,
        created_at: '2026-04-01T00:00:00Z',
        started_at: '2026-04-01T00:00:01Z',
        completed_at: succeeded ? '2026-04-01T00:00:02Z' : null,
        input_reference: activeFileId !== null ? `file:${activeFileId}` : '',
        result_reference: null,
        error_code: null,
        error_message: null,
      }
    },
    /** Completion payload: the file record plus the job the client polls. */
    completeFor(fileId: string) {
      const file = files.find((entry) => entry.id === fileId)
      if (!file) return undefined
      file.status = 'processing'
      file.updated_at = '2026-04-01T00:00:00Z'
      activeFileId = file.id
      return {
        id: file.id,
        original_filename: file.original_filename,
        content_type: file.content_type,
        size_bytes: file.size_bytes,
        status: file.status,
        created_by_user_id: file.created_by_user_id,
        created_at: file.created_at,
        checksum: file.checksum,
        updated_at: file.updated_at,
        processing_job_id: nextJobId(),
      }
    },
  }
}

/**
 * In-memory notifications fixture (Scope §6.5) served over the mocked
 * `/api/v1/**` surface. The fixture is stateful the way the real backend is:
 * marking a notification read flips its `read_at` and drops the unread count,
 * and sending a test notification appends a fresh unread row.
 */
export function createNotificationsFixture() {
  const notifications: Array<{
    id: string
    type: string
    title: string
    body: string
    resource_type: string | null
    resource_id: string | null
    read_at: string | null
    created_at: string
  }> = [
    {
      id: 'aaaaaaaa-aaaa-4aaa-8aaa-444444444444',
      type: 'file.ready',
      title: 'File ready',
      body: 'Your file welcome.pdf is ready.',
      resource_type: 'file',
      resource_id: 'aaaaaaaa-aaaa-4aaa-8aaa-111111111111',
      read_at: null,
      created_at: '2026-01-01T00:00:00Z',
    },
    {
      id: 'aaaaaaaa-aaaa-4aaa-8aaa-555555555555',
      type: 'notification.test_sent',
      title: 'Test notification',
      body: 'This is a test notification.',
      resource_type: 'notification',
      resource_id: null,
      read_at: '2026-01-02T00:00:00Z',
      created_at: '2026-01-02T00:00:00Z',
    },
  ]

  const nextId = () => 'aaaaaaaa-aaaa-4aaa-8aaa-666666666666'

  const unreadCount = () => notifications.filter((entry) => entry.read_at === null).length

  const envelope = (page: number, pageSize: number) => ({
    items: notifications,
    page,
    page_size: pageSize,
    total: notifications.length,
    unread_count: unreadCount(),
  })

  return {
    notifications,
    envelope,
    unreadCount,
    markRead: (id: string) => {
      const notification = notifications.find((entry) => entry.id === id)
      if (!notification) return undefined
      notification.read_at = '2026-06-01T00:00:00Z'
      return notification
    },
    sendTest: () => {
      const notification = {
        id: nextId(),
        type: 'notification.test_sent',
        title: 'Test notification',
        body: 'This is a test notification.',
        resource_type: 'notification',
        resource_id: null,
        read_at: null,
        created_at: '2026-06-01T00:00:00Z',
      }
      notifications.unshift(notification)
      return notification
    },
  }
}

/**
 * Mock the backend `/api/v1/**` surface consumed by the shell (blueprint
 * §5, §15): `me`, and the records list/detail/create/update/delete routes
 * with the standard pagination envelope (blueprint §12). Captured request
 * headers (the Bearer token and `X-Org-Id`) are recorded for assertions.
 *
 * With `options.platformAdmin` the `/me` payload reports `platform_roles` and
 * the `/api/v1/platform/**` surface (Scope §6.9) is answered from the
 * platform fixture — organisations, memberships, invitations, feature flags
 * and audit events — so the platform-admin journey flows data end to end.
 *
 * With `options.files` the files/jobs surface (Scope §6.6) is answered from
 * the files fixture — list/detail/delete/download-url, the upload intent and
 * completion steps, and the job-poll endpoint — so the files journey flows
 * the direct-upload lifecycle end to end.
 *
 * With `options.notifications` the notifications surface (Scope §6.5) is
 * answered from the notifications fixture — list, unread-count, mark-read and
 * test-send — so the notifications journey flows the list-and-mark-read flow
 * end to end.
 */
export async function mockBackendApi(
  page: Page,
  fixture: ReturnType<typeof createRecordFixture>,
  options: {
    platformAdmin?: boolean
    platformFixture?: ReturnType<typeof createPlatformFixture>
    files?: ReturnType<typeof createFileFixture>
    notifications?: ReturnType<typeof createNotificationsFixture>
  } = {},
): Promise<{ capturedHeaders: Array<{ authorization: string | null; orgId: string | null }> }> {
  const capturedHeaders: Array<{ authorization: string | null; orgId: string | null }> = []
  const platform = options.platformFixture
  const notifications = options.notifications

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
        platform_roles: options.platformAdmin ? ['platform_admin'] : [],
      })
    }

    // Platform Admin Centre (Scope §6.9): answered only for the platform
    // journey; the real backend would 403 without platform membership.
    if (url.pathname.startsWith('/api/v1/platform/') && platform) {
      capturedHeaders.push({ authorization, orgId })
      const orgMatch = url.pathname.match(
        /^\/api\/v1\/platform\/organisations\/([^/]+)\/(invitations|memberships)$/,
      )
      if (method === 'GET' && url.pathname === '/api/v1/platform/organisations') {
        const pageNumber = Number(url.searchParams.get('page') ?? '1')
        const pageSize = Number(url.searchParams.get('page_size') ?? '25')
        return json(platform.envelope(platform.organisations, pageNumber, pageSize))
      }
      const detailMatch = url.pathname.match(/^\/api\/v1\/platform\/organisations\/([^/]+)$/)
      if (method === 'GET' && detailMatch) {
        const organisation = platform.organisations.find((entry) => entry.id === detailMatch[1])
        return json(organisation ?? { code: 'not_found' }, organisation ? 200 : 404)
      }
      if (method === 'PATCH' && detailMatch) {
        const body = request.postDataJSON()
        const organisation = platform.organisations.find((entry) => entry.id === detailMatch[1])
        if (organisation) {
          organisation.name = body.name
          organisation.updated_at = '2026-03-01T00:00:00Z'
        }
        return json(organisation ?? { code: 'not_found' }, organisation ? 200 : 404)
      }
      if (method === 'POST' && orgMatch && orgMatch[2] === 'invitations') {
        const body = request.postDataJSON()
        return json(platform.inviteUser(body.email, body.role_code), 201)
      }
      if (method === 'GET' && orgMatch) {
        const pageNumber = Number(url.searchParams.get('page') ?? '1')
        const pageSize = Number(url.searchParams.get('page_size') ?? '25')
        const source = orgMatch[2] === 'memberships' ? platform.memberships : platform.invitations
        return json(platform.envelope(source, pageNumber, pageSize))
      }
      if (method === 'GET' && url.pathname === '/api/v1/platform/feature-flags') {
        const organisationId = url.searchParams.get('organisation_id')
        const items = organisationId
          ? platform.featureFlags.map((flag) => ({ ...flag, overridden: true, enabled: true }))
          : platform.featureFlags
        return json({ items })
      }
      if (method === 'PUT' && url.pathname.startsWith('/api/v1/platform/feature-flags/')) {
        const body = request.postDataJSON()
        const key = url.pathname.split('/').at(-1)
        const flag = platform.featureFlags.find((entry) => entry.feature_key === key)
        if (flag) {
          flag.enabled = body.enabled
          flag.overridden = true
        }
        return json(flag ?? { code: 'not_found' }, flag ? 200 : 404)
      }
      if (method === 'GET' && url.pathname === '/api/v1/platform/audit-events') {
        const pageNumber = Number(url.searchParams.get('page') ?? '1')
        const pageSize = Number(url.searchParams.get('page_size') ?? '25')
        const action = url.searchParams.get('action')
        const items = action
          ? platform.auditEvents.filter((entry) => entry.action === action)
          : platform.auditEvents
        return json(platform.envelope(items, pageNumber, pageSize))
      }
      return json({ code: 'not_found', message: 'Not found', request_id: 'mock-404' }, 404)
    }

    // Files and jobs surface (Scope §6.6): answered only for the files
    // journey. Mirrors the real backend lifecycle — intent creates a pending
    // record, completion verifies and moves it to processing, and the job
    // poll eventually flips it to ready.
    const files = options.files
    if (files) {
      const filesMatch = url.pathname.match(/^\/api\/v1\/files\/([^/]+)$/)
      const filesCompleteMatch = url.pathname.match(/^\/api\/v1\/files\/([^/]+)\/complete$/)
      const filesDownloadMatch = url.pathname.match(/^\/api\/v1\/files\/([^/]+)\/download-url$/)
      const jobMatch = url.pathname.match(/^\/api\/v1\/jobs\/([^/]+)$/)

      if (method === 'GET' && url.pathname === '/api/v1/files') {
        capturedHeaders.push({ authorization, orgId })
        const pageNumber = Number(url.searchParams.get('page') ?? '1')
        const pageSize = Number(url.searchParams.get('page_size') ?? '25')
        return json({
          items: files.files,
          page: pageNumber,
          page_size: pageSize,
          total: files.files.length,
        })
      }

      if (method === 'POST' && url.pathname === '/api/v1/files') {
        capturedHeaders.push({ authorization, orgId })
        const body = request.postDataJSON()
        const file = {
          id: files.nextFileId(),
          original_filename: body.original_filename,
          content_type: body.content_type,
          size_bytes: body.size_bytes,
          status: 'pending',
          created_by_user_id: TEST_USER_ID,
          created_at: '2026-04-01T00:00:00Z',
          checksum: null,
          updated_at: '2026-04-01T00:00:00Z',
        }
        files.files.push(file)
        return json(
          {
            file_id: file.id,
            upload_url: 'https://storage.example.com/upload',
            expires_at: '2026-04-01T00:10:00Z',
          },
          201,
        )
      }

      if (method === 'POST' && filesCompleteMatch) {
        capturedHeaders.push({ authorization, orgId })
        const completed = files.completeFor(filesCompleteMatch[1])
        return completed
          ? json(completed)
          : json({ code: 'not_found', message: 'File not found', request_id: 'mock-404' }, 404)
      }

      if (method === 'GET' && filesMatch) {
        capturedHeaders.push({ authorization, orgId })
        const file = files.files.find((entry) => entry.id === filesMatch[1])
        return file
          ? json(file)
          : json({ code: 'not_found', message: 'File not found', request_id: 'mock-404' }, 404)
      }

      if (method === 'GET' && filesDownloadMatch) {
        capturedHeaders.push({ authorization, orgId })
        const file = files.files.find((entry) => entry.id === filesDownloadMatch[1])
        return file
          ? json({
              download_url: 'https://storage.example.com/download?X-Amz-Signature=mock',
              expires_at: '2026-04-01T00:10:00Z',
            })
          : json({ code: 'not_found', message: 'File not found', request_id: 'mock-404' }, 404)
      }

      if (method === 'DELETE' && filesMatch) {
        capturedHeaders.push({ authorization, orgId })
        const index = files.files.findIndex((entry) => entry.id === filesMatch[1])
        if (index === -1) {
          return json({ code: 'not_found', message: 'File not found', request_id: 'mock-404' }, 404)
        }
        files.files.splice(index, 1)
        return route.fulfill({ status: 204 })
      }

      if (method === 'GET' && jobMatch) {
        capturedHeaders.push({ authorization, orgId })
        return json(files.jobFor())
      }
    }

    // Notifications surface (Scope §6.5): answered only for the
    // notifications journey. Mirrors the real backend lifecycle — marking a
    // notification read flips its read_at, and test-send appends a new
    // unread row.
    if (notifications) {
      const readMatch = url.pathname.match(/^\/api\/v1\/notifications\/([^/]+)\/read$/)

      if (method === 'GET' && url.pathname === '/api/v1/notifications') {
        capturedHeaders.push({ authorization, orgId })
        const pageNumber = Number(url.searchParams.get('page') ?? '1')
        const pageSize = Number(url.searchParams.get('page_size') ?? '25')
        return json(notifications.envelope(pageNumber, pageSize))
      }

      if (method === 'GET' && url.pathname === '/api/v1/notifications/unread-count') {
        capturedHeaders.push({ authorization, orgId })
        return json({ unread_count: notifications.unreadCount() })
      }

      if (method === 'PATCH' && readMatch) {
        capturedHeaders.push({ authorization, orgId })
        const notification = notifications.markRead(readMatch[1])
        return notification
          ? json(notification)
          : json(
              { code: 'not_found', message: 'Notification not found', request_id: 'mock-404' },
              404,
            )
      }

      if (method === 'POST' && url.pathname === '/api/v1/notifications/test') {
        capturedHeaders.push({ authorization, orgId })
        return json(notifications.sendTest(), 201)
      }
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
 * Platform-admin journey setup (Scope §6.9): the authenticated shell plus the
 * `/api/v1/platform/**` surface, with `/me` reporting `platform_roles` so the
 * Platform Admin Centre renders.
 */
export async function setupPlatformAdminJourney(page: Page, clientId: string) {
  const fixture = createRecordFixture()
  const platformFixture = createPlatformFixture()
  await injectSession(page, clientId)
  await mockWorkOsTokenEndpoint(page)
  const api = await mockBackendApi(page, fixture, {
    platformAdmin: true,
    platformFixture,
  })
  return { fixture, platformFixture, capturedHeaders: api.capturedHeaders }
}

/**
 * Files journey setup (Scope §6.6): the authenticated shell plus the
 * files/jobs surface, including an intercepted storage host for the direct
 * PUT. The upload URL points at `https://storage.example.com`, which is not
 * under `/api/v1`, so this helper also fulfils that PUT with CORS headers —
 * the browser XHR enforces CORS exactly as it would against real MinIO.
 */
export async function setupFilesJourney(page: Page, clientId: string) {
  const fixture = createRecordFixture()
  const filesFixture = createFileFixture()
  await injectSession(page, clientId)
  await mockWorkOsTokenEndpoint(page)
  const api = await mockBackendApi(page, fixture, { files: filesFixture })

  // The direct upload: the browser PUTs the file bytes to the signed URL on
  // the storage host. Playwright fulfils the request with CORS headers so
  // the XHR completes, and with the same content-type the browser sent.
  await page.route('https://storage.example.com/**', async (route) => {
    const request = route.request()
    if (request.method() === 'PUT') {
      await route.fulfill({
        status: 200,
        headers: { 'access-control-allow-origin': '*' },
      })
      return
    }
    await route.fulfill({
      status: 404,
      headers: { 'access-control-allow-origin': '*' },
    })
  })

  return { fixture, filesFixture, capturedHeaders: api.capturedHeaders }
}

/**
 * Notifications journey setup (Scope §6.5): the authenticated shell plus the
 * notifications surface, so the bell badge and the `/notifications` view flow
 * the list-and-mark-read flow end to end.
 */
export async function setupNotificationsJourney(page: Page, clientId: string) {
  const fixture = createRecordFixture()
  const notificationsFixture = createNotificationsFixture()
  await injectSession(page, clientId)
  await mockWorkOsTokenEndpoint(page)
  const api = await mockBackendApi(page, fixture, { notifications: notificationsFixture })
  return { fixture, notificationsFixture, capturedHeaders: api.capturedHeaders }
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
