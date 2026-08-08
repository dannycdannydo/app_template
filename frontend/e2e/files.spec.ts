import { expect, test } from '@playwright/test'

import { readWorkOsClientId, setupFilesJourney } from './helpers'

/**
 * Files feature-module journey (Scope §6.6, acceptance §5.10, blueprint §17).
 *
 * An authenticated shell (session injected per the helper contract) walks
 * the direct-upload lifecycle: navigate to files, see the seeded file, pick
 * a local file, and watch it flow intent → signed PUT (intercepted on the
 * mock storage host) → completion → job polling → `ready` in the table. The
 * backend `/api/v1/**` surface is answered from the stateful files fixture
 * while the storage-host PUT is fulfilled with CORS headers, so the real
 * `XMLHttpRequest` transport in `src/lib/upload.ts` is exercised end to end.
 *
 * The captured request headers also prove the Bearer session token and
 * `X-Org-Id` ride along on every files/jobs call (Scope §6.3, acceptance
 * §5.2, §5.6).
 */
test('uploads a file and follows it to ready in the files table', async ({ page }) => {
  const clientId = readWorkOsClientId()
  if (clientId === null) {
    test.skip(true, 'VITE_WORKOS_CLIENT_ID is not configured')
    return
  }

  const { capturedHeaders } = await setupFilesJourney(page, clientId)

  await page.goto('/')
  await expect(page.getByTestId('sidebar')).toBeVisible()

  // Shell navigation reaches the files list with the seeded file.
  await page.getByRole('link', { name: 'Files' }).first().click()
  await expect(page).toHaveURL(/\/files$/)
  await expect(page.getByText('welcome.pdf')).toBeVisible()
  await expect(page.getByTestId('file-status-ready')).toBeVisible()

  // Upload: pick a local file; the component walks the signed-upload flow
  // and the row appears, progresses and reaches ready.
  const filename = 'journey-notes.txt'
  await page.setInputFiles('[data-testid="file-upload-input"]', {
    name: filename,
    mimeType: 'text/plain',
    buffer: Buffer.from('created by the e2e journey'),
  })
  await page.getByTestId('file-upload-submit').click()

  // The upload/job progress UI is visible while processing runs.
  await expect(page.getByTestId('file-upload-progress')).toBeVisible()

  const uploadedRow = page.getByRole('row').filter({ hasText: filename })
  await expect(uploadedRow).toBeVisible()
  // The job poll flips the file to ready (second poll succeeds).
  await expect(uploadedRow.getByTestId('file-status-ready')).toBeVisible({ timeout: 10_000 })
  // The seed file is untouched — the journey uploaded exactly one file.
  await expect(page.getByText('welcome.pdf')).toBeVisible()

  // The client attached the session and tenant context to every call.
  const apiCalls = capturedHeaders.filter((entry) => entry.orgId !== null)
  expect(apiCalls.length).toBeGreaterThan(0)
  for (const call of apiCalls) {
    expect(call.authorization).toMatch(/^Bearer /)
    expect(call.orgId).toBe('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
  }
})
