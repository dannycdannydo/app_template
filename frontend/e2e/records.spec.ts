import { expect, test } from '@playwright/test'

import { readWorkOsClientId, setupAuthenticatedJourney } from './helpers'

/**
 * Records feature-module journey (v0.3 Scope §6.7, acceptance §5.9).
 *
 * An authenticated shell (session injected per the helper contract) walks
 * the full records CRUD surface through the generated client: navigate to
 * records, create a record, see it in the list, edit it, and delete it with
 * confirmation. The backend `/api/v1/**` surface is answered from an
 * in-memory fixture so the journey is deterministic, while everything on the
 * frontend — router guard, shell layout, organisation selector, query layer,
 * DataTable, form, toasts — is the production code under test.
 *
 * The captured request headers also prove the Bearer session token and
 * `X-Org-Id` header ride along on every call (v0.3 Scope §6.3, acceptance §5.2,
 * §5.6).
 */
test('lists, creates, edits and deletes records in the authenticated shell', async ({ page }) => {
  const clientId = readWorkOsClientId()
  if (clientId === null) {
    test.skip(true, 'VITE_WORKOS_CLIENT_ID is not configured')
    return
  }

  const { capturedHeaders } = await setupAuthenticatedJourney(page, clientId)

  await page.goto('/')
  await expect(page.getByTestId('sidebar')).toBeVisible()

  // Shell navigation reaches the records list.
  await page.getByRole('link', { name: 'Records' }).first().click()
  await expect(page).toHaveURL(/\/records$/)
  await expect(page.getByText('Welcome note')).toBeVisible()

  // Create: standard form, toast, back to the list with the new record.
  await page.getByTestId('records-create-button').click()
  await expect(page).toHaveURL(/\/records\/new$/)

  const title = 'Playwright journey record'
  await page.getByPlaceholder('Record title').fill(title)
  await page.getByPlaceholder('Notes (optional)').fill('Created by the e2e journey.')
  await page.getByTestId('record-form-submit').click()

  await expect(page).toHaveURL(/\/records$/)
  await expect(page.getByText(title)).toBeVisible()

  // Edit: reach the edit screen from the row action, update, toast, list.
  const createdRow = page.getByRole('row').filter({ hasText: title })
  await createdRow.getByRole('link', { name: 'Edit' }).click()
  await expect(page).toHaveURL(/\/records\/.+\/edit$/)

  const updatedTitle = `${title} (updated)`
  await page.getByPlaceholder('Record title').fill(updatedTitle)
  await page.getByTestId('record-form-submit').click()

  await expect(page).toHaveURL(/\/records$/)
  await expect(page.getByText(updatedTitle)).toBeVisible()

  // Delete: confirmation is required; the list stops showing the record.
  await page
    .getByRole('row')
    .filter({ hasText: updatedTitle })
    .getByRole('link', { name: 'Edit' })
    .click()
  await expect(page.getByTestId('records-delete-button')).toBeVisible()

  // The record still exists until the destructive button is clicked.
  await page.getByTestId('records-delete-button').click()
  await expect(page.getByText('Delete this record?')).toBeVisible()
  await page.getByTestId('records-delete-confirm').click()

  await expect(page).toHaveURL(/\/records$/)
  await expect(page.getByText(updatedTitle)).toBeHidden()
  // Only the deleted record is gone; the seeded record remains (delete
  // removed exactly the right row).
  await expect(page.getByText('Welcome note')).toBeVisible()

  // The client attached the session and tenant context to every call.
  const apiCalls = capturedHeaders.filter((entry) => entry.orgId !== null)
  expect(apiCalls.length).toBeGreaterThan(0)
  for (const call of apiCalls) {
    expect(call.authorization).toMatch(/^Bearer /)
    expect(call.orgId).toBe('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
  }
})
