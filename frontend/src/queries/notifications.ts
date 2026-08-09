import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import type { MaybeRefOrGetter } from 'vue'
import { computed, toValue } from 'vue'

import { client } from '@/api/client'
import type { components } from '@/api/generated/openapi'
import { useOrganisationStore } from '@/stores/organisation'

type NotificationListItem = components['schemas']['NotificationListItem']

/**
 * List parameters accepted by the notifications query layer (Scope §6.5).
 *
 * camelCase on the TS side; the snake_case API query parameters are produced
 * here, in one place (blueprint §12). The `type` filter is optional and only
 * approved values may be sent.
 */
export interface NotificationsListParams {
  page: number
  pageSize: number
  type?: string | null
}

/**
 * Query-key factory for the notifications domain (Scope §6.5).
 *
 * Keys are per-organisation under the `organisations` root so the
 * organisation-switch invalidator covers them automatically
 * (src/queries/records.ts documents the convention). The unread count shares
 * the domain so a mark-read or test-send invalidation can refresh both the
 * list and the header badge from one factory.
 */
export const notificationsQueryKeys = {
  all: ['organisations'] as const,
  lists: (organisationId: string) =>
    ['organisations', organisationId, 'notifications', 'list'] as const,
  list: (organisationId: string, params: NotificationsListParams) =>
    ['organisations', organisationId, 'notifications', 'list', params] as const,
  unreadCount: (organisationId: string) =>
    ['organisations', organisationId, 'notifications', 'unread-count'] as const,
}

/**
 * Paginated, organisation-scoped notifications list (Scope §6.5).
 *
 * Returns only the caller's own notifications in the selected organisation
 * (the backend enforces the org + user scoping). The response envelope
 * carries the caller's `unread_count`, so a single list request refreshes
 * both the table and the header badge. Reads the selected organisation from
 * Pinia and stays disabled without one.
 */
export function useNotificationsQuery(params: MaybeRefOrGetter<NotificationsListParams>) {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)
  const resolvedParams = computed(() => toValue(params))

  return useQuery({
    queryKey: computed(() =>
      organisationId.value === null
        ? (['organisations', null] as const)
        : notificationsQueryKeys.list(organisationId.value, resolvedParams.value),
    ),
    queryFn: async () => {
      if (organisationId.value === null) {
        throw new Error('Cannot list notifications without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/notifications', {
        params: {
          query: {
            page: resolvedParams.value.page,
            page_size: resolvedParams.value.pageSize,
            type: resolvedParams.value.type ?? undefined,
          },
        },
      })
      if (error) throw error
      if (!data) throw new Error('Empty notifications list response')
      return data
    },
    enabled: computed(() => organisationId.value !== null),
    retry: false,
    staleTime: 30_000,
  })
}

/**
 * Unread-count query for the header bell badge (Scope §6.5).
 *
 * Polls on a one-minute interval so the badge tracks deliveries produced by
 * background jobs (file completions, test notifications) without a manual
 * refetch; mutations in this module invalidate the key immediately so the
 * badge also updates the moment the user marks a notification read.
 */
export function useUnreadNotificationsCountQuery() {
  const organisation = useOrganisationStore()
  const organisationId = computed(() => organisation.selectedOrganisationId)

  return useQuery({
    queryKey: computed(() =>
      organisationId.value === null
        ? (['organisations', null] as const)
        : notificationsQueryKeys.unreadCount(organisationId.value),
    ),
    queryFn: async () => {
      if (organisationId.value === null) {
        throw new Error('Cannot load the unread count without a selected organisation')
      }
      const { data, error } = await client.GET('/api/v1/notifications/unread-count')
      if (error) throw error
      if (!data) throw new Error('Empty unread-count response')
      return data
    },
    enabled: computed(() => organisationId.value !== null),
    retry: false,
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
}

/**
 * Mark-one-read mutation (Scope §6.5).
 *
 * PATCHes the caller's notification; a foreign or other-user id is a 404
 * server-side. Success invalidates the notifications lists and the unread
 * count so the table rows and the header badge refresh together.
 */
export function useMarkNotificationReadMutation(options?: {
  onSuccess?: (notification: NotificationListItem) => void
  onError?: (error: unknown) => void
}) {
  const queryClient = useQueryClient()
  const organisation = useOrganisationStore()

  return useMutation({
    mutationFn: async (notificationId: string) => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot mark a notification read without a selected organisation')
      }
      const { data, error } = await client.PATCH('/api/v1/notifications/{notification_id}/read', {
        params: { path: { notification_id: notificationId } },
      })
      if (error) throw error
      if (!data) throw new Error('Empty mark-read response')
      return { organisationId, notification: data }
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({
        queryKey: notificationsQueryKeys.lists(result.organisationId),
      })
      void queryClient.invalidateQueries({
        queryKey: notificationsQueryKeys.unreadCount(result.organisationId),
      })
      options?.onSuccess?.(result.notification)
    },
    onError: (error) => {
      options?.onError?.(error)
    },
  })
}

/**
 * Test-send mutation (Scope §6.5).
 *
 * POSTs to the gated `notifications.manage` endpoint; the backend creates an
 * in-app notification for the caller and enqueues the email delivery. The
 * UI offers the affordance only to manager-bundle holders, but the backend
 * stays the enforcement point. Success invalidates the lists and the unread
 * count so the new notification appears immediately.
 */
export function useSendTestNotificationMutation(options?: {
  onSuccess?: (notification: NotificationListItem) => void
  onError?: (error: unknown) => void
}) {
  const queryClient = useQueryClient()
  const organisation = useOrganisationStore()

  return useMutation({
    mutationFn: async () => {
      const organisationId = organisation.selectedOrganisationId
      if (!organisationId) {
        throw new Error('Cannot send a test notification without a selected organisation')
      }
      const { data, error } = await client.POST('/api/v1/notifications/test')
      if (error) throw error
      if (!data) throw new Error('Empty test notification response')
      return { organisationId, notification: data }
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({
        queryKey: notificationsQueryKeys.lists(result.organisationId),
      })
      void queryClient.invalidateQueries({
        queryKey: notificationsQueryKeys.unreadCount(result.organisationId),
      })
      options?.onSuccess?.(result.notification)
    },
    onError: (error) => {
      options?.onError?.(error)
    },
  })
}
