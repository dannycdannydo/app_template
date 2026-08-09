import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import type { components } from '@/api/generated/openapi'
import type { NotificationsListParams } from '@/queries/notifications'
import type { MaybeRefOrGetter } from 'vue'

const mockUseNotificationsQuery = vi.hoisted(() =>
  vi.fn<(params: MaybeRefOrGetter<NotificationsListParams>) => unknown>(),
)
const mockUseNotificationPermissions = vi.hoisted(() => vi.fn<() => unknown>())
const mockUseMarkNotificationReadMutation = vi.hoisted(() =>
  vi.fn<(options?: unknown) => unknown>(),
)
const mockUseSendTestNotificationMutation = vi.hoisted(() =>
  vi.fn<(options?: unknown) => unknown>(),
)
const mockShowApiErrorToast = vi.hoisted(() => vi.fn<(error: unknown, options?: unknown) => void>())
const mockShowSuccessToast = vi.hoisted(() => vi.fn<(message: string) => void>())

vi.mock('@/queries/notifications', () => ({
  useNotificationsQuery: mockUseNotificationsQuery,
  useMarkNotificationReadMutation: mockUseMarkNotificationReadMutation,
  useSendTestNotificationMutation: mockUseSendTestNotificationMutation,
}))

vi.mock('@/lib/permissions', () => ({
  useNotificationPermissions: mockUseNotificationPermissions,
}))

vi.mock('@/lib/toast', () => ({
  showApiErrorToast: mockShowApiErrorToast,
  showSuccessToast: mockShowSuccessToast,
}))

import NotificationsListView from '@/views/NotificationsListView.vue'

type NotificationListItem = components['schemas']['NotificationListItem']
type NotificationListResponse = components['schemas']['NotificationListResponse']

const UNREAD_ID = '11111111-1111-4111-8111-111111111111'
const READ_ID = '22222222-2222-4222-8222-222222222222'

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

function listResponse(items: NotificationListItem[] = [unread, read]): NotificationListResponse {
  return { items, page: 1, page_size: 25, total: items.length, unread_count: 1 }
}

interface PermissionsShape {
  canRead: boolean
  canManage: boolean
}

let wrapper: VueWrapper

function stubPermissions(permissions: PermissionsShape, mePending = false): void {
  mockUseNotificationPermissions.mockReturnValue({
    permissions: ref(permissions),
    mePending: ref(mePending),
  })
}

function stubListQuery(overrides: Record<string, unknown> = {}): void {
  mockUseNotificationsQuery.mockReturnValue({
    data: ref(listResponse()),
    isPending: ref(false),
    isError: ref(false),
    error: ref(null),
    ...overrides,
  })
}

function stubMutations(): void {
  mockUseMarkNotificationReadMutation.mockReturnValue({
    mutate: vi.fn<(id: string) => void>(),
    isPending: ref(false),
  })
  mockUseSendTestNotificationMutation.mockReturnValue({
    mutate: vi.fn<() => void>(),
    isPending: ref(false),
  })
}

async function mountView(): Promise<void> {
  wrapper = mount(NotificationsListView, { global: { plugins: [createPinia()] } })
}

describe('NotificationsListView', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockUseNotificationsQuery.mockReset()
    mockUseNotificationPermissions.mockReset()
    mockUseMarkNotificationReadMutation.mockReset()
    mockUseSendTestNotificationMutation.mockReset()
    mockShowApiErrorToast.mockReset()
    mockShowSuccessToast.mockReset()
    stubPermissions({ canRead: true, canManage: true })
    stubListQuery()
    stubMutations()
  })

  afterEach(() => {
    wrapper?.unmount()
    document.body.innerHTML = ''
  })

  it('renders notifications from the org-scoped query with read/unread state', async () => {
    await mountView()

    const rows = wrapper.findAll('[data-testid="data-table-row"]')
    expect(rows).toHaveLength(2)
    expect(wrapper.text()).toContain('Test notification')
    expect(wrapper.text()).toContain('File ready')
    expect(wrapper.find('[data-testid="notification-status-unread"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="notification-status-read"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('2 notifications')
  })

  it('shows the test-send card for roles with notifications.manage', async () => {
    stubPermissions({ canRead: true, canManage: true })
    await mountView()

    expect(wrapper.find('[data-testid="notifications-test-card"]').exists()).toBe(true)
  })

  it('hides the test-send card for a member (read-only, no manage)', async () => {
    stubPermissions({ canRead: true, canManage: false })
    await mountView()

    expect(wrapper.find('[data-testid="notifications-test-card"]').exists()).toBe(false)
  })

  it('marks a notification read through the mutation', async () => {
    await mountView()

    const markReadMutation = mockUseMarkNotificationReadMutation.mock.results[0]?.value as {
      mutate: ReturnType<typeof vi.fn>
    }
    await wrapper.find(`[data-testid="notification-mark-read-${UNREAD_ID}"]`).trigger('click')

    expect(markReadMutation.mutate).toHaveBeenCalledWith(UNREAD_ID)
    // A read row offers no mark-read action.
    expect(wrapper.find(`[data-testid="notification-mark-read-${READ_ID}"]`).exists()).toBe(false)
  })

  it('sends a test notification through the gated mutation and toasts success', async () => {
    await mountView()

    const sendTestMutation = mockUseSendTestNotificationMutation.mock.results[0]?.value as {
      mutate: ReturnType<typeof vi.fn>
    }
    await wrapper.find('[data-testid="notifications-test-send"]').trigger('click')

    expect(sendTestMutation.mutate).toHaveBeenCalledOnce()
  })
})
