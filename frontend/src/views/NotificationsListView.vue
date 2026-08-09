<script setup lang="ts">
import { CheckIcon, SendIcon } from '@lucide/vue'
import { computed, h, ref } from 'vue'

import type { ApiError } from '@/api/errors'
import type { components } from '@/api/generated/openapi'
import DataTable from '@/components/application/DataTable.vue'
import type { DataTableColumn } from '@/components/application/DataTable.vue'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { formatDateTime } from '@/lib/format'
import { useNotificationPermissions } from '@/lib/permissions'
import { showApiErrorToast, showSuccessToast } from '@/lib/toast'
import {
  useMarkNotificationReadMutation,
  useNotificationsQuery,
  useSendTestNotificationMutation,
} from '@/queries/notifications'

type NotificationListItem = components['schemas']['NotificationListItem']

/**
 * Notifications list screen (Scope §6.5, blueprint §14, §12, §16).
 *
 * A `DataTable` fed by the org-scoped `useNotificationsQuery` (the standard
 * pagination envelope carrying the caller's own notifications) with a
 * read/unread state column, a per-row mark-read action and a test-send card
 * for `notifications.manage` holders. The backend stays the enforcement
 * point; the permission composable only shapes what the UI offers.
 */
const page = ref(1)
const pageSize = 25

const { data, isPending, isError, error } = useNotificationsQuery(
  computed(() => ({ page: page.value, pageSize })),
)

const { permissions, mePending } = useNotificationPermissions()

const { mutate: markRead, isPending: markReadPending } = useMarkNotificationReadMutation({
  onError: (markError) => {
    showApiErrorToast(markError, { title: 'Could not mark notification as read' })
  },
})

const { mutate: sendTest, isPending: testPending } = useSendTestNotificationMutation({
  onSuccess: () => {
    showSuccessToast('Test notification sent')
  },
  onError: (sendError) => {
    showApiErrorToast(sendError, { title: 'Could not send the test notification' })
  },
})

const notifications = computed(() => data.value?.items ?? [])

const pagination = computed(() => ({
  page: data.value?.page ?? page.value,
  pageSize: data.value?.page_size ?? pageSize,
  total: data.value?.total ?? 0,
}))

const tableError = computed<ApiError | null>(() =>
  isError.value ? (error.value as ApiError) : null,
)

const columns: DataTableColumn<NotificationListItem>[] = [
  {
    key: 'title',
    header: 'Notification',
    cell: (row) =>
      h('div', { class: 'flex flex-col gap-0.5' }, [
        h(
          'span',
          {
            class: row.read_at === null ? 'font-medium' : '',
            'data-testid': 'notification-title',
          },
          row.title,
        ),
        h('span', { class: 'text-muted-foreground text-xs' }, row.body),
      ]),
  },
  {
    key: 'type',
    header: 'Type',
    cell: (row) => h('code', { class: 'text-muted-foreground text-xs' }, row.type),
  },
  {
    key: 'created_at',
    header: 'Received',
    cell: (row) => formatDateTime(row.created_at),
  },
  {
    key: 'status',
    header: 'Status',
    cell: (row) =>
      h(
        'span',
        {
          class: row.read_at === null ? 'text-foreground font-medium' : 'text-muted-foreground',
          'data-testid':
            row.read_at === null ? 'notification-status-unread' : 'notification-status-read',
        },
        row.read_at === null ? 'Unread' : 'Read',
      ),
  },
  {
    key: 'actions',
    header: '',
    align: 'right',
    className: 'w-32',
    cell: (row) =>
      row.read_at === null
        ? h(
            Button,
            {
              variant: 'ghost',
              size: 'icon-sm',
              disabled: markReadPending.value,
              'aria-label': `Mark ${row.title} as read`,
              'data-testid': `notification-mark-read-${row.id}`,
              onClick: () => markRead(row.id),
            },
            { default: () => h(CheckIcon) },
          )
        : '',
  },
]

function handleSendTest(): void {
  if (testPending.value) return
  sendTest()
}
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-semibold">Notifications</h1>
      <p class="text-muted-foreground mt-1 text-sm">
        Your notifications in the selected organisation, newest first.
      </p>
    </div>

    <Card v-if="!mePending && permissions.canManage" data-testid="notifications-test-card">
      <CardHeader>
        <CardTitle>Test notification</CardTitle>
        <CardDescription>
          Sends a test in-app notification and enqueues an email delivery to your address.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Button
          :disabled="testPending"
          data-testid="notifications-test-send"
          @click="handleSendTest"
        >
          <SendIcon />
          {{ testPending ? 'Sending…' : 'Send test notification' }}
        </Button>
      </CardContent>
    </Card>

    <Card>
      <CardHeader>
        <CardTitle>All notifications</CardTitle>
        <CardDescription>
          {{ pagination.total }} notification{{ pagination.total === 1 ? '' : 's' }} in this
          organisation.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          :columns="columns"
          :data="notifications"
          row-key="id"
          :pagination="pagination"
          :loading="isPending"
          :error="tableError"
          empty-message="No notifications yet."
          @update:page="page = $event"
        />
      </CardContent>
    </Card>
  </div>
</template>
