import { expect, test } from '@playwright/test'

import {
  createRecordFixture,
  mockBackendApi,
  mockWorkOsTokenEndpoint,
  readWorkOsClientId,
} from './helpers'

/**
 * Authentication journeys (v0.3 Scope §6.7, acceptance §5.4).
 *
 * 1. An unauthenticated visit to any protected route lands on `/login`.
 * 2. The successful callback round-trip is covered explicitly (per v0.3 Scope §6.2
 *    review feedback on the boot-restore × history-snapshot coupling):
 *    `/auth/callback?code=…` → code exchange → session stored → redirect
 *    to the shell. The WorkOS token endpoint is stubbed at the network
 *    boundary, so the exchange succeeds deterministically; the shell's
 *    backend reads are mocked the same way.
 */

test.describe('authentication journeys', () => {
  test('an unauthenticated visit to a protected route redirects to login', async ({ page }) => {
    await page.goto('/')
    await expect(page).toHaveURL(/\/login/)
  })

  test('an unauthenticated visit to the records route redirects to login', async ({ page }) => {
    await page.goto('/records')
    await expect(page).toHaveURL(/\/login/)
  })

  test('visiting /login while authenticated redirects to the shell', async ({ page }) => {
    const clientId = readWorkOsClientId()
    if (clientId === null) {
      test.skip(true, 'VITE_WORKOS_CLIENT_ID is not configured')
      return
    }

    await mockWorkOsTokenEndpoint(page)
    await mockBackendApi(page, createRecordFixture())
    await page.addInitScript((cid) => {
      localStorage.setItem(`workos:refresh-token:${cid}`, 'test-refresh-token')
    }, clientId)

    await page.goto('/login')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByTestId('sidebar')).toBeVisible()
  })

  test('the callback round-trip stores the session and redirects to the shell', async ({
    page,
  }) => {
    const clientId = readWorkOsClientId()
    if (clientId === null) {
      test.skip(true, 'VITE_WORKOS_CLIENT_ID is not configured')
      return
    }

    await page.addInitScript((verifier) => {
      // The callback flow needs the PKCE verifier the SDK stashed when it
      // built the authorization URL; in a mocked journey we seed it.
      sessionStorage.setItem('workos:code-verifier', verifier)
    }, 'test-code-verifier')
    await mockWorkOsTokenEndpoint(page)
    await mockBackendApi(page, createRecordFixture())

    await page.goto('/auth/callback?code=test-authorization-code')

    // Session stored (dev mode: refresh token in localStorage) and the user
    // lands in the protected shell, not back on /login.
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByTestId('sidebar')).toBeVisible()
    const hasRefreshToken = await page.evaluate(() => {
      const keys = Object.keys(localStorage)
      return keys.some((key) => key.startsWith('workos:refresh-token') && localStorage.getItem(key))
    })
    expect(hasRefreshToken).toBe(true)
  })
})
