import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import type { Pinia } from 'pinia'

vi.mock('@/api/client', () => ({
  client: {
    GET: vi.fn<typeof client.GET>(),
    PATCH: vi.fn<typeof client.PATCH>(),
    POST: vi.fn<typeof client.POST>(),
  },
}))

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import {
  notificationsQueryKeys,
  useMarkNotificationReadMutation,
  useNotificationsQuery,
  useSendTestNotificationMutation,
  useUnreadNotificationsCountQuery,
} from '@/queries/notifications'
import { queryClient } from '@/queries/queryClient'
import { useOrganisationStore } from '@/stores/organisation'

type NotificationListResponse = components['schemas']['NotificationListResponse']
type NotificationListItem = components['schemas']['NotificationListItem']

const ORG_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const NOTIFICATION_ID = '11111111-1111-4111-8111-111111111111'

const getMock = vi.mocked(client.GET)
const patchMock = vi.mocked(client.PATCH)
const postMock = vi.mocked(client.POST)

const notification: NotificationListItem = {
  id: NOTIFICATION_ID,
  type: 'notification.test_sent',
  title: 'Test notification',
  body: 'This is a test notification.',
  resource_type: 'notification',
  resource_id: null,
  read_at: null,
  created_at: '2026-01-01T00:00:00Z',
}

const readNotification: NotificationListItem = {
  ...notification,
  read_at: '2026-01-02T00:00:00Z',
}

function listEnvelope(
  items: NotificationListItem[] = [],
  unreadCount = 0,
): NotificationListResponse {
  return { items, page: 1, page_size: 50, total: items.length, unread_count: unreadCount }
}

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

let pinia: Pinia

let captured!: {
  list: ReturnType<typeof useNotificationsQuery>
  unread: ReturnType<typeof useUnreadNotificationsCountQuery>
  markRead: ReturnType<typeof useMarkNotificationReadMutation>
  sendTest: ReturnType<typeof useSendTestNotificationMutation>
}

function mountQueries(): void {
  const CapturingComponent = defineComponent({
    setup() {
      captured = {
        list: useNotificationsQuery({ page: 1, pageSize: 50 }),
        unread: useUnreadNotificationsCountQuery(),
        markRead: useMarkNotificationReadMutation(),
        sendTest: useSendTestNotificationMutation(),
      }
      return {}
    },
    template: '<div />',
  })
  mount(CapturingComponent, {
    global: { plugins: [pinia, [VueQueryPlugin, { queryClient }]] },
  })
}

const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

describe('notifications query composables', () => {
  beforeEach(() => {
    localStorage.clear()
    pinia = createPinia()
    setActivePinia(pinia)
    getMock.mockReset()
    patchMock.mockReset()
    postMock.mockReset()
    queryClient.clear()
    invalidateSpy.mockClear()
  })

  it('maps camelCase list params to the snake_case API query and returns the envelope with unread count', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([notification], 1), error: undefined })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/notifications', {
      params: { query: { page: 1, page_size: 50, type: undefined } },
    })
    expect(captured.list.isSuccess.value).toBe(true)
    expect(captured.list.data.value?.total).toBe(1)
    expect(captured.list.data.value?.unread_count).toBe(1)
    expect(captured.list.data.value?.items[0]?.title).toBe('Test notification')
  })

  it('forwards the approved type filter to the API', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([], 0), error: undefined })

    const CapturingComponent = defineComponent({
      setup() {
        captured = {
          ...captured,
          list: useNotificationsQuery({ page: 2, pageSize: 25, type: 'file.ready' }),
        }
        return {}
      },
      template: '<div />',
    })
    mount(CapturingComponent, {
      global: { plugins: [pinia, [VueQueryPlugin, { queryClient }]] },
    })
    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/notifications', {
      params: { query: { page: 2, page_size: 25, type: 'file.ready' } },
    })
  })

  it('stays disabled without a selected organisation', async () => {
    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).not.toHaveBeenCalled()
  })

  it('fetches the unread count through the generated client', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: { unread_count: 3 }, error: undefined })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/notifications/unread-count')
    expect(captured.unread.isSuccess.value).toBe(true)
    expect(captured.unread.data.value?.unread_count).toBe(3)
  })

  it('mark-read mutation PATCHes the notification and invalidates lists and the unread count', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([notification], 1), error: undefined })
    patchMock.mockResolvedValue({
      data: readNotification,
      error: undefined,
      response: new Response(),
    })

    mountQueries()
    await flushPromises()
    await flushPromises()

    await captured.markRead.mutateAsync(NOTIFICATION_ID)
    await flushPromises()

    expect(patchMock).toHaveBeenCalledWith('/api/v1/notifications/{notification_id}/read', {
      params: { path: { notification_id: NOTIFICATION_ID } },
    })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: notificationsQueryKeys.lists(ORG_A),
    })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: notificationsQueryKeys.unreadCount(ORG_A),
    })
  })

  it('test-send mutation POSTs and invalidates lists and the unread count', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    getMock.mockResolvedValue({ data: listEnvelope([], 0), error: undefined })
    postMock.mockResolvedValue({
      data: notification,
      error: undefined,
      response: new Response(),
    })

    mountQueries()
    await flushPromises()
    await flushPromises()

    await captured.sendTest.mutateAsync()
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/api/v1/notifications/test')
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: notificationsQueryKeys.lists(ORG_A),
    })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: notificationsQueryKeys.unreadCount(ORG_A),
    })
  })

  it('surfaces the client error when the list request fails', async () => {
    const organisation = useOrganisationStore()
    organisation.setSelectedOrganisation(ORG_A)
    const mockError = { code: 'notifications.list_failed', message: 'Boom' }
    getMock.mockResolvedValue({ data: undefined, error: mockError })

    mountQueries()
    await flushPromises()
    await flushPromises()

    expect(captured.list.isError.value).toBe(true)
    expect(captured.list.error.value).toEqual(mockError)
  })
})
