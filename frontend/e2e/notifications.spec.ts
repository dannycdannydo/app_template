import { expect, test } from '@playwright/test'

import { readWorkOsClientId, setupNotificationsJourney } from './helpers'

/**
 * Notifications feature-module journey (Scope §6.5, acceptance §5.6,
 * blueprint §20).
 *
 * An authenticated shell (session injected per the helper contract) walks
 * the notifications list: the shell header shows the bell with the unread
 * badge from the unread-count query, the `/notifications` route lists the
 * caller's notifications with read/unread state, marking one read flips its
 * state, and the badge drops to reflect the new unread count. The backend
 * `/api/v1/**` surface is answered from the stateful notifications fixture,
 * so mark-read mutates the fixture the way the real backend would.
 */
test('lists notifications, marks one read and updates the header badge', async ({ page }) => {
  const clientId = readWorkOsClientId()
  if (clientId === null) {
    test.skip(true, 'VITE_WORKOS_CLIENT_ID is not configured')
    return
  }

  const { notificationsFixture, capturedHeaders } = await setupNotificationsJourney(page, clientId)

  await page.goto('/')
  await expect(page.getByTestId('sidebar')).toBeVisible()

  // The header bell carries the unread badge from the unread-count query.
  await expect(page.getByTestId('notification-bell-badge')).toHaveText('1')

  // Sidebar navigation reaches the notifications list with both rows.
  await page.getByRole('link', { name: 'Notifications' }).first().click()
  await expect(page).toHaveURL(/\/notifications$/)
  await expect(page.getByRole('row').filter({ hasText: 'File ready' })).toBeVisible()
  await expect(page.getByRole('row').filter({ hasText: 'Test notification' })).toBeVisible()
  await expect(page.getByTestId('notification-status-unread')).toHaveCount(1)
  await expect(page.getByTestId('notification-status-read')).toHaveCount(1)

  // Mark the unread row read: the row flips state and the badge drops.
  const unreadId = notificationsFixture.notifications.find((entry) => entry.read_at === null)!.id
  await page.getByTestId(`notification-mark-read-${unreadId}`).click()

  await expect(page.getByTestId('notification-status-unread')).toHaveCount(0)
  await expect(page.getByTestId('notification-status-read')).toHaveCount(2)
  await expect(page.getByTestId('notification-bell-badge')).toHaveCount(0)

  // The client attached the session and tenant context to every call.
  const apiCalls = capturedHeaders.filter((entry) => entry.orgId !== null)
  expect(apiCalls.length).toBeGreaterThan(0)
  for (const call of apiCalls) {
    expect(call.authorization).toMatch(/^Bearer /)
    expect(call.orgId).toBe('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
  }
})
