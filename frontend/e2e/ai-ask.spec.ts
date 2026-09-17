import { expect, test, type Page } from '@playwright/test'

import { readWorkOsClientId, setupAiJourney } from './helpers'

/**
 * AI scratch/ask browser journey (v0.8 Scope §2.2/§6.4/§6.5, plan P9).
 *
 * The journeys exercise the real Vue shell and the real direct-upload
 * transport in `src/lib/upload.ts` against an external storage origin. A PDF
 * is a non-simple `XMLHttpRequest` body, so the browser sends a CORS preflight
 * first; the mocked storage host answers it with the exact app origin. The
 * positive journey proves a preflight + signed PUT + ask flows end to end, and
 * the negative journey proves the browser blocks the upload when the storage
 * origin does not grant CORS — the control P9 requires.
 */
async function uploadScratchPdf(page: Page): Promise<void> {
  await page.goto('/ai/ask')
  await page.setInputFiles('[data-testid="scratch-file-upload-input"]', {
    name: 'lease.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.from('%PDF-1.7 e2e lease fixture'),
  })
  await page.getByTestId('scratch-file-upload-submit').click()
}

test('uploads a scratch PDF from the storage origin and asks a question', async ({ page }) => {
  const clientId = readWorkOsClientId()
  if (clientId === null) {
    test.skip(true, 'VITE_WORKOS_CLIENT_ID is not configured')
    return
  }

  await setupAiJourney(page, clientId)
  await uploadScratchPdf(page)

  // The preflight and the direct PUT were accepted by the storage origin.
  await expect(page.getByText('Done')).toBeVisible()

  await page.getByTestId('ai-ask-question-input').fill('What is the renewal term?')
  await page.getByTestId('ai-ask-submit').click()
  await expect(page.getByTestId('ai-ask-answer')).toContainText('five years')
})

test('blocks the scratch upload when the storage origin denies CORS', async ({ page }) => {
  const clientId = readWorkOsClientId()
  if (clientId === null) {
    test.skip(true, 'VITE_WORKOS_CLIENT_ID is not configured')
    return
  }

  await setupAiJourney(page, clientId, { storagePath: 'denied' })
  await uploadScratchPdf(page)

  // The browser refused the cross-origin PUT, so the screen surfaces the
  // failure and never reports a completed upload.
  await expect(page.getByTestId('scratch-file-upload-error')).toBeVisible()
  await expect(page.getByText('Done')).not.toBeVisible()
})
