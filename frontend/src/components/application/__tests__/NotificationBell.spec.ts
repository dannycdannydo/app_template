import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { components } from '@/api/generated/openapi'

const mockUseUnreadNotificationsCountQuery = vi.hoisted(() => vi.fn<() => unknown>())
const mockUseNotificationsQuery = vi.hoisted(() => vi.fn<(params: unknown) => unknown>())
const mockUseMarkNotificationReadMutation = vi.hoisted(() =>
  vi.fn<(options?: unknown) => unknown>(),
)
const mockUseMarkAllNotificationsReadMutation = vi.hoisted(() =>
  vi.fn<(options?: unknown) => unknown>(),
)
const mockShowApiErrorToast = vi.hoisted(() => vi.fn<(error: unknown, options?: unknown) => void>())

vi.mock('@/queries/notifications', () => ({
  useUnreadNotificationsCountQuery: mockUseUnreadNotificationsCountQuery,
  useNotificationsQuery: mockUseNotificationsQuery,
  useMarkNotificationReadMutation: mockUseMarkNotificationReadMutation,
  useMarkAllNotificationsReadMutation: mockUseMarkAllNotificationsReadMutation,
}))

vi.mock('@/lib/toast', () => ({
  showApiErrorToast: mockShowApiErrorToast,
  showSuccessToast: vi.fn<(message: string) => void>(),
}))

import NotificationBell from '@/components/application/NotificationBell.vue'

type NotificationListItem = components['schemas']['NotificationListItem']

const UNREAD_ID = '11111111-1111-4111-8111-111111111111'
const READ_ID = '22222222-2222-4222-8222-222222222222'
const SECOND_UNREAD_ID = '33333333-3333-4333-8333-333333333333'

const unread: NotificationListItem = {
  id: UNREAD_ID,
  type: 'notification.test_sent',
  title: 'Test notification',
  body: 'This is a test notification.',
  resource_type: 'notification',
  resource_id: null,
  read_at: null,
  created_at: '2026-01-01T00:00:00Z',
}

const read: NotificationListItem = {
  id: READ_ID,
  type: 'file.ready',
  title: 'File ready',
  body: 'Your file report.pdf is ready.',
  resource_type: 'file',
  resource_id: 'file-1',
  read_at: '2026-01-02T00:00:00Z',
  created_at: '2026-01-02T00:00:00Z',
}

const secondUnread: NotificationListItem = {
  ...unread,
  id: SECOND_UNREAD_ID,
  title: 'Second test notification',
}

let wrapper: VueWrapper

function buildRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/notifications', name: 'notifications', component: { template: '<div />' } }],
  })
}

function stubQueries(items: NotificationListItem[], unreadCount: number): void {
  mockUseUnreadNotificationsCountQuery.mockReturnValue({
    data: ref({ unread_count: unreadCount }),
  })
  mockUseNotificationsQuery.mockReturnValue({
    data: ref({
      items,
      page: 1,
      page_size: 5,
      total: items.length,
      unread_count: unreadCount,
    }),
    isPending: ref(false),
  })
}

async function mountBell(): Promise<void> {
  stubQueries([unread, secondUnread, read], 2)
  mockUseMarkNotificationReadMutation.mockReturnValue({
    mutate: vi.fn<(id: string) => void>(),
    isPending: ref(false),
  })
  mockUseMarkAllNotificationsReadMutation.mockReturnValue({
    mutate: vi.fn<() => void>(),
    isPending: ref(false),
  })

  wrapper = mount(NotificationBell, {
    global: { plugins: [createPinia(), buildRouter()] },
  })
  await flushPromises()
}

