import { expect, test } from '@playwright/test'

import { readWorkOsClientId, setupPlatformAdminJourney } from './helpers'

/**
 * Platform Admin Centre journey (Scope §6.9, acceptance §5.10).
 *
 * The platform-admin session (test profile with `platform_roles`) walks the
 * invite flow end to end through the production code under test: the router
 * guard admits `/platform`, the nav exposes the centre, the organisations
 * list and detail render from the generated client, and the invite form
 * sends email + role. The mocked backend answers the platform surface from an
 * in-memory fixture (helpers.ts), where accepting an invitation at login
 * (login-time linking, Scope §6.5) surfaces the invitee in the memberships —
 * the journey proves both round trips.
 */
test('platform admin invites a user who then appears in memberships', async ({ page }) => {
  const clientId = readWorkOsClientId()
  if (clientId === null) {
    test.skip(true, 'VITE_WORKOS_CLIENT_ID is not configured')
    return
  }

  const { platformFixture, capturedHeaders } = await setupPlatformAdminJourney(page, clientId)

  // The guard admits the centre and the nav shows the entry for the
  // platform-admin profile.
  await page.goto('/')
  await expect(page.getByTestId('sidebar')).toBeVisible()
  await expect(page.getByRole('link', { name: 'Platform Admin' })).toBeVisible()

  await page.getByRole('link', { name: 'Platform Admin' }).first().click()
  await expect(page).toHaveURL(/\/platform$/)
  await expect(page.getByText('Platform Admin Centre')).toBeVisible()

  // Dashboard surfaces the organisation count from the platform list.
  await expect(page.getByTestId('platform-dashboard-organisations')).toContainText('1')

  // The organisations list renders from the platform query layer.
  await page.getByRole('link', { name: 'Organisations' }).first().click()
  await expect(page).toHaveURL(/\/platform\/organisations$/)
  await expect(page.getByText('Acme Ltd')).toBeVisible()

  // Detail: memberships table shows the platform admin themselves.
  await page
    .getByRole('row')
    .filter({ hasText: 'Acme Ltd' })
    .getByRole('link', { name: 'View' })
    .click()
  await expect(page).toHaveURL(/\/platform\/organisations\/.+/)
  await expect(page.getByText('Memberships')).toBeVisible()
  await expect(page.getByText('ada@example.com')).toBeVisible()

  // Invite flow: email + role round-trip through the generated client.
  await page.getByTestId('platform-invite-user-button').click()
  await expect(page).toHaveURL(/\/invite$/)

  const inviteeEmail = 'invitee@example.com'
  await page.getByPlaceholder('invitee@example.com').fill(inviteeEmail)
  await page.getByRole('combobox').selectOption('member')
  await page.getByTestId('platform-invite-submit').click()

  // Success returns to the detail where the pending invitation is listed.
  await expect(page).toHaveURL(/\/platform\/organisations\/.+/)
  await expect(page.getByText(inviteeEmail)).toBeVisible()

  // The invitee accepted at login (login-time linking, Scope §6.5) and now
  // appears in the memberships with the intended role.
  await expect(page.getByRole('row').filter({ hasText: inviteeEmail })).toBeVisible()
  await expect(page.getByRole('row').filter({ hasText: inviteeEmail })).toContainText('member')

  // The platform fixture recorded the invitation, membership and audit event.
  expect(platformFixture.invitations).toHaveLength(1)
  expect(platformFixture.invitations[0]).toMatchObject({
    email: inviteeEmail,
    role_code: 'member',
    status: 'sent',
  })
  expect(platformFixture.memberships.some((entry) => entry.user_email === inviteeEmail)).toBe(true)
  expect(platformFixture.auditEvents.some((entry) => entry.action === 'invitation.sent')).toBe(true)

  // The platform calls carry the Bearer token on every request. X-Org-Id is
  // attached by the client whenever an organisation is selected (v0.3 Scope
  // §6.3) and ignored by the backend on platform routes (Scope §6.2 — the
  // platform plane never consults it), which the security suite proves
  // server-side.
  const platformCalls = capturedHeaders.filter(
    (entry) => entry.authorization !== null && entry.orgId !== null,
  )
  expect(platformCalls.length).toBeGreaterThan(0)
  for (const call of platformCalls) {
    expect(call.authorization).toMatch(/^Bearer /)
  }
})