describe('NotificationBell', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockUseUnreadNotificationsCountQuery.mockReset()
    mockUseNotificationsQuery.mockReset()
    mockUseMarkNotificationReadMutation.mockReset()
    mockUseMarkAllNotificationsReadMutation.mockReset()
    mockShowApiErrorToast.mockReset()
  })

  afterEach(() => {
    // Unmount and clear portal leftovers (reka-ui teleports open menus to
    // document.body, which would otherwise leak into the next test).
    wrapper?.unmount()
    document.body.innerHTML = ''
  })

  it('shows the bell trigger with the unread badge from the unread-count query', async () => {
    await mountBell()

    expect(wrapper.find('[data-testid="notification-bell-trigger"]').exists()).toBe(true)
    const badge = wrapper.find('[data-testid="notification-bell-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('2')
  })

  it('hides the badge when there is nothing unread', async () => {
    stubQueries([read], 0)
    mockUseMarkNotificationReadMutation.mockReturnValue({
      mutate: vi.fn<(id: string) => void>(),
      isPending: ref(false),
    })
    mockUseMarkAllNotificationsReadMutation.mockReturnValue({
      mutate: vi.fn<() => void>(),
      isPending: ref(false),
    })

    wrapper = mount(NotificationBell, {
      global: { plugins: [createPinia(), buildRouter()] },
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="notification-bell-badge"]').exists()).toBe(false)
  })

  it('opens a dropdown listing recent notifications with titles, bodies and mark-read actions', async () => {
    await mountBell()

    await wrapper.find('[data-testid="notification-bell-trigger"]').trigger('click')
    await flushPromises()

    expect(
      document.querySelector(`[data-testid="notification-bell-title-${UNREAD_ID}"]`)?.textContent,
    ).toBe('Test notification')
    expect(
      document.querySelector(`[data-testid="notification-bell-title-${READ_ID}"]`)?.textContent,
    ).toBe('File ready')
    // An unread row offers a mark-read action; a read row does not.
    expect(
      document.querySelector(`[data-testid="notification-bell-mark-read-${UNREAD_ID}"]`),
    ).not.toBeNull()
    expect(
      document.querySelector(`[data-testid="notification-bell-mark-read-${READ_ID}"]`),
    ).toBeNull()
  })

  it('marks a notification read through the mutation', async () => {
    await mountBell()

    await wrapper.find('[data-testid="notification-bell-trigger"]').trigger('click')
    await flushPromises()

    const markReadMutation = mockUseMarkNotificationReadMutation.mock.results[0]?.value as {
      mutate: ReturnType<typeof vi.fn>
    }
    const markReadButton = document.querySelector(
      `[data-testid="notification-bell-mark-read-${UNREAD_ID}"]`,
    ) as HTMLElement
    markReadButton.click()
    await flushPromises()

    expect(markReadMutation.mutate).toHaveBeenCalledWith(UNREAD_ID)
  })

  it('only disables the notification currently being marked read', async () => {
    await mountBell()

    await wrapper.find('[data-testid="notification-bell-trigger"]').trigger('click')
    await flushPromises()

    const firstButton = document.querySelector(
      `[data-testid="notification-bell-mark-read-${UNREAD_ID}"]`,
    ) as HTMLButtonElement
    const secondButton = document.querySelector(
      `[data-testid="notification-bell-mark-read-${SECOND_UNREAD_ID}"]`,
    ) as HTMLButtonElement

    firstButton.click()
    await flushPromises()

    expect(firstButton.disabled).toBe(true)
    expect(secondButton.disabled).toBe(false)
  })

  it('renders an empty state without a bulk action when there are no unread notifications', async () => {
    stubQueries([], 0)
    mockUseMarkNotificationReadMutation.mockReturnValue({
      mutate: vi.fn<(id: string) => void>(),
      isPending: ref(false),
    })
    mockUseMarkAllNotificationsReadMutation.mockReturnValue({
      mutate: vi.fn<() => void>(),
      isPending: ref(false),
    })

    wrapper = mount(NotificationBell, {
      global: { plugins: [createPinia(), buildRouter()] },
    })
    await flushPromises()

    await wrapper.find('[data-testid="notification-bell-trigger"]').trigger('click')
    await flushPromises()

    expect(document.querySelector('[data-testid="notification-bell-empty"]')).not.toBeNull()
    expect(document.querySelector('[data-testid="notification-bell-mark-all-read"]')).toBeNull()
  })

  it('marks every unread notification read through the bulk mutation', async () => {
    await mountBell()

    await wrapper.find('[data-testid="notification-bell-trigger"]').trigger('click')
    await flushPromises()

    const markAllMutation = mockUseMarkAllNotificationsReadMutation.mock.results[0]?.value as {
      mutate: ReturnType<typeof vi.fn>
    }
    const markAllButton = document.querySelector(
      '[data-testid="notification-bell-mark-all-read"]',
    ) as HTMLElement
    markAllButton.click()
    await flushPromises()

    expect(markAllMutation.mutate).toHaveBeenCalledExactlyOnceWith()
  })
})
